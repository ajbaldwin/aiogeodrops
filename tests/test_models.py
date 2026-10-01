from datetime import UTC, date, datetime

import pytest

from aiogeodrops import UNCLASSIFIED
from aiogeodrops.models import reading_from_row


def test_missing_values_stay_none_and_zero_is_kept() -> None:
    reading = reading_from_row({"deviceId": 1001, "temperatureCSurface": 0.0})
    assert reading.device_id == 1001
    assert reading.temp_surface == 0.0
    assert reading.moisture_pct is None
    assert reading.battery_pct is None
    assert reading.sync_delay_hours is None
    assert reading.read_at is None


def test_unclassified_quality_and_index_default_to_minus_one() -> None:
    reading = reading_from_row({"deviceId": 1, "qcnDepth1": None, "moistureIndex": None})
    assert reading.qcn_d1 == reading.qcn_d2 == reading.qcn_d3 == UNCLASSIFIED
    assert reading.moisture_index == UNCLASSIFIED
    assert reading.all_training


def test_one_classified_depth_is_not_all_training() -> None:
    assert not reading_from_row({"deviceId": 1, "qcnDepth2": 0}).all_training


def test_timestamp_is_kept_and_naive_means_utc() -> None:
    aware = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)
    assert reading_from_row({"deviceId": 1, "date": aware}).read_at == aware
    naive = datetime(2026, 9, 29, 6, 0)
    assert reading_from_row({"deviceId": 1, "date": naive}).read_at == aware


def test_non_timestamp_date_is_unknown() -> None:
    # e.g. if GeoDrops ever changed the column to a plain DATE
    assert reading_from_row({"deviceId": 1, "date": date(2026, 9, 29)}).read_at is None


def test_numbers_sent_as_text_are_read() -> None:
    # e.g. if GeoDrops changed a FLOAT column to STRING
    reading = reading_from_row(
        {"deviceId": 1, "moisturePct": "42.5", "miscBattPercent": 88, "qcnDepth1": "2"}
    )
    assert reading.moisture_pct == 42.5
    assert reading.battery_pct == 88.0
    assert isinstance(reading.battery_pct, float)
    assert reading.qcn_d1 == 2


@pytest.mark.parametrize("value", ["wet", True, float("nan"), float("inf"), [1], {"v": 1}])
def test_values_that_are_not_numbers_are_unknown(value: object) -> None:
    reading = reading_from_row(
        {"deviceId": 1, "moisturePct": value, "qcnDepth1": value, "moistureIndex": value}
    )
    assert reading.moisture_pct is None
    assert reading.qcn_d1 == UNCLASSIFIED
    assert reading.moisture_index == UNCLASSIFIED


def test_fractional_classification_is_unclassified() -> None:
    assert reading_from_row({"deviceId": 1, "qcnDepth1": 2.5}).qcn_d1 == UNCLASSIFIED
    assert reading_from_row({"deviceId": 1, "qcnDepth1": 2.0}).qcn_d1 == 2


def test_battery_and_signal_fields() -> None:
    reading = reading_from_row(
        {"deviceId": 1, "deviceBattMV": 3010.0, "deviceRssiDbM": -97.0,
         "miscIsBattPoorQuality": False}
    )  # fmt: skip
    assert reading.battery_mv == 3010.0
    assert reading.rssi_dbm == -97.0
    assert reading.battery_poor is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("true", True), ("false", False), (None, None), (1, None),
     ("yes", None)],
)  # fmt: skip
def test_battery_poor_flag(value: object, expected: bool | None) -> None:
    assert (
        reading_from_row({"deviceId": 1, "miscIsBattPoorQuality": value}).battery_poor is expected
    )


def test_battery_and_signal_default_to_unknown() -> None:
    reading = reading_from_row({"deviceId": 1})
    assert (reading.battery_mv, reading.rssi_dbm, reading.battery_poor) == (None, None, None)


def test_overall_quality_and_irrigation_confidence() -> None:
    reading = reading_from_row({"deviceId": 1, "qcn": 2, "irrConfidencePct": 85.0})
    assert reading.qcn == 2
    assert reading.irrigation_confidence_pct == 85.0
    blank = reading_from_row({"deviceId": 1})
    assert (blank.qcn, blank.irrigation_confidence_pct) == (UNCLASSIFIED, None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("DW_M_LOW12,CHK_M_HWR,", {"DW_M_LOW12", "CHK_M_HWR"}), (" ATT_DW_NEW , ", {"ATT_DW_NEW"}),
     ("", set()), (",", set()), (None, set()), (12, None)],
)  # fmt: skip
def test_next_action_codes(value: object, expected: set[str] | None) -> None:
    codes = reading_from_row({"deviceId": 1, "nextAction": value}).next_action
    assert codes == (None if expected is None else frozenset(expected))


def test_next_action_missing_column_is_unknown() -> None:
    assert reading_from_row({"deviceId": 1}).next_action is None
