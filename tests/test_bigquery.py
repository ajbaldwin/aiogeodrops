from datetime import UTC, date, datetime
import json
from typing import Any

import aiohttp
import pytest

from aiogeodrops import (
    GeoDropsAccessDeniedError,
    GeoDropsAuthError,
    GeoDropsConnectionError,
    GeoDropsQueryError,
)
from aiogeodrops.auth import ServiceAccountKey, TokenSource
from aiogeodrops.bigquery import BigQuery, QueryParameter, parse_rows

from .conftest import API, QUERIES, RESULTS, TOKEN, UNREACHABLE, FakeGoogle, query_result


def _bigquery(
    session: aiohttp.ClientSession,
    key_json: str,
    google: FakeGoogle,
    api_root: str | None = None,
    project: str = "my-project",
) -> BigQuery:
    key = ServiceAccountKey.from_json(key_json)
    tokens = TokenSource(session, key, token_uri=google.url(TOKEN))
    return BigQuery(session, tokens, project, api_root=api_root or google.url(API))


@pytest.fixture
def bigquery(session: aiohttp.ClientSession, key_json: str, google: FakeGoogle) -> BigQuery:
    return _bigquery(session, key_json, google)


def test_parse_rows_converts_by_schema_type() -> None:
    fields = [
        ("i", "INTEGER"),
        ("f", "FLOAT"),
        ("n", "NUMERIC"),
        ("b", "BOOLEAN"),
        ("t", "TIMESTAMP"),
        ("d", "DATE"),
        ("dt", "DATETIME"),
        ("s", "STRING"),
        ("g", "GEOGRAPHY"),
        ("missing", "FLOAT"),
    ]
    row = ["42", "1.5", "2.25", "true", "1727589600000001", "2026-09-29",
           "2026-09-29T06:00:00", "AAA111", "POINT(1 2)", None]  # fmt: skip
    (parsed,) = parse_rows(query_result(fields, [row]))
    assert parsed == {
        "i": 42,
        "f": 1.5,
        "n": 2.25,
        "b": True,
        "t": datetime(2024, 9, 29, 6, 0, 0, 1, tzinfo=UTC),
        "d": date(2026, 9, 29),
        "dt": datetime(2026, 9, 29, 6, 0),
        "s": "AAA111",
        "g": "POINT(1 2)",
        "missing": None,
    }


def test_parse_rows_leaves_repeated_columns_alone() -> None:
    result = {
        "schema": {"fields": [{"name": "r", "type": "INTEGER", "mode": "REPEATED"}]},
        "rows": [{"f": [{"v": [{"v": "1"}]}]}],
    }
    assert parse_rows(result) == [{"r": [{"v": "1"}]}]


def test_parse_rows_without_rows() -> None:
    assert parse_rows({"jobComplete": True}) == []


async def test_query_sends_standard_sql_with_named_parameters(
    bigquery: BigQuery, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("POST", QUERIES, json=query_result([("x", "INTEGER")], [["1"]]))

    rows = await bigquery.query("SELECT @s", [QueryParameter("s", "STRING", "AAA111")])

    assert rows == [{"x": 1}]
    (request,) = google.sent("POST", QUERIES)
    assert request.headers["Authorization"] == "Bearer tok-1"
    assert request.json == {
        "query": "SELECT @s",
        "useLegacySql": False,
        "timeoutMs": 10_000,
        "formatOptions": {"useInt64Timestamp": True},
        "parameterMode": "NAMED",
        "queryParameters": [
            {
                "name": "s",
                "parameterType": {"type": "STRING"},
                "parameterValue": {"value": "AAA111"},
            }
        ],
    }


async def test_query_without_parameters_omits_them(bigquery: BigQuery, google: FakeGoogle) -> None:
    google.token_ok()
    google.reply("POST", QUERIES, json=query_result([], []))
    assert await bigquery.query("SELECT 1") == []
    (request,) = google.sent("POST", QUERIES)
    assert "queryParameters" not in request.json


async def test_project_id_is_escaped_in_the_path(
    session: aiohttp.ClientSession, key_json: str, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("POST", f"{API}/projects/example.com:proj/queries", json=query_result([], []))
    bigquery = _bigquery(session, key_json, google, project="example.com:proj")
    assert await bigquery.query("SELECT 1") == []


async def test_a_slash_in_the_project_id_cannot_change_the_path(
    session: aiohttp.ClientSession, key_json: str, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("POST", f"{API}/projects/a/../b/queries", json=query_result([], []))
    bigquery = _bigquery(session, key_json, google, project="a/../b")
    assert await bigquery.query("SELECT 1") == []
    (request,) = google.sent("POST", f"{API}/projects/a/../b/queries")
    assert request.raw_path == f"{API}/projects/a%2F..%2Fb/queries"


async def test_a_redirect_is_not_followed(bigquery: BigQuery, google: FakeGoogle) -> None:
    # The bearer token must only ever go to the API root it was meant for.
    google.token_ok()
    google.reply(
        "POST", QUERIES, status=302, body="", headers={"Location": google.url("/elsewhere")}
    )
    with pytest.raises(GeoDropsQueryError, match="302"):
        await bigquery.query("SELECT 1")
    assert google.sent("POST", "/elsewhere") == []


async def test_query_waits_for_a_slow_job(bigquery: BigQuery, google: FakeGoogle) -> None:
    google.token_ok()
    job = {"projectId": "my-project", "jobId": "job_1", "location": "EU"}
    google.reply("POST", QUERIES, json={"jobComplete": False, "jobReference": job})
    google.reply("GET", RESULTS, json={"jobComplete": False, "jobReference": job})
    google.reply("GET", RESULTS, json=query_result([("x", "INTEGER")], [["7"]]))

    assert await bigquery.query("SELECT 7") == [{"x": 7}]

    polls = google.sent("GET", RESULTS)
    assert len(polls) == 2
    assert polls[0].query == {
        "timeoutMs": "10000",
        "formatOptions.useInt64Timestamp": "true",
        "location": "EU",
    }


async def test_query_follows_result_pages(bigquery: BigQuery, google: FakeGoogle) -> None:
    google.token_ok()
    fields = [("x", "INTEGER")]
    google.reply("POST", QUERIES, json=query_result(fields, [["1"]], pageToken="page-2"))
    google.reply("GET", RESULTS, json=query_result(fields, [["2"]]))

    assert await bigquery.query("SELECT x") == [{"x": 1}, {"x": 2}]
    (poll,) = google.sent("GET", RESULTS)
    assert poll.query["pageToken"] == "page-2"


async def test_poll_without_location_or_project(bigquery: BigQuery, google: FakeGoogle) -> None:
    google.token_ok()
    google.reply("POST", QUERIES, json={"jobComplete": False, "jobReference": {"jobId": "job_1"}})
    google.reply("GET", RESULTS, json=query_result([], []))
    assert await bigquery.query("SELECT 1") == []
    (poll,) = google.sent("GET", RESULTS)
    assert "location" not in poll.query


async def test_stale_token_is_refreshed_once(bigquery: BigQuery, google: FakeGoogle) -> None:
    google.token_ok("tok-1")
    google.token_ok("tok-2")
    google.reply("POST", QUERIES, status=401, json={"error": {"code": 401, "message": "Invalid"}})
    google.reply("POST", QUERIES, json=query_result([], []))

    assert await bigquery.query("SELECT 1") == []
    requests = google.sent("POST", QUERIES)
    assert [r.headers["Authorization"] for r in requests] == ["Bearer tok-1", "Bearer tok-2"]


async def test_fresh_token_refused_too_is_an_auth_error(
    bigquery: BigQuery, google: FakeGoogle
) -> None:
    google.token_ok("tok-1")
    google.token_ok("tok-2")
    for _ in range(2):
        google.reply(
            "POST", QUERIES, status=401, json={"error": {"code": 401, "message": "Invalid"}}
        )
    with pytest.raises(GeoDropsAuthError, match="401 Invalid"):
        await bigquery.query("SELECT 1")


async def test_revoked_key_is_an_auth_error_without_a_query(
    bigquery: BigQuery, google: FakeGoogle
) -> None:
    google.reply("POST", TOKEN, status=400, json={"error": "invalid_grant"})
    with pytest.raises(GeoDropsAuthError):
        await bigquery.query("SELECT 1")
    assert google.sent("POST", QUERIES) == []


def _error(status: int, reason: str, message: str = "Denied") -> dict[str, Any]:
    return {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, _error(403, "accessDenied"), GeoDropsAccessDeniedError),
        (403, {"error": {"code": 403, "message": "API not enabled"}}, GeoDropsAccessDeniedError),
        (403, _error(403, "rateLimitExceeded"), GeoDropsConnectionError),
        (403, _error(403, "quotaExceeded"), GeoDropsConnectionError),
        (400, _error(400, "invalidQuery", "Syntax error"), GeoDropsQueryError),
        (404, _error(404, "notFound", "Not found: Project"), GeoDropsQueryError),
        (429, _error(429, "rateLimitExceeded"), GeoDropsConnectionError),
        (500, _error(500, "backendError"), GeoDropsConnectionError),
        (503, "<html>unavailable</html>", GeoDropsConnectionError),
        (400, '"just a string"', GeoDropsQueryError),
    ],
)
async def test_api_errors_map_to_exceptions(
    bigquery: BigQuery,
    google: FakeGoogle,
    status: int,
    body: dict[str, Any] | str,
    expected: type[Exception],
) -> None:
    google.token_ok()
    google.reply(
        "POST", QUERIES, status=status, body=body if isinstance(body, str) else json.dumps(body)
    )
    with pytest.raises(expected, match=str(status)) as info:
        await bigquery.query("SELECT 1")
    if expected is GeoDropsQueryError:
        assert not isinstance(info.value, GeoDropsAccessDeniedError)


async def test_error_message_carries_googles_text(bigquery: BigQuery, google: FakeGoogle) -> None:
    google.token_ok()
    google.reply("POST", QUERIES, status=400, json=_error(400, "invalidQuery", "Unrecognized x"))
    with pytest.raises(GeoDropsQueryError, match="400 Unrecognized x"):
        await bigquery.query("SELECT x")


async def test_empty_error_body(bigquery: BigQuery, google: FakeGoogle) -> None:
    google.token_ok()
    google.reply("POST", QUERIES, status=400, body="")
    with pytest.raises(GeoDropsQueryError, match="400 no details"):
        await bigquery.query("SELECT 1")


async def test_unexpected_success_body_is_a_query_error(
    bigquery: BigQuery, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("POST", QUERIES, body='["unexpected"]')
    with pytest.raises(GeoDropsQueryError, match="unexpected response"):
        await bigquery.query("SELECT 1")


async def test_network_failure_is_a_connection_error(
    session: aiohttp.ClientSession, key_json: str, google: FakeGoogle
) -> None:
    google.token_ok()
    bigquery = _bigquery(session, key_json, google, api_root=f"{UNREACHABLE}{API}")
    with pytest.raises(GeoDropsConnectionError, match="BigQuery request failed"):
        await bigquery.query("SELECT 1")


async def test_query_gives_up_after_the_overall_timeout(
    bigquery: BigQuery, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("POST", QUERIES, json=query_result([], []), delay=0.5)
    with pytest.raises(GeoDropsConnectionError, match=r"within 0\.05 s"):
        await bigquery.query("SELECT 1", timeout=0.05)
