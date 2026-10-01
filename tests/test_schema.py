"""The client's handling of GeoDrops changing its table's columns."""

import logging
from types import SimpleNamespace

import aiohttp
import pytest

from aiogeodrops import GeoDropsClient, GeoDropsQueryError, GeoDropsSchemaError
from aiogeodrops import client as client_module
from aiogeodrops.client import COLUMNS, build_latest_query

from .conftest import API, QUERIES, TABLE_META, TOKEN, FakeGoogle, query_result, table_schema

ALL_COLUMNS = [*COLUMNS, "createdAtOrigin", "isRaining"]
_ROW_FIELDS = [("deviceId", "INTEGER"), ("mfgSn", "STRING"), ("moisturePct", "FLOAT")]
_ROW = ["1001", "AAA111", "42.5"]


def _without(*gone: str) -> list[str]:
    return [c for c in ALL_COLUMNS if c not in gone]


def _unknown_column(name: str) -> dict[str, object]:
    message = f"Unrecognized name: {name} at [1:20]"
    return {"error": {"code": 400, "message": message, "errors": [{"reason": "invalidQuery"}]}}


def _selected(google: FakeGoogle, index: int = -1) -> list[str]:
    sql = google.sent("POST", QUERIES)[index].json["query"]
    return sql.partition("\n")[0].removeprefix("SELECT ").split(", ")


@pytest.fixture
def client(session: aiohttp.ClientSession, key_json: str, google: FakeGoogle) -> GeoDropsClient:
    google.token_ok()
    return GeoDropsClient(
        session, "my-project", key_json, api_root=google.url(API), token_uri=google.url(TOKEN)
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    # only the client's clock: the token cache keeps real time
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


async def test_all_columns_present_selects_them_all(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    google.reply("POST", QUERIES, json=query_result(_ROW_FIELDS, [_ROW]))
    await client.fetch_latest([1001], 12)
    assert _selected(google) == list(COLUMNS)
    assert client.missing_columns == frozenset()


async def test_missing_optional_column_is_left_out(
    client: GeoDropsClient, google: FakeGoogle, caplog: pytest.LogCaptureFixture
) -> None:
    google.reply("GET", TABLE_META, json=table_schema(_without("miscBattPercent", "qcnDepth2")))
    google.reply("POST", QUERIES, json=query_result(_ROW_FIELDS, [_ROW]))

    readings = await client.fetch_latest([1001], 12)

    assert readings[1001].moisture_pct == 42.5
    assert readings[1001].battery_pct is None
    assert "miscBattPercent" not in _selected(google)
    assert "qcnDepth2" not in _selected(google)
    assert client.missing_columns == {"miscBattPercent", "qcnDepth2"}
    assert "no longer has column(s) miscBattPercent, qcnDepth2" in caplog.text


async def test_columns_are_cached_then_reread(
    client: GeoDropsClient, google: FakeGoogle, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    google.reply("GET", TABLE_META, json=table_schema(_without("miscBattPercent")))
    for _ in range(3):
        google.reply("POST", QUERIES, json=query_result(_ROW_FIELDS, [_ROW]))

    await client.fetch_latest([1001], 12)
    clock[0] += client_module.SCHEMA_TTL - 1
    await client.fetch_latest([1001], 12)
    assert len(google.sent("GET", TABLE_META)) == 1
    assert caplog.text.count("no longer has column") == 1

    # the column comes back
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    clock[0] += 1
    await client.fetch_latest([1001], 12)
    assert len(google.sent("GET", TABLE_META)) == 2
    assert "miscBattPercent" in _selected(google)
    assert client.missing_columns == frozenset()
    assert "has all its columns again" in caplog.text


async def test_missing_required_column_raises_without_querying(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.reply("GET", TABLE_META, json=table_schema(_without("createdAtOrigin")))
    with pytest.raises(GeoDropsSchemaError, match="no longer has column\\(s\\) createdAtOrigin"):
        await client.fetch_latest([1001], 12)
    assert google.sent("POST", QUERIES) == []

    # re-checked on the next call rather than cached
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    google.reply("POST", QUERIES, json=query_result(_ROW_FIELDS, [_ROW]))
    assert 1001 in await client.fetch_latest([1001], 12)


async def test_serial_lookup_needs_the_serial_column(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.reply("GET", TABLE_META, json=table_schema(_without("mfgSn")))
    with pytest.raises(GeoDropsSchemaError, match="mfgSn"):
        await client.lookup_serial("AAA111", 12)

    # polling by device id doesn't need it
    google.reply("GET", TABLE_META, json=table_schema(_without("mfgSn")))
    google.reply("POST", QUERIES, json=query_result(_ROW_FIELDS, [_ROW]))
    await client.fetch_latest([1001], 12)
    assert "mfgSn" not in _selected(google)


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (403, {"error": {"code": 403, "message": "no tables.get"}}),
        (404, {"error": {"code": 404, "message": "Not found: Table"}}),
        (200, {"id": "x"}),
    ],
    ids=["access-denied", "not-found", "no-schema"],
)
async def test_unreadable_columns_select_everything(
    client: GeoDropsClient, google: FakeGoogle, status: int, payload: dict[str, object]
) -> None:
    google.reply("GET", TABLE_META, status=status, json=payload)
    google.reply("POST", QUERIES, json=query_result(_ROW_FIELDS, [_ROW]))
    await client.fetch_latest([1001], 12)
    assert _selected(google) == list(COLUMNS)
    assert client.missing_columns == frozenset()


async def test_unknown_column_rereads_columns_and_retries(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    google.reply("POST", QUERIES, status=400, json=_unknown_column("miscBattPercent"))
    google.reply("GET", TABLE_META, json=table_schema(_without("miscBattPercent")))
    google.reply("POST", QUERIES, json=query_result(_ROW_FIELDS, [_ROW]))

    readings = await client.fetch_latest([1001], 12)

    assert readings[1001].battery_pct is None
    assert "miscBattPercent" in _selected(google, 0)
    assert "miscBattPercent" not in _selected(google, 1)
    assert client.missing_columns == {"miscBattPercent"}


async def test_unknown_column_after_reread_is_a_schema_error(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    # e.g. the columns can't be read, so the retry selects everything again
    for _ in range(2):
        google.reply("GET", TABLE_META, status=403, json={"error": {"message": "denied"}})
        google.reply("POST", QUERIES, status=400, json=_unknown_column("miscBattPercent"))
    with pytest.raises(GeoDropsSchemaError, match="Unrecognized name: miscBattPercent"):
        await client.fetch_latest([1001], 12)


async def test_other_query_errors_are_not_retried(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    google.reply("POST", QUERIES, status=400, json={"error": {"message": "Syntax error"}})
    with pytest.raises(GeoDropsQueryError, match="Syntax error") as err:
        await client.fetch_latest([1001], 12)
    assert not isinstance(err.value, GeoDropsSchemaError)
    assert len(google.sent("POST", QUERIES)) == 1


async def test_other_error_on_the_retry_is_raised_as_is(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    google.reply("POST", QUERIES, status=400, json=_unknown_column("qcnDepth3"))
    google.reply("GET", TABLE_META, json=table_schema(_without("qcnDepth3")))
    google.reply("POST", QUERIES, status=400, json={"error": {"message": "Syntax error"}})
    with pytest.raises(GeoDropsQueryError, match="Syntax error") as err:
        await client.fetch_latest([1001], 12)
    assert not isinstance(err.value, GeoDropsSchemaError)


async def test_table_columns_lists_every_column(client: GeoDropsClient, google: FakeGoogle) -> None:
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    assert await client.table_columns() == ALL_COLUMNS


def test_latest_query_selects_the_given_columns() -> None:
    sql = build_latest_query([1001], 12, ["deviceId", "date"])
    assert sql.startswith("SELECT deviceId, date\n")
