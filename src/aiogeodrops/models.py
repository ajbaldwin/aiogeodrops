"""A GeoDrops probe reading."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# GeoDrops' value for a quality/moisture-index column with no classification
# yet (the probe is still training).
UNCLASSIFIED = -1


@dataclass(frozen=True)
class DeviceReading:
    """One probe's latest reading.

    Numeric fields are None when GeoDrops has no value for them. The quality
    (qcn) fields and moisture_index are UNCLASSIFIED (-1) instead.
    """

    device_id: int
    sync_delay_hours: float | None
    moisture_index: int
    moisture_pct: float | None
    moisture_d1: float | None
    moisture_d2: float | None
    moisture_d3: float | None
    temp_surface: float | None
    temp_d1: float | None
    temp_d2: float | None
    temp_d3: float | None
    battery_pct: float | None
    sun_7d: float | None
    qcn_d1: int
    qcn_d2: int
    qcn_d3: int
    read_at: datetime | None = None
    """When the probe took the reading."""

    @property
    def all_training(self) -> bool:
        """Whether no depth has a quality classification yet."""
        return self.qcn_d1 == self.qcn_d2 == self.qcn_d3 == UNCLASSIFIED


def _classified(row: Mapping[str, Any], column: str) -> int:
    value = row.get(column)
    return UNCLASSIFIED if value is None else int(value)


def _timestamp(row: Mapping[str, Any], column: str) -> datetime | None:
    """Read GeoDrops' `date` TIMESTAMP column.

    A naive datetime is taken as UTC; anything else (e.g. a plain DATE after a
    schema change) is unknown.
    """
    value = row.get(column)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def reading_from_row(row: Mapping[str, Any]) -> DeviceReading:
    """Build a reading from a row of GeoDrops' p_sensor_unified table."""
    return DeviceReading(
        device_id=int(row["deviceId"]),
        sync_delay_hours=row.get("miscSensorSyncDelayHour"),
        moisture_index=_classified(row, "moistureIndex"),
        moisture_pct=row.get("moisturePct"),
        moisture_d1=row.get("moisturePctDepth1"),
        moisture_d2=row.get("moisturePctDepth2"),
        moisture_d3=row.get("moisturePctDepth3"),
        temp_surface=row.get("temperatureCSurface"),
        temp_d1=row.get("temperatureCDepth1"),
        temp_d2=row.get("temperatureCDepth2"),
        temp_d3=row.get("temperatureCDepth3"),
        battery_pct=row.get("miscBattPercent"),
        sun_7d=row.get("avg7dSunExposureHourPerDay"),
        qcn_d1=_classified(row, "qcnDepth1"),
        qcn_d2=_classified(row, "qcnDepth2"),
        qcn_d3=_classified(row, "qcnDepth3"),
        read_at=_timestamp(row, "date"),
    )
