"""A GeoDrops probe reading."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import math
from typing import Any

# GeoDrops' value for a quality/moisture-index column with no classification
# yet (the probe is still training).
UNCLASSIFIED = -1


@dataclass(frozen=True)
class DeviceReading:
    """One probe's latest reading.

    Numeric fields are None when GeoDrops has no value for them, or the
    column is missing or holds something that isn't a number. The quality
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
    battery_mv: float | None = None
    """Battery voltage, in millivolts."""
    rssi_dbm: float | None = None
    """Radio signal strength, in dBm."""
    battery_poor: bool | None = None
    """Whether GeoDrops flags the battery as poor quality."""
    qcn: int = UNCLASSIFIED
    """GeoDrops' overall quality classification for the reading."""
    irrigation_confidence_pct: float | None = None
    """GeoDrops' irrigation confidence, in percent."""
    next_action: frozenset[str] | None = None
    """GeoDrops' status codes for the probe (e.g. "ATT_DW_NEW").

    Empty when GeoDrops lists none; None when the column is missing.
    """

    @property
    def all_training(self) -> bool:
        """Whether no depth has a quality classification yet."""
        return self.qcn_d1 == self.qcn_d2 == self.qcn_d3 == UNCLASSIFIED


def _number(row: Mapping[str, Any], column: str) -> float | None:
    """Read a numeric column; None if it is missing or not a finite number.

    A number sent as text (e.g. after GeoDrops changes a column to STRING) is
    still read.
    """
    value = row.get(column)
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _classified(row: Mapping[str, Any], column: str) -> int:
    """Read a classification column; UNCLASSIFIED unless it is a whole number."""
    value = _number(row, column)
    return UNCLASSIFIED if value is None or not value.is_integer() else int(value)


def _flag(row: Mapping[str, Any], column: str) -> bool | None:
    """Read a BOOL column; None if it is missing or not a boolean."""
    value = row.get(column)
    if isinstance(value, bool):
        return value
    # e.g. if GeoDrops changed the column to STRING
    return {"true": True, "false": False}.get(value) if isinstance(value, str) else None


def _codes(row: Mapping[str, Any], column: str) -> frozenset[str] | None:
    """Read a comma-separated code list such as "DW_M_LOW12,CHK_M_HWR,".

    NULL or blank is no codes; None only when the column is missing or holds
    something other than text.
    """
    if column not in row:
        return None
    value = row[column]
    if value is None:
        return frozenset()
    if not isinstance(value, str):
        return None
    return frozenset(code for part in value.split(",") if (code := part.strip()))


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
        sync_delay_hours=_number(row, "miscSensorSyncDelayHour"),
        moisture_index=_classified(row, "moistureIndex"),
        moisture_pct=_number(row, "moisturePct"),
        moisture_d1=_number(row, "moisturePctDepth1"),
        moisture_d2=_number(row, "moisturePctDepth2"),
        moisture_d3=_number(row, "moisturePctDepth3"),
        temp_surface=_number(row, "temperatureCSurface"),
        temp_d1=_number(row, "temperatureCDepth1"),
        temp_d2=_number(row, "temperatureCDepth2"),
        temp_d3=_number(row, "temperatureCDepth3"),
        battery_pct=_number(row, "miscBattPercent"),
        sun_7d=_number(row, "avg7dSunExposureHourPerDay"),
        qcn_d1=_classified(row, "qcnDepth1"),
        qcn_d2=_classified(row, "qcnDepth2"),
        qcn_d3=_classified(row, "qcnDepth3"),
        read_at=_timestamp(row, "date"),
        battery_mv=_number(row, "deviceBattMV"),
        rssi_dbm=_number(row, "deviceRssiDbM"),
        battery_poor=_flag(row, "miscIsBattPoorQuality"),
        qcn=_classified(row, "qcn"),
        irrigation_confidence_pct=_number(row, "irrConfidencePct"),
        next_action=_codes(row, "nextAction"),
    )
