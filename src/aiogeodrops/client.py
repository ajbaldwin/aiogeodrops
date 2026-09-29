"""Read GeoDrops soil-probe data from GeoDrops' public BigQuery table."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import aiohttp

from .auth import DEFAULT_TOKEN_URI, ServiceAccountKey, TokenSource
from .bigquery import API_ROOT, BigQuery, QueryParameter
from .models import DeviceReading, reading_from_row

TABLE = "geodrops-prod.db_public.p_sensor_unified"
QUERY_TIMEOUT = 60.0

_COLUMNS = """
      deviceId, mfgSn, date, moistureIndex, moisturePct,
      moisturePctDepth1, moisturePctDepth2, moisturePctDepth3,
      temperatureCSurface, temperatureCDepth1, temperatureCDepth2, temperatureCDepth3,
      miscSensorSyncDelayHour, miscBattPercent, avg7dSunExposureHourPerDay,
      qcnDepth1, qcnDepth2, qcnDepth3""".rstrip()


def build_latest_query(device_ids: Iterable[int], lookback_hours: int) -> str:
    """Build the SQL for each device's latest reading within the lookback window.

    Device ids are interpolated, so each must convert to an int.
    """
    ids = ", ".join(str(int(d)) for d in device_ids)
    return (
        f"SELECT{_COLUMNS}\n"
        f"    FROM `{TABLE}`\n"
        f"    WHERE deviceId IN ({ids})\n"
        f"      AND createdAtOrigin > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
        f"INTERVAL {int(lookback_hours)} HOUR)\n"
        f"    QUALIFY ROW_NUMBER() OVER (PARTITION BY deviceId ORDER BY date DESC) = 1"
    )


def build_serial_lookup_query(lookback_hours: int) -> str:
    """Build the SQL for the latest reading of the probe with serial `@serial`."""
    return (
        f"SELECT{_COLUMNS}\n"
        f"    FROM `{TABLE}`\n"
        f"    WHERE mfgSn = @serial\n"
        f"      AND createdAtOrigin > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
        f"INTERVAL {int(lookback_hours)} HOUR)\n"
        f"    ORDER BY date DESC\n"
        f"    LIMIT 1"
    )


class GeoDropsClient:
    """Reads GeoDrops probe data with a Google service account.

    Queries run in, and are billed to, `project_id`: the caller's own Google
    Cloud project, not GeoDrops'. The service account needs the BigQuery Job
    User role there.
    """

    def __init__(  # noqa: PLR0913 - the extras are keyword-only
        self,
        session: aiohttp.ClientSession,
        project_id: str,
        credentials: str | Mapping[str, Any],
        *,
        query_timeout: float = QUERY_TIMEOUT,
        api_root: str = API_ROOT,
        token_uri: str = DEFAULT_TOKEN_URI,
    ) -> None:
        """Create a client using the caller's aiohttp session.

        `credentials` is the service-account JSON key file's contents. Raises
        GeoDropsCredentialsError if it is not a usable key.
        """
        key = ServiceAccountKey.from_json(credentials)
        self._project_id = project_id
        self._timeout = query_timeout
        tokens = TokenSource(session, key, token_uri=token_uri)
        self._bigquery = BigQuery(session, tokens, project_id, api_root=api_root)

    @property
    def project_id(self) -> str:
        """The Google Cloud project queries are billed to."""
        return self._project_id

    async def validate_access(self) -> None:
        """Prove the key and project can query GeoDrops' table.

        Selects a literal, so BigQuery bills close to 0 bytes while still
        checking authentication, the project and table access.
        """
        await self._bigquery.query(f"SELECT 1 FROM `{TABLE}` LIMIT 1", timeout=self._timeout)

    async def fetch_latest(
        self, device_ids: Iterable[int], lookback_hours: int
    ) -> dict[int, DeviceReading]:
        """Return each device's latest reading, keyed by device id.

        A device with no reading in the last `lookback_hours` is left out.
        Makes no query when `device_ids` is empty.
        """
        ids = [int(d) for d in device_ids]
        if not ids:
            return {}
        rows = await self._bigquery.query(
            build_latest_query(ids, lookback_hours), timeout=self._timeout
        )
        readings = (reading_from_row(row) for row in rows)
        return {reading.device_id: reading for reading in readings}

    async def lookup_serial(self, serial: str, lookback_hours: int) -> DeviceReading | None:
        """Return the latest reading for the probe with this serial, or None.

        Used to find a probe's device id from the serial shown in the GeoDrops
        app. None means no reading in the last `lookback_hours`.
        """
        rows = await self._bigquery.query(
            build_serial_lookup_query(lookback_hours),
            [QueryParameter("serial", "STRING", serial)],
            timeout=self._timeout,
        )
        return reading_from_row(rows[0]) if rows else None
