import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import json
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

TOKEN = "/token"
API = "/bigquery/v2"
QUERIES = f"{API}/projects/my-project/queries"
RESULTS = f"{QUERIES}/job_1"
TABLE_META = f"{API}/projects/geodrops-prod/datasets/db_public/tables/p_sensor_unified"
# Nothing listens here, so connecting fails straight away.
UNREACHABLE = "http://127.0.0.1:1"


@dataclass
class Sent:
    """A request the fake Google server received."""

    method: str
    path: str
    raw_path: str
    query: dict[str, str]
    headers: dict[str, str]
    json: Any = None
    form: dict[str, str] = field(default_factory=dict)


@dataclass
class _Reply:
    status: int
    payload: Any
    body: str | None
    delay: float
    headers: dict[str, str]


class FakeGoogle:
    """Stands in for Google's token endpoint and the BigQuery REST API.

    Tests queue replies per (method, path); each request takes the next one.
    """

    def __init__(self) -> None:
        self.server: TestServer | None = None
        self._replies: dict[tuple[str, str], list[_Reply]] = defaultdict(list)
        self._sent: list[Sent] = []
        self.unexpected: list[str] = []

    def url(self, path: str) -> str:
        assert self.server is not None
        return str(self.server.make_url(path))

    def reply(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json: Any = None,
        body: str | None = None,
        delay: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._replies[(method, path)].append(_Reply(status, json, body, delay, headers or {}))

    def token_ok(self, token: str = "tok-1", expires_in: int = 3600) -> None:
        self.reply("POST", TOKEN, json={"access_token": token, "expires_in": expires_in})

    def sent(self, method: str, path: str) -> list[Sent]:
        return [s for s in self._sent if s.method == method and s.path == path]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        sent = Sent(
            request.method,
            request.path,
            request.raw_path.partition("?")[0],
            dict(request.query),
            dict(request.headers),
        )
        if request.content_type == "application/json":
            sent.json = await request.json()
        elif request.content_type == "application/x-www-form-urlencoded":
            sent.form = {k: str(v) for k, v in (await request.post()).items()}
        self._sent.append(sent)
        queue = self._replies.get((request.method, request.path))
        if not queue:
            self.unexpected.append(f"{request.method} {request.path}")
            return web.Response(status=599, text="unexpected request")
        reply = queue.pop(0)
        if reply.delay:
            await asyncio.sleep(reply.delay)
        if reply.body is not None:
            return web.Response(status=reply.status, text=reply.body, headers=reply.headers)
        return web.json_response(reply.payload, status=reply.status, headers=reply.headers)


@pytest.fixture
async def google() -> AsyncIterator[FakeGoogle]:
    fake = FakeGoogle()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    async with TestServer(app) as server:
        fake.server = server
        yield fake
    assert fake.unexpected == []


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def key_info(rsa_key: rsa.RSAPrivateKey) -> dict[str, str]:
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "project_id": "key-project",
        "private_key_id": "kid-1",
        "private_key": pem,
        "client_email": "sa@key-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


@pytest.fixture
def key_json(key_info: dict[str, str]) -> str:
    return json.dumps(key_info)


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def query_result(
    fields: list[tuple[str, str]],
    rows: list[list[Any]],
    **extra: Any,
) -> dict[str, Any]:
    """A completed jobs.query / getQueryResults response."""
    return {
        "jobComplete": True,
        "jobReference": {"projectId": "my-project", "jobId": "job_1", "location": "US"},
        "schema": {"fields": [{"name": n, "type": t, "mode": "NULLABLE"} for n, t in fields]},
        "rows": [{"f": [{"v": v} for v in row]} for row in rows],
        **extra,
    }


def table_schema(columns: list[str]) -> dict[str, Any]:
    """A tables.get response listing these columns."""
    return {
        "id": "geodrops-prod:db_public.p_sensor_unified",
        "schema": {"fields": [{"name": c, "type": "STRING", "mode": "NULLABLE"} for c in columns]},
    }
