"""Read GeoDrops soil-probe data from GeoDrops' public BigQuery table."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import logging
import time
from typing import Any

import aiohttp

from .auth import DEFAULT_TOKEN_URI, ServiceAccountKey, TokenSource
from .bigquery import API_ROOT, BigQuery, QueryParameter, Row
from .exceptions import GeoDropsQueryError, GeoDropsSchemaError
from .models import DeviceReading, reading_from_row

_LOGGER = logging.getLogger(__name__)

TABLE = "geodrops-prod.db_public.p_sensor_unified"
QUERY_TIMEOUT = 60.0
# How long a read of the table's columns is trusted. A column GeoDrops drops
# is noticed straight away (the query fails and the columns are re-read); one
# that comes back is picked up within this long.
SCHEMA_TTL = 6 * 3600.0

# Every column a reading is built from, in SELECT order.
COLUMNS = (
    "deviceId",
    "mfgSn",
    "date",
    "moistureIndex",
    "moisturePct",
    "moisturePctDepth1",
    "moisturePctDepth2",
    "moisturePctDepth3",
    "temperatureCSurface",
    "temperatureCDepth1",
    "temperatureCDepth2",
    "temperatureCDepth3",
    "miscSensorSyncDelayHour",
    "miscBattPercent",
    "avg7dSunExposureHourPerDay",
    "qcnDepth1",
    "qcnDepth2",
    "qcnDepth3",
    "deviceBattMV",
    "deviceRssiDbM",
    "miscIsBattPoorQuality",
)
# Columns the latest-reading query filters, orders or keys rows by: without
# one of these there is nothing sensible to query. Every other column is
# optional; if GeoDrops drops or renames it, readings carry None (or
# UNCLASSIFIED) for the field it fed.
REQUIRED_COLUMNS = ("deviceId", "date", "createdAtOrigin")
# lookup_serial also filters by serial.
_SERIAL_REQUIRED = (*REQUIRED_COLUMNS, "mfgSn")

# BigQuery's message for a column the table doesn't have.
_UNKNOWN_COLUMN = "Unrecognized name"


def _select(columns: Sequence[str]) -> str:
    return "SELECT " + ", ".join(columns)


def build_latest_query(
    device_ids: Iterable[int], lookback_hours: int, columns: Sequence[str] = COLUMNS
) -> str:
    """Build the SQL for each device's latest reading within the lookback window.

    Device ids are interpolated, so each must convert to an int. So are
    `columns`, which must come from COLUMNS.
    """
    ids = ", ".join(str(int(d)) for d in device_ids)
    return (
        f"{_select(columns)}\n"
        f"    FROM `{TABLE}`\n"
        f"    WHERE deviceId IN ({ids})\n"
        f"      AND createdAtOrigin > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
        f"INTERVAL {int(lookback_hours)} HOUR)\n"
        f"    QUALIFY ROW_NUMBER() OVER (PARTITION BY deviceId ORDER BY date DESC) = 1"
    )


def build_serial_lookup_query(lookback_hours: int, columns: Sequence[str] = COLUMNS) -> str:
    """Build the SQL for the latest reading of the probe with serial `@serial`."""
    return (
        f"{_select(columns)}\n"
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

    GeoDrops changes its table's columns now and then. Before querying, the
    client reads which columns exist (a free metadata call) and selects only
    those, so a dropped or renamed column blanks just the values it fed.
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
        # The table's columns as last read: None if they couldn't be read.
        self._live: frozenset[str] | None = None
        # When they were last read (time.monotonic()); None means read them next.
        self._live_read_at: float | None = None
        self._missing: frozenset[str] = frozenset()

    @property
    def project_id(self) -> str:
        """The Google Cloud project queries are billed to."""
        return self._project_id

    @property
    def missing_columns(self) -> frozenset[str]:
        """Columns of COLUMNS the table lacked when its columns were last read.

        Readings carry None (or UNCLASSIFIED) for the fields these feed.
        """
        return self._missing

    async def validate_access(self) -> None:
        """Prove the key and project can query GeoDrops' table.

        Selects a literal, so BigQuery bills close to 0 bytes while still
        checking authentication, the project and table access.
        """
        await self._bigquery.query(f"SELECT 1 FROM `{TABLE}` LIMIT 1", timeout=self._timeout)

    async def table_columns(self) -> list[str]:
        """Return the name of every column in GeoDrops' table.

        A free metadata call, handy for seeing what GeoDrops publishes.
        """
        return await self._bigquery.table_columns(TABLE, timeout=self._timeout)

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
        rows = await self._query(
            lambda columns: build_latest_query(ids, lookback_hours, columns),
            REQUIRED_COLUMNS,
        )
        readings = (reading_from_row(row) for row in rows)
        return {reading.device_id: reading for reading in readings}

    async def lookup_serial(self, serial: str, lookback_hours: int) -> DeviceReading | None:
        """Return the latest reading for the probe with this serial, or None.

        Used to find a probe's device id from the serial shown in the GeoDrops
        app. None means no reading in the last `lookback_hours`.
        """
        rows = await self._query(
            lambda columns: build_serial_lookup_query(lookback_hours, columns),
            _SERIAL_REQUIRED,
            [QueryParameter("serial", "STRING", serial)],
        )
        return reading_from_row(rows[0]) if rows else None

    async def _query(
        self,
        build: Callable[[Sequence[str]], str],
        required: Sequence[str],
        params: Sequence[QueryParameter] = (),
    ) -> list[Row]:
        """Run the query `build` makes from the columns that exist.

        If BigQuery still reports an unknown column (the table changed since
        its columns were read), re-read them and try once more.
        """
        columns = await self._columns(required)
        try:
            return await self._bigquery.query(build(columns), params, timeout=self._timeout)
        except GeoDropsQueryError as err:
            if _UNKNOWN_COLUMN not in str(err):
                raise
            _LOGGER.info("GeoDrops' table changed; re-reading its columns (%s)", err)
        self._live_read_at = None
        columns = await self._columns(required)
        try:
            return await self._bigquery.query(build(columns), params, timeout=self._timeout)
        except GeoDropsQueryError as err:
            if _UNKNOWN_COLUMN not in str(err):
                raise
            raise GeoDropsSchemaError(
                f"GeoDrops' table changed in a way this version can't handle: {err}"
            ) from err

    async def _columns(self, required: Sequence[str]) -> list[str]:
        """Return the columns to select, re-reading the table's if due.

        Raises GeoDropsSchemaError if one of `required` is gone.
        """
        now = time.monotonic()
        if self._live_read_at is None or now - self._live_read_at >= SCHEMA_TTL:
            await self._read_live_columns()
            self._live_read_at = now
        live = self._live
        if live is None:
            # Without the table's columns, select every one as before.
            return list(COLUMNS)
        gone = [c for c in required if c not in live]
        if gone:
            # Check again next time rather than trusting this for SCHEMA_TTL.
            self._live_read_at = None
            raise GeoDropsSchemaError(f"GeoDrops' table no longer has column(s) {', '.join(gone)}")
        return [c for c in COLUMNS if c in live]

    async def _read_live_columns(self) -> None:
        try:
            names = await self._bigquery.table_columns(TABLE, timeout=self._timeout)
        except GeoDropsQueryError as err:
            # E.g. the service account may not read table metadata, while the
            # query itself is still allowed: carry on without the columns.
            _LOGGER.debug("Can't read the columns of %s: %s", TABLE, err)
            names = []
        if not names:
            self._live = None
            return
        self._live = frozenset(names)
        missing = frozenset(COLUMNS) - self._live
        if missing == self._missing:
            return
        if missing:
            _LOGGER.warning(
                "GeoDrops' table no longer has column(s) %s; readings will lack "
                "the values they fed",
                ", ".join(sorted(missing)),
            )
        else:
            _LOGGER.info("GeoDrops' table has all its columns again")
        self._missing = missing
