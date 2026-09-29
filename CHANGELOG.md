# Changelog

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
