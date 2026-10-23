from datetime import UTC, datetime

import aiohttp
import pytest

from aiogeodrops import (
    BatteryPoint,
    GeoDropsClient,
    GeoDropsConnectionError,
    GeoDropsCredentialsError,
    GeoDropsSchemaError,
)
from aiogeodrops.client import (
    COLUMNS,
    TABLE,
    build_battery_history_query,
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


def test_latest_query_carries_the_last_irrigation() -> None:
    sql = build_latest_query([1001], 12)
    window = (
        "OVER (PARTITION BY deviceId ORDER BY date "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"
    )
    assert f"LAST_VALUE(irrConfidencePct IGNORE NULLS) {window} AS lastIrrConfidencePct" in sql
    assert (
        f"LAST_VALUE(IF(irrConfidencePct IS NULL, NULL, date) IGNORE NULLS) {window} AS lastIrrDate"
    ) in sql


async def test_fetch_latest_reads_the_last_irrigation(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    fields = [
        ("deviceId", "INTEGER"),
        ("irrConfidencePct", "FLOAT"),
        ("lastIrrConfidencePct", "FLOAT"),
        ("lastIrrDate", "TIMESTAMP"),
    ]
    rows = [["1001", None, "0.24", "1727589600000000"], ["1002", None, None, None]]
    google.reply("POST", QUERIES, json=query_result(fields, rows))

    readings = await client.fetch_latest([1001, 1002], 12)

    assert readings[1001].irrigation_confidence_pct is None
    assert readings[1001].last_irrigation_confidence == 0.24
    assert readings[1001].last_irrigation_at == datetime(2024, 9, 29, 6, 0, tzinfo=UTC)
    assert readings[1002].last_irrigation_at is None


def test_latest_query_refuses_non_numeric_ids() -> None:
    # device ids are interpolated, so anything but an int must never reach the SQL
    with pytest.raises(ValueError, match="invalid literal"):
        build_latest_query(["1001) OR (1=1"], 12)  # type: ignore[list-item]


def test_battery_history_query_filters_ids_and_days() -> None:
    sql = build_battery_history_query([1001, 1002], 14)
    assert sql.startswith("SELECT deviceId, date, deviceBattMV\n")
    assert f"FROM `{TABLE}`" in sql
    assert "deviceId IN (1001, 1002)" in sql
    assert "date > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)" in sql
    assert "deviceBattMV IS NOT NULL" in sql
    assert sql.endswith("ORDER BY deviceId, date")


def test_battery_history_query_refuses_non_numeric_input() -> None:
    with pytest.raises(ValueError, match="invalid literal"):
        build_battery_history_query(["1001) OR (1=1"], 14)  # type: ignore[list-item]
    with pytest.raises(ValueError, match="invalid literal"):
        build_battery_history_query([1001], "14 DAY) OR (1=1")  # type: ignore[arg-type]


async def test_fetch_battery_history_groups_points_by_device(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.token_ok()
    google.reply("GET", TABLE_META, json=table_schema(ALL_COLUMNS))
    # As if GeoDrops had changed the column to STRING: numbers sent as text are
    # still read, and anything else is skipped rather than failing the fetch.
    fields = [("deviceId", "INTEGER"), ("date", "TIMESTAMP"), ("deviceBattMV", "STRING")]
    rows = [
        ["1001", "1727589600000000", "5523.0"],
        ["1001", "1727591400000000", "5517.0"],
        ["1001", "1727593200000000", "n/a"],
        ["1002", "1727589600000000", "4270.0"],
    ]
    google.reply("POST", QUERIES, json=query_result(fields, rows))

    history = await client.fetch_battery_history([1001, 1002, 1003], 14)

    assert history == {
        1001: [
            BatteryPoint(datetime(2024, 9, 29, 6, 0, tzinfo=UTC), 5523.0),
            BatteryPoint(datetime(2024, 9, 29, 6, 30, tzinfo=UTC), 5517.0),
        ],
        1002: [BatteryPoint(datetime(2024, 9, 29, 6, 0, tzinfo=UTC), 4270.0)],
    }
    (request,) = google.sent("POST", QUERIES)
    assert "deviceId IN (1001, 1002, 1003)" in request.json["query"]
    assert "INTERVAL 14 DAY" in request.json["query"]


async def test_fetch_battery_history_with_no_devices_makes_no_query(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    assert await client.fetch_battery_history([], 14) == {}
    assert google.sent("POST", QUERIES) == []


async def test_fetch_battery_history_needs_the_voltage_column(
    client: GeoDropsClient, google: FakeGoogle
) -> None:
    google.token_ok()
    columns = [c for c in ALL_COLUMNS if c != "deviceBattMV"]
    google.reply("GET", TABLE_META, json=table_schema(columns))

    with pytest.raises(GeoDropsSchemaError, match="deviceBattMV"):
        await client.fetch_battery_history([1001], 14)
    assert google.sent("POST", QUERIES) == []


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
