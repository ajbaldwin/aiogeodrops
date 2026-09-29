"""A minimal async BigQuery REST client: run one query, return typed rows."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
from typing import Any
from urllib.parse import quote

import aiohttp

from .auth import REQUEST_TIMEOUT, TokenSource
from .exceptions import (
    GeoDropsAccessDeniedError,
    GeoDropsAuthError,
    GeoDropsConnectionError,
    GeoDropsError,
    GeoDropsQueryError,
)

API_ROOT = "https://bigquery.googleapis.com/bigquery/v2"

# How long one jobs.query / getQueryResults call waits for the job to finish
# before returning jobComplete: false.
_SERVER_WAIT_MS = 10_000
# BigQuery reports these as 403 too, but they clear on their own.
_TRANSIENT_403_REASONS = frozenset({"rateLimitExceeded", "quotaExceeded"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

type Row = dict[str, Any]


@dataclass(frozen=True)
class QueryParameter:
    """A named query parameter (`@name` in the SQL)."""

    name: str
    type: str
    value: str


def _timestamp(value: str) -> datetime:
    # Requested as microseconds since the epoch (useInt64Timestamp).
    return _EPOCH + timedelta(microseconds=int(value))


def _bool(value: str) -> bool:
    return value == "true"


_CONVERTERS: dict[str, Callable[[str], Any]] = {
    "INTEGER": int,
    "INT64": int,
    "FLOAT": float,
    "FLOAT64": float,
    "NUMERIC": float,
    "BIGNUMERIC": float,
    "BOOLEAN": _bool,
    "BOOL": _bool,
    "TIMESTAMP": _timestamp,
    "DATE": date.fromisoformat,
    "DATETIME": datetime.fromisoformat,
}


def parse_rows(result: dict[str, Any]) -> list[Row]:
    """Turn a query response's rows into dicts keyed by column name.

    Scalar columns are converted by their schema type; anything else (strings,
    and nested or repeated columns) is returned as BigQuery sent it.
    """
    fields = result.get("schema", {}).get("fields", [])
    return [
        {
            field["name"]: _convert(field, cell.get("v"))
            for field, cell in zip(fields, row["f"], strict=True)
        }
        for row in result.get("rows", [])
    ]


def _convert(field: dict[str, Any], value: Any) -> Any:
    if value is None or field.get("mode") == "REPEATED":
        return value
    converter = _CONVERTERS.get(field["type"])
    return converter(value) if converter else value


def _api_error(status: int, body: dict[str, Any], text: str) -> GeoDropsError:
    error = body.get("error")
    error = error if isinstance(error, dict) else {}
    message = f"{status} {error.get('message') or text[:200] or 'no details'}"
    reasons = {e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)}
    if status == 401:
        return GeoDropsAuthError(message)
    if status == 403:
        if reasons & _TRANSIENT_403_REASONS:
            return GeoDropsConnectionError(message)
        return GeoDropsAccessDeniedError(message)
    if status == 429 or status >= 500:
        return GeoDropsConnectionError(message)
    return GeoDropsQueryError(message)


class BigQuery:
    """Runs queries billed to one project."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenSource,
        project_id: str,
        *,
        api_root: str = API_ROOT,
    ) -> None:
        """Query as `tokens`' service account, billed to `project_id`."""
        self._session = session
        self._tokens = tokens
        self._project_id = project_id
        self._api_root = api_root

    async def query(
        self,
        sql: str,
        params: Sequence[QueryParameter] = (),
        *,
        timeout: float = 60,  # noqa: ASYNC109 - timeouts become GeoDropsConnectionError
    ) -> list[Row]:
        """Run a GoogleSQL query and return all its rows.

        Gives up with GeoDropsConnectionError after `timeout` seconds overall.
        """
        try:
            async with asyncio.timeout(timeout):
                return await self._query(sql, params)
        except TimeoutError as err:
            raise GeoDropsConnectionError(f"Query did not finish within {timeout} s") from err

    async def _query(self, sql: str, params: Sequence[QueryParameter]) -> list[Row]:
        body: dict[str, Any] = {
            "query": sql,
            "useLegacySql": False,
            "timeoutMs": _SERVER_WAIT_MS,
            "formatOptions": {"useInt64Timestamp": True},
        }
        if params:
            body["parameterMode"] = "NAMED"
            body["queryParameters"] = [
                {
                    "name": p.name,
                    "parameterType": {"type": p.type},
                    "parameterValue": {"value": p.value},
                }
                for p in params
            ]
        result = await self._request(
            "POST", f"/projects/{quote(self._project_id, safe=':')}/queries", json=body
        )
        job = result.get("jobReference")
        rows: list[Row] = []
        while True:
            page_token: str | None = None
            if result.get("jobComplete"):
                rows += parse_rows(result)
                if not (page_token := result.get("pageToken")):
                    return rows
            if not isinstance(job, dict) or "jobId" not in job:
                raise GeoDropsQueryError("BigQuery returned an unexpected response (no job id)")
            result = await self._get_results(job, page_token)

    async def _get_results(self, job: dict[str, Any], page_token: str | None) -> dict[str, Any]:
        params = {"timeoutMs": str(_SERVER_WAIT_MS), "formatOptions.useInt64Timestamp": "true"}
        if location := job.get("location"):
            params["location"] = location
        if page_token:
            params["pageToken"] = page_token
        project = job.get("projectId", self._project_id)
        path = f"/projects/{quote(project, safe=':')}/queries/{quote(job['jobId'], safe='')}"
        return await self._request("GET", path, params=params)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        status, body, text = await self._request_once(method, path, **kwargs)
        if status == 401:
            # This can just mean the cached token went stale early. Only a
            # fresh token being refused too means the key itself is bad.
            self._tokens.invalidate()
            status, body, text = await self._request_once(method, path, **kwargs)
        if status < 300:
            return body
        raise _api_error(status, body, text)

    async def _request_once(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[int, dict[str, Any], str]:
        token = await self._tokens.token()
        try:
            async with self._session.request(
                method,
                self._api_root + path,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                allow_redirects=False,
                **kwargs,
            ) as resp:
                status, text = resp.status, await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GeoDropsConnectionError(f"BigQuery request failed: {err!r}") from err
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        return status, body, text
