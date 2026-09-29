from datetime import UTC, date, datetime

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
