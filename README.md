# aiogeodrops

Async Python client for [GeoDrops](https://geodrops.io/) soil-probe readings.

GeoDrops publishes every probe's readings to a public Google BigQuery table.
This library queries it over BigQuery's REST API with a Google service
account. It is built on `aiohttp` and `PyJWT` only (no Google Cloud SDK, no
gRPC), takes your `aiohttp.ClientSession`, and is fully typed.

It is the library behind the
[GeoDrops Home Assistant integration](https://github.com/ajbaldwin/ha-geodrops-hacs).

## Install

```bash
pip install aiogeodrops
```

## Prerequisites

A Google Cloud project of your own (queries run and are billed there, well
within BigQuery's free tier for a handful of probes), with the BigQuery API
enabled, and a service account that has **BigQuery Job User** on that
project. Download a JSON key for the service account.

## Usage

```python
import asyncio
from pathlib import Path

import aiohttp

from aiogeodrops import GeoDropsClient


async def main() -> None:
    key = Path("service-account.json").read_text()
    async with aiohttp.ClientSession() as session:
        client = GeoDropsClient(session, "my-gcp-project", key)

        await client.validate_access()

        # Find a probe's device id from the serial shown in the GeoDrops app.
        reading = await client.lookup_serial("AAA111", lookback_hours=12)
        if reading is None:
            print("No reading in the last 12 hours")
            return

        latest = await client.fetch_latest([reading.device_id], lookback_hours=12)
        probe = latest[reading.device_id]
        print(probe.moisture_pct, probe.temp_surface, probe.read_at)


asyncio.run(main())
```

`fetch_latest` makes one query for all the device ids you pass. Each query
gives up after 60 seconds (`query_timeout=` to change).

### When GeoDrops changes its table

GeoDrops' table is theirs to change, and columns have been renamed or dropped
before. Before querying, the client reads which columns exist (a free metadata
call, repeated at most every 6 hours) and selects only those:

- A missing column leaves the reading fields it fed as `None` (or
  `UNCLASSIFIED`). `client.missing_columns` lists them, and one warning is
  logged.
- If a query still names an unknown column, the columns are re-read and the
  query retried once.
- `GeoDropsSchemaError` is raised only when a column the query can't run
  without is gone: `deviceId`, `date`, `createdAtOrigin`, and `mfgSn` for
  `lookup_serial`.

`await client.table_columns()` lists every column GeoDrops publishes.

## Errors

All errors derive from `GeoDropsError`:

| Exception | Meaning | What to do |
| --- | --- | --- |
| `GeoDropsCredentialsError` | The key isn't a usable service-account key | Fix the key; raised by the constructor |
| `GeoDropsAuthError` | Google rejected the key (deleted, revoked, account disabled) | Create a new key |
| `GeoDropsAccessDeniedError` | 403: missing BigQuery Job User role, or BigQuery API not enabled | Fix it in Google Cloud; then retry |
| `GeoDropsSchemaError` | GeoDrops' table lost a column the query can't run without | Update aiogeodrops |
| `GeoDropsQueryError` | BigQuery rejected the query (parent of `GeoDropsAccessDeniedError` and `GeoDropsSchemaError`) | Check the project id |
| `GeoDropsConnectionError` | Network error, timeout, Google outage, rate or quota limit | Retry later |

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest --cov=aiogeodrops
```

Releases are published to PyPI by GitHub Actions when a GitHub release is
published, using PyPI trusted publishing (no stored token) with attestations.

## License

[MIT](LICENSE)
