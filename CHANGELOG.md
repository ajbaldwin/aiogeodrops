# Changelog

## 0.3.0

New reading fields, read from three more columns. They have defaults, so existing construction keeps working.

- `qcn` (`qcn`): GeoDrops' overall quality classification, `UNCLASSIFIED` when unknown, like the per-depth `qcn_d1`..`qcn_d3`.
- `irrigation_confidence_pct` (`irrConfidencePct`): GeoDrops' irrigation confidence, in percent.
- `next_action` (`nextAction`): GeoDrops' status codes for the probe, as a `frozenset[str]` parsed from the comma-separated column (e.g. `{"ATT_DW_NEW"}`). Empty when GeoDrops lists none, `None` when the column is missing.

## 0.2.0

Survives GeoDrops changing its table's columns. No API changes are needed by callers.

- Before querying, the client reads which columns GeoDrops' table has. This is a free metadata call, made at most every 6 hours. It then selects only those columns, so a dropped or renamed column leaves just the fields it fed as `None` (or `UNCLASSIFIED`) instead of failing every query. It logs one warning when columns go missing and an info line when they return.
- If BigQuery still reports `Unrecognized name`, the client re-reads the columns and retries once.
- New `GeoDropsSchemaError` (a `GeoDropsQueryError`) when a column the query can't run without is gone: `deviceId`, `date` or `createdAtOrigin`, plus `mfgSn` for `lookup_serial`. It is also raised when the retry still hits an unknown column.
- If the table's columns can't be read (for example, no permission for table metadata), every column is selected, as in 0.1.x.
- New `GeoDropsClient.missing_columns`: the columns the table lacked at the last check.
- New `GeoDropsClient.table_columns()`: every column in GeoDrops' table.
- New reading fields, read from three more columns: `battery_mv` (`deviceBattMV`), `rssi_dbm` (`deviceRssiDbM`) and `battery_poor` (`miscIsBattPoorQuality`). They default to `None`, so existing positional and keyword construction keeps working.
- Readings tolerate a column changing type. Numbers sent as text are read. Values that aren't finite numbers become `None` (or `UNCLASSIFIED`) instead of raising. Numeric fields are now always `float`.

## 0.1.1

Security hardening; no API changes for callers using the defaults.

- A key file's `token_uri` is ignored. Tokens always come from Google's endpoint (or the new `token_uri=` argument), so a crafted key cannot send its signed assertion elsewhere.
- Redirects are not followed, so neither the signed assertion nor the access token can be forwarded to another host. A redirect from the token endpoint is a `GeoDropsConnectionError`.
- A `/` in the project id is escaped instead of changing the request path.
- A malformed `expires_in` from Google no longer raises `ValueError`; the token is kept for an hour.
- The private key is left out of `repr()`.
- CI: actions pinned to commit SHAs, `build` pinned, checkout no longer keeps the GitHub token, workflows default to read-only, Dependabot keeps the pins current.

## 0.1.0

- First release: `GeoDropsClient` with `validate_access`, `fetch_latest` and `lookup_serial`, service-account auth over the caller's aiohttp session, and typed errors.
