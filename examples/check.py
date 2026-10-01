"""Check a service-account key against GeoDrops' table, end to end.

    python examples/check.py KEY_FILE PROJECT_ID [SERIAL ...]

Validates access, lists the table's columns, then looks up each serial and
fetches its latest reading.
"""

import asyncio
from pathlib import Path
import sys

import aiohttp

from aiogeodrops import GeoDropsClient, GeoDropsError


async def main(key: str, project_id: str, serials: list[str]) -> int:
    """Run the checks and print what comes back."""
    async with aiohttp.ClientSession() as session:
        client = GeoDropsClient(session, project_id, key)
        try:
            await client.validate_access()
            print("access: ok")
            print("columns:", ", ".join(await client.table_columns()))
            ids = []
            for serial in serials:
                reading = await client.lookup_serial(serial, 12)
                print(f"{serial}: device_id={reading.device_id if reading else None}")
                if reading:
                    ids.append(reading.device_id)
            for device_id, reading in (await client.fetch_latest(ids, 12)).items():
                print(f"{device_id}: {reading}")
            print("missing columns:", ", ".join(sorted(client.missing_columns)) or "none")
        except GeoDropsError as err:
            print(f"{type(err).__name__}: {err}")
            return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    key = Path(sys.argv[1]).read_text()
    sys.exit(asyncio.run(main(key, sys.argv[2], sys.argv[3:])))
