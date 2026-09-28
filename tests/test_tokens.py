"""Token state, extraction, claims and the JWKS signature pre-check."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import aiohttp
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from anycubic_cloud_client import (
    AuthMode,
    SignatureStatus,
    TokenState,
    decode_claims,
    extract_token,
    store_is_stale,
    trim_signature,
    verify_token_signature,
)
from anycubic_cloud_client.tokens import (
    check_token_signature,
    fetch_jwks,
    jwks_public_keys,
    signature_length,
    store_overlay,
)

from .conftest import FakeResponse, FakeSession, aiohttp_session

ISSUER = "https://uc.makeronline.com"
JWKS = "https://uc.makeronline.com/.well-known/jwks"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_jwt(claims: dict[str, Any], key: rsa.RSAPrivateKey | None = None) -> str:
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "other"}).encode())
    payload = b64url(json.dumps(claims).encode())
    if key is None:
        return f"{header}.{payload}.c2ln"
    sig = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64url(sig)}"


def jwk(key: rsa.RSAPrivateKey) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": "published",
        "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": b64url(numbers.e.to_bytes(3, "big")),
    }


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# -- token state and store --------------------------------------------------------


def test_token_state_export_and_repr() -> None:
    state = TokenState("user-tok", "access-tok", "zz-9", AuthMode.SLICER)
    assert state.to_store() == {
        "auth_token": "user-tok",
        "auth_access_token": "access-tok",
        "device_id": "zz-9",
        "auth_mode": 3,
    }
    text = repr(state)
    assert "user-tok" not in text and "access-tok" not in text and "zz-9" not in text
    assert "SLICER" in text
    assert "None" in repr(TokenState())


def test_store_overlay_reads_only_token_keys() -> None:
    store = {
        "auth_token": "u",
        "auth_access_token": None,
        "auth_mode": 1,
        "app_id": "x",
        "app_secret": "y",
        "app_version": "z",
        "app_client_id": "w",
    }
    assert store_overlay(store) == {"auth_token": "u", "auth_access_token": None}
    assert store_overlay({}) == {}
    assert store_overlay({"device_id": ""}) == {"device_id": None}


def test_store_is_stale() -> None:
    assert store_is_stale({"auth_access_token": "old"}, "new", AuthMode.SLICER)
    assert not store_is_stale({"auth_access_token": "new"}, "new", AuthMode.SLICER)
    assert store_is_stale({"auth_token": "old"}, "new", AuthMode.WEB)
    assert not store_is_stale({"auth_token": None}, "new", AuthMode.ANDROID)


# -- extraction -------------------------------------------------------------------


def test_extract_jwt_anywhere() -> None:
    token = make_jwt({"iss": ISSUER})
    assert extract_token(f'garbage "{token}" more') == token
    two_part = token.rsplit(".", 1)[0]
    assert extract_token(f"prefix {two_part}") == two_part


def test_extract_from_slicer_config() -> None:
    config = json.dumps({"anycubic_cloud": {"access_token": "opaque-token-1"}})
    assert extract_token(config) == "opaque-token-1"
    assert extract_token(json.dumps({"XX-Token": " opaque-2 "})) == "opaque-2"
    assert extract_token(json.dumps({"auth_token": "opaque-3"})) == "opaque-3"
    assert extract_token(json.dumps({"other": 1, "token": ""})) is None


def test_extract_fragment_and_plain() -> None:
    assert extract_token('XX-Token: "abc123def"') == "abc123def"
    assert extract_token("  'plain-token'  ") == "plain-token"
    assert extract_token("[token-in-brackets]") == "token-in-brackets"
    assert extract_token("two words") is None
    assert extract_token('  ""  ') is None
    assert extract_token(json.dumps({"nothing": "here"})) is None


# -- claims ---------------------------------------------------------------------------


def test_decode_claims() -> None:
    now = time.time()
    token = make_jwt({"exp": int(now) + 100, "tokenType": "access-token", "iss": ISSUER})
    claims = decode_claims(token)
    assert claims is not None
    assert claims.issuer == ISSUER
    assert not claims.is_wrong_type
    assert not claims.is_expired(now)
    left = claims.seconds_left(now)
    assert left is not None and 99 <= left <= 100
    expired = decode_claims(make_jwt({"exp": int(now) - 1, "tokenType": "user"}))
    assert expired is not None and expired.is_expired() and expired.is_wrong_type
    no_exp = decode_claims(make_jwt({"exp": "soon", "iss": 5}))
    assert no_exp is not None
    assert no_exp.exp is None and no_exp.issuer is None and not no_exp.is_expired()
    assert no_exp.seconds_left() is None


@pytest.mark.parametrize("token", ["opaque", "eyJ", "eyJhbGci.!!!.x", "eyJ." + b64url(b"[1]")])
def test_decode_claims_not_a_jwt(token: str) -> None:
    assert decode_claims(token) is None


# -- signature check -----------------------------------------------------------------


def test_valid_signature(key: rsa.RSAPrivateKey) -> None:
    token = make_jwt({"iss": ISSUER}, key)
    keys = jwks_public_keys({"keys": [jwk(key)]})
    result = check_token_signature(token, keys)
    assert result.status is SignatureStatus.VALID
    assert result.acceptable
    assert result.token == token
    assert "valid" in repr(result) and token not in repr(result)


def test_over_long_signature_is_trimmed(key: rsa.RSAPrivateKey) -> None:
    token = make_jwt({"iss": ISSUER}, key)
    public = key.public_key()
    assert signature_length(public) == 342  # 256 bytes -> ceil(2048 / 6)
    polluted = token + "AAAA"
    assert trim_signature(polluted, public) == token
    assert trim_signature(token, public) == token
    assert trim_signature("a.b", public) == "a.b"
    result = check_token_signature(polluted, [public])
    assert result.status is SignatureStatus.TRIMMED
    assert result.token == token


def test_invalid_signature(key: rsa.RSAPrivateKey) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = make_jwt({"iss": ISSUER}, other)
    result = check_token_signature(token, [key.public_key()])
    assert result.status is SignatureStatus.INVALID
    assert not result.acceptable


def test_not_checked_corrupted_and_no_keys(key: rsa.RSAPrivateKey) -> None:
    assert check_token_signature("opaque", []).status is SignatureStatus.NOT_CHECKED
    other_issuer = make_jwt({"iss": "https://elsewhere.invalid"}, key)
    assert check_token_signature(other_issuer, []).status is SignatureStatus.NOT_CHECKED
    corrupted = make_jwt({"iss": ISSUER}).rsplit(".", 1)[0]
    assert check_token_signature(corrupted, []).status is SignatureStatus.CORRUPTED
    assert check_token_signature(corrupted + ".", []).status is SignatureStatus.CORRUPTED
    token = make_jwt({"iss": ISSUER}, key)
    result = check_token_signature(token, [])
    assert result.status is SignatureStatus.KEYS_UNAVAILABLE
    assert result.acceptable


def test_jwks_parsing_skips_bad_entries(key: rsa.RSAPrivateKey) -> None:
    assert jwks_public_keys(None) == []
    assert jwks_public_keys({"keys": "x"}) == []
    keys = jwks_public_keys(
        {"keys": [jwk(key), {"kty": "EC"}, {"kty": "RSA"}, "junk", {"kty": "RSA", "n": "!", "e": "AQAB"}]}
    )
    assert len(keys) == 1


async def test_verify_fetches_jwks_with_browser_agent(key: rsa.RSAPrivateKey) -> None:
    http = FakeSession()
    http.add("GET", JWKS, {"keys": [jwk(key)]})
    token = make_jwt({"iss": ISSUER}, key)
    result = await verify_token_signature(aiohttp_session(http), token)
    assert result.status is SignatureStatus.VALID
    call = http.calls[0]
    assert call.headers["User-Agent"].startswith("Mozilla/5.0")
    assert call.kwargs["timeout"].total == 15


async def test_verify_accepts_when_keys_unavailable(key: rsa.RSAPrivateKey) -> None:
    http = FakeSession()
    http.add("GET", JWKS, aiohttp.ClientConnectionError("down"))
    token = make_jwt({"iss": ISSUER}, key)
    result = await verify_token_signature(aiohttp_session(http), token)
    assert result.status is SignatureStatus.KEYS_UNAVAILABLE
    http.routes.clear()
    http.add("GET", JWKS, {"keys": []})
    assert (await verify_token_signature(aiohttp_session(http), token)).acceptable
    http.routes.clear()
    http.add("GET", JWKS, FakeResponse(json_error=ValueError("html")))
    assert await fetch_jwks(aiohttp_session(http)) == []


async def test_verify_without_network_for_other_tokens() -> None:
    http = FakeSession()
    session = aiohttp_session(http)
    assert (await verify_token_signature(session, "opaque")).status is SignatureStatus.NOT_CHECKED
    corrupted = make_jwt({"iss": ISSUER}).rsplit(".", 1)[0]
    assert (await verify_token_signature(session, corrupted)).status is SignatureStatus.CORRUPTED
    assert http.calls == []
