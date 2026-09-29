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

## Errors

All errors derive from `GeoDropsError`:

| Exception | Meaning | What to do |
| --- | --- | --- |
| `GeoDropsCredentialsError` | The key isn't a usable service-account key | Fix the key; raised by the constructor |
| `GeoDropsAuthError` | Google rejected the key (deleted, revoked, account disabled) | Create a new key |
| `GeoDropsAccessDeniedError` | 403: missing BigQuery Job User role, or BigQuery API not enabled | Fix it in Google Cloud; then retry |
| `GeoDropsQueryError` | BigQuery rejected the query (parent of `GeoDropsAccessDeniedError`) | Check the project id |
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
