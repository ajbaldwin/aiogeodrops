from datetime import UTC, datetime

import aiohttp
import pytest

from aiogeodrops import GeoDropsClient, GeoDropsConnectionError, GeoDropsCredentialsError
from aiogeodrops.client import (
    COLUMNS,
    TABLE,
    build_latest_query,
    build_serial_lookup_query,
)

from .conftest import API, QUERIES, TABLE_META, TOKEN, FakeGoogle, query_result, table_schema

ALL_COLUMNS = [*COLUMNS, "createdAtOrigin"]

_FIELDS = [
    ("deviceId", "INTEGER"),
    ("mfgSn", "STRING"),
    ("date", "TIMESTAMP"),
    ("moistureIndex", "INTEGER"),
    ("moisturePct", "FLOAT"),
    ("qcnDepth1", "INTEGER"),
    ("qcnDepth2", "INTEGER"),
    ("qcnDepth3", "INTEGER"),
    ("miscBattPercent", "FLOAT"),
]
_ROW_1001 = ["1001", "AAA111", "1727589600000000", "3", "42.5", "2", "2", "1", "88.0"]
_ROW_1002 = ["1002", "BBB222", "1727589600000000", None, None, None, None, None, None]


@pytest.fixture
def client(session: aiohttp.ClientSession, key_json: str, google: FakeGoogle) -> GeoDropsClient:
    return GeoDropsClient(
        session, "my-project", key_json, api_root=google.url(API), token_uri=google.url(TOKEN)
    )


def test_unusable_key_fails_at_construction(session: aiohttp.ClientSession) -> None:
    with pytest.raises(GeoDropsCredentialsError):
        GeoDropsClient(session, "my-project", "{}")


def test_project_id(client: GeoDropsClient) -> None:
    assert client.project_id == "my-project"


def test_latest_query_filters_ids_and_lookback() -> None:
    sql = build_latest_query([1001, 1002], 12)
    assert f"FROM `{TABLE}`" in sql
    assert "deviceId IN (1001, 1002)" in sql
    assert "INTERVAL 12 HOUR" in sql
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY deviceId ORDER BY date DESC) = 1" in sql


def test_latest_query_refuses_non_numeric_ids() -> None:
    # device ids are interpolated, so anything but an int must never reach the SQL
    with pytest.raises(ValueError, match="invalid literal"):
        build_latest_query(["1001) OR (1=1"], 12)  # type: ignore[list-item]


def test_serial_lookup_query_is_parameterized() -> None:
    sql = build_serial_lookup_query(24)
    assert "mfgSn = @serial" in sql
    assert "INTERVAL 24 HOUR" in sql
    assert "LIMIT 1" in sql


async def test_validate_access_queries_a_literal(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("POST", QUERIES, json=query_result([("f0_", "INTEGER")], [["1"]]))
    await client.validate_access()
    (request,) = google.sent("POST", QUERIES)
    assert request.json["query"] == f"SELECT 1 FROM `{TABLE}` LIMIT 1"


async def test_fetch_latest_maps_readings_by_device_id(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    google.reply("POST", QUERIES, json=query_result(_FIELDS, [_ROW_1001, _ROW_1002]))

    readings = await client.fetch_latest([1001, 1002], 6)

    assert set(readings) == {1001, 1002}
    front = readings[1001]
    assert front.moisture_pct == 42.5
    assert front.moisture_index == 3
    assert (front.qcn_d1, front.qcn_d3) == (2, 1)
    assert front.battery_pct == 88.0
    assert front.read_at == datetime(2024, 9, 29, 6, 0, tzinfo=UTC)
    assert readings[1002].moisture_pct is None
    assert readings[1002].all_training
    (request,) = google.sent("POST", QUERIES)
    assert "deviceId IN (1001, 1002)" in request.json["query"]
    assert "INTERVAL 6 HOUR" in request.json["query"]


async def test_fetch_latest_with_no_devices_makes_no_query(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    assert await client.fetch_latest([], 12) == {}
    assert google.sent("POST", QUERIES) == []


async def test_lookup_serial_binds_the_serial(client: GeoDropsClient, google: FakeGoogle) -> None:
    google.token_ok()
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    google.reply("POST", QUERIES, json=query_result(_FIELDS, [_ROW_1001]))

    reading = await client.lookup_serial("AAA111", 12)

    assert reading is not None
    assert reading.device_id == 1001
    (request,) = google.sent("POST", QUERIES)
    assert request.json["queryParameters"] == [
        {
            "name": "serial",
            "parameterType": {"type": "STRING"},
            "parameterValue": {"value": "AAA111"},
        }
    ]


async def test_lookup_unknown_serial_is_none(client: GeoDropsClient, google: FakeGoogle) -> None:
    google.token_ok()
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    google.reply("POST", QUERIES, json=query_result(_FIELDS, []))
    assert await client.lookup_serial("ZZZ999", 12) is None


async def test_query_timeout_is_configurable(
    session: aiohttp.ClientSession, key_json: str, google: FakeGoogle
) -> None:
    client = GeoDropsClient(
        session,
        "my-project",
        key_json,
        query_timeout=0.05,
        api_root=google.url(API),
        token_uri=google.url(TOKEN),
    )
    google.token_ok()
    google.reply("POST", QUERIES, json=query_result([], []), delay=0.5)
    with pytest.raises(GeoDropsConnectionError, match=r"within 0\.05 s"):
        await client.validate_access()
