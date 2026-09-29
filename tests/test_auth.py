import asyncio
import json
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
import jwt
import pytest

from aiogeodrops import (
    GeoDropsAuthError,
    GeoDropsConnectionError,
    GeoDropsCredentialsError,
)
from aiogeodrops.auth import BIGQUERY_SCOPE, DEFAULT_TOKEN_URI, ServiceAccountKey, TokenSource

from .conftest import TOKEN, UNREACHABLE, FakeGoogle


def test_key_from_json_string(key_json: str) -> None:
    key = ServiceAccountKey.from_json(key_json)
    assert key.client_email == "sa@key-project.iam.gserviceaccount.com"
    assert key.private_key_id == "kid-1"


def test_key_from_mapping_without_key_id(key_info: dict[str, str]) -> None:
    info = {k: v for k, v in key_info.items() if k != "private_key_id"}
    assert ServiceAccountKey.from_json(info).private_key_id is None


def test_key_repr_hides_the_private_key(key_json: str) -> None:
    assert "private_key=" not in repr(ServiceAccountKey.from_json(key_json))


def test_tokens_come_from_google_by_default(
    session: aiohttp.ClientSession, key_json: str, rsa_key: rsa.RSAPrivateKey
) -> None:
    tokens = TokenSource(session, ServiceAccountKey.from_json(key_json))
    jwt.decode(
        tokens._assertion(), rsa_key.public_key(), algorithms=["RS256"], audience=DEFAULT_TOKEN_URI
    )


async def test_the_keys_own_token_uri_is_ignored(
    session: aiohttp.ClientSession,
    google: FakeGoogle,
    key_info: dict[str, str],
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    # A crafted key must not be able to send its signed assertion elsewhere.
    google.token_ok()
    key = ServiceAccountKey.from_json({**key_info, "token_uri": google.url("/attacker")})
    await TokenSource(session, key, token_uri=google.url(TOKEN)).token()
    assert google.sent("POST", "/attacker") == []
    (request,) = google.sent("POST", TOKEN)
    jwt.decode(
        request.form["assertion"],
        rsa_key.public_key(),
        algorithms=["RS256"],
        audience=google.url(TOKEN),
    )


@pytest.mark.parametrize("raw", ["not json", '"a string"', "[]", "123", "null", "{}"])
def test_unusable_json_is_a_credentials_error(raw: str) -> None:
    with pytest.raises(GeoDropsCredentialsError):
        ServiceAccountKey.from_json(raw)


def test_garbage_private_key_is_a_credentials_error(key_info: dict[str, str]) -> None:
    with pytest.raises(GeoDropsCredentialsError, match="private_key could not be loaded"):
        ServiceAccountKey.from_json({**key_info, "private_key": "-----BEGIN nonsense"})


def test_non_rsa_private_key_is_a_credentials_error(key_info: dict[str, str]) -> None:
    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(GeoDropsCredentialsError, match="not an RSA key"):
        ServiceAccountKey.from_json({**key_info, "private_key": pem})


def _tokens(session: aiohttp.ClientSession, key_json: str, google: FakeGoogle) -> TokenSource:
    return TokenSource(session, ServiceAccountKey.from_json(key_json), token_uri=google.url(TOKEN))


async def test_token_request_is_a_signed_jwt_bearer_grant(
    session: aiohttp.ClientSession,
    google: FakeGoogle,
    key_json: str,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    google.token_ok()

    assert await _tokens(session, key_json, google).token() == "tok-1"

    (request,) = google.sent("POST", TOKEN)
    assert request.form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assertion = request.form["assertion"]
    assert jwt.get_unverified_header(assertion)["kid"] == "kid-1"
    claims = jwt.decode(
        assertion, rsa_key.public_key(), algorithms=["RS256"], audience=google.url(TOKEN)
    )
    assert claims["iss"] == "sa@key-project.iam.gserviceaccount.com"
    assert claims["scope"] == BIGQUERY_SCOPE
    assert claims["exp"] - claims["iat"] == 3600


async def test_key_without_key_id_sends_no_kid(
    session: aiohttp.ClientSession, google: FakeGoogle, key_info: dict[str, str]
) -> None:
    google.token_ok()
    info = {k: v for k, v in key_info.items() if k != "private_key_id"}
    await TokenSource(
        session, ServiceAccountKey.from_json(info), token_uri=google.url(TOKEN)
    ).token()
    (request,) = google.sent("POST", TOKEN)
    assert "kid" not in jwt.get_unverified_header(request.form["assertion"])


async def test_token_is_cached_until_close_to_expiry(
    session: aiohttp.ClientSession,
    google: FakeGoogle,
    key_json: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr("aiogeodrops.auth.time.monotonic", lambda: now[0])
    google.token_ok("tok-1", expires_in=3600)
    google.token_ok("tok-2", expires_in=3600)
    tokens = _tokens(session, key_json, google)

    assert await tokens.token() == "tok-1"
    now[0] += 3600 - 301
    assert await tokens.token() == "tok-1"
    now[0] += 2  # inside the 5-minute refresh margin
    assert await tokens.token() == "tok-2"


async def test_invalidate_fetches_a_new_token(
    session: aiohttp.ClientSession, google: FakeGoogle, key_json: str
) -> None:
    google.token_ok("tok-1")
    google.token_ok("tok-2")
    tokens = _tokens(session, key_json, google)
    assert await tokens.token() == "tok-1"
    tokens.invalidate()
    assert await tokens.token() == "tok-2"


async def test_concurrent_callers_share_one_token_request(
    session: aiohttp.ClientSession, google: FakeGoogle, key_json: str
) -> None:
    google.token_ok()
    tokens = _tokens(session, key_json, google)
    assert await asyncio.gather(tokens.token(), tokens.token()) == ["tok-1", "tok-1"]
    assert len(google.sent("POST", TOKEN)) == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}),
        (401, {"error": "invalid_client", "error_description": "The OAuth client was not found."}),
        (200, {"token_type": "Bearer"}),
    ],
)
async def test_rejected_key_is_an_auth_error(
    session: aiohttp.ClientSession,
    google: FakeGoogle,
    key_json: str,
    status: int,
    body: dict[str, Any],
) -> None:
    google.reply("POST", TOKEN, status=status, json=body)
    with pytest.raises(GeoDropsAuthError, match=str(status)):
        await _tokens(session, key_json, google).token()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, json.dumps({"error": "backend_error"})),
        (429, json.dumps({"error": "rate_limit_exceeded"})),
        (400, json.dumps({"error": "temporarily_unavailable"})),
        (502, "<html>Bad Gateway</html>"),
    ],
)
async def test_google_outage_is_a_connection_error(
    session: aiohttp.ClientSession,
    google: FakeGoogle,
    key_json: str,
    status: int,
    body: str,
) -> None:
    # The key is fine; asking for a new one would be wrong.
    google.reply("POST", TOKEN, status=status, body=body)
    with pytest.raises(GeoDropsConnectionError, match=str(status)):
        await _tokens(session, key_json, google).token()


async def test_network_failure_is_a_connection_error(
    session: aiohttp.ClientSession, key_info: dict[str, str]
) -> None:
    key = ServiceAccountKey.from_json(key_info)
    tokens = TokenSource(session, key, token_uri=f"{UNREACHABLE}/token")
    with pytest.raises(GeoDropsConnectionError, match="Token request failed"):
        await tokens.token()


async def test_a_redirect_is_not_followed(
    session: aiohttp.ClientSession, google: FakeGoogle, key_json: str
) -> None:
    # Following a 307 would re-send the signed assertion to the new location.
    google.reply("POST", TOKEN, status=307, body="", headers={"Location": google.url("/elsewhere")})
    with pytest.raises(GeoDropsConnectionError, match="307"):
        await _tokens(session, key_json, google).token()
    assert google.sent("POST", "/elsewhere") == []


@pytest.mark.parametrize("expires_in", ["soon", None, "nan", "inf", 0, -60])
async def test_unusable_expires_in_falls_back_to_an_hour(
    session: aiohttp.ClientSession, google: FakeGoogle, key_json: str, expires_in: Any
) -> None:
    google.reply("POST", TOKEN, json={"access_token": "tok-1", "expires_in": expires_in})
    tokens = _tokens(session, key_json, google)
    assert await tokens.token() == "tok-1"
    # Cached: a second fetch would find no reply queued and fail the test.
    assert await tokens.token() == "tok-1"
