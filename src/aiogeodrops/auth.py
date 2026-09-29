"""Google service-account authentication (OAuth 2.0 JWT bearer grant)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import time
from typing import Any

import aiohttp
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import jwt

from .exceptions import GeoDropsAuthError, GeoDropsConnectionError, GeoDropsCredentialsError

DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery"
REQUEST_TIMEOUT = 20.0

_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_ASSERTION_LIFETIME = 3600
# Renew a token this many seconds before Google says it expires.
_REFRESH_MARGIN = 300


@dataclass(frozen=True)
class ServiceAccountKey:
    """The parts of a service-account JSON key needed to get access tokens."""

    client_email: str
    private_key: RSAPrivateKey = field(repr=False)
    private_key_id: str | None

    @classmethod
    def from_json(cls, credentials: str | Mapping[str, Any]) -> ServiceAccountKey:
        """Parse a key file's contents (a JSON string or an already-decoded mapping).

        Raises GeoDropsCredentialsError if it is not a usable service-account key.
        The key's own `token_uri` is ignored: a crafted key could otherwise send
        its signed assertion anywhere. Tokens always come from TokenSource's
        `token_uri`.
        """
        if isinstance(credentials, str):
            try:
                info = json.loads(credentials)
            except ValueError as err:
                raise GeoDropsCredentialsError(f"Not valid JSON: {err}") from err
        else:
            info = credentials
        if not isinstance(info, Mapping):
            raise GeoDropsCredentialsError("Expected a JSON object (the service-account key file)")
        missing = [k for k in ("client_email", "private_key") if not isinstance(info.get(k), str)]
        if missing:
            raise GeoDropsCredentialsError(f"Key file is missing {', '.join(missing)}")
        try:
            private_key = load_pem_private_key(info["private_key"].encode(), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as err:
            raise GeoDropsCredentialsError(f"private_key could not be loaded: {err}") from err
        if not isinstance(private_key, RSAPrivateKey):
            raise GeoDropsCredentialsError("private_key is not an RSA key")
        key_id = info.get("private_key_id")
        return cls(
            client_email=info["client_email"],
            private_key=private_key,
            private_key_id=key_id if isinstance(key_id, str) else None,
        )


class TokenSource:
    """Hands out an access token, fetching a new one shortly before it expires."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        key: ServiceAccountKey,
        scope: str = BIGQUERY_SCOPE,
        *,
        token_uri: str = DEFAULT_TOKEN_URI,
    ) -> None:
        """Get tokens for `key` over `session` from Google's `token_uri`."""
        self._session = session
        self._key = key
        self._scope = scope
        self._token_uri = token_uri
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Forget the current token, so the next call fetches a fresh one."""
        self._token = None

    async def token(self) -> str:
        """Return a valid access token."""
        async with self._lock:
            if self._token is None or time.monotonic() >= self._expires_at - _REFRESH_MARGIN:
                token, lifetime = await self._fetch()
                self._token, self._expires_at = token, time.monotonic() + lifetime
            return self._token

    def _assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._key.client_email,
            "scope": self._scope,
            "aud": self._token_uri,
            "iat": now,
            "exp": now + _ASSERTION_LIFETIME,
        }
        headers = {"kid": self._key.private_key_id} if self._key.private_key_id else None
        return jwt.encode(claims, self._key.private_key, algorithm="RS256", headers=headers)

    async def _fetch(self) -> tuple[str, float]:
        form = {"grant_type": _GRANT_TYPE, "assertion": self._assertion()}
        try:
            async with self._session.post(
                self._token_uri,
                data=form,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                # A 307/308 would re-send the signed assertion to the new location.
                allow_redirects=False,
            ) as resp:
                status, text = resp.status, await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GeoDropsConnectionError(f"Token request failed: {err!r}") from err
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        if status == 200 and isinstance(body.get("access_token"), str):
            return body["access_token"], _lifetime(body.get("expires_in"))
        error = body.get("error") or "no error code"
        message = f"{status} {error}: {body.get('error_description') or text[:200]}"
        # Google marks its own outages this way; the key itself is fine then. A
        # redirect (Google sends none) points at a proxy or portal, not the key.
        if (
            status >= 500
            or status == 429
            or 300 <= status < 400
            or error == "temporarily_unavailable"
        ):
            raise GeoDropsConnectionError(message)
        raise GeoDropsAuthError(message)


def _lifetime(expires_in: Any) -> float:
    """Seconds a token lasts, per Google; the assertion's lifetime if unusable."""
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        return _ASSERTION_LIFETIME
    return seconds if math.isfinite(seconds) and seconds > 0 else _ASSERTION_LIFETIME
