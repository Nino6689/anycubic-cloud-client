"""Token state and the config-flow token helpers (PROTOCOL A §2, BEHAVIOUR §5.8).

Nothing here logs a token or puts one in a ``repr``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .regions import BROWSER_USER_AGENT, JWKS_ISSUER, JWKS_URL, AuthMode

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

_LOGGER = logging.getLogger(__name__)

#: ``tokenType`` claim of a slicer access token.
ACCESS_TOKEN_TYPE = "access-token"  # noqa: S105 - a claim value

#: Keys of the 2.x token store the library reads back (PROTOCOL A §2.10).
STORE_TOKEN_KEYS = ("auth_token", "auth_access_token", "device_id")

#: Timeout of the JWKS fetch (BEHAVIOUR §5.8).
JWKS_TIMEOUT = 15.0

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?")
_TOKEN_KEYS = ("access_token", "XX-Token", "token", "auth_token")
_FRAGMENT_RE = re.compile(
    r"""["']?(access_token|XX-Token|token|auth_token)["']?\s*[:=]\s*["']([^"'\s]+)["']"""
)
_WRAPPERS = "\"'`[](){}<> \t\r\n"


# --------------------------------------------------------------------------
# Token state and the token store
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class TokenState:
    """The tokens a client holds, in the 2.x token-store shape (COMPAT §6).

    ``auth_token`` is the user token in use, ``auth_access_token`` the slicer
    access token (international SLICER only).
    """

    auth_token: str | None = None
    auth_access_token: str | None = None
    device_id: str | None = None
    auth_mode: AuthMode = AuthMode.WEB

    def __repr__(self) -> str:
        return (
            f"TokenState(auth_token={_mark(self.auth_token)}, "
            f"auth_access_token={_mark(self.auth_access_token)}, "
            f"device_id={_mark(self.device_id)}, auth_mode={self.auth_mode.name})"
        )

    def to_store(self) -> dict[str, Any]:
        """The token-store dict: exactly the four keys, never ``app_*``."""
        return {
            "auth_token": self.auth_token,
            "auth_access_token": self.auth_access_token,
            "device_id": self.device_id,
            "auth_mode": int(self.auth_mode),
        }


def _mark(value: str | None) -> str:
    return "<set>" if value else "None"


def store_overlay(store: Mapping[str, Any]) -> dict[str, str | None]:
    """The part of a token store that is laid over the entry (PROTOCOL A §2.6.5).

    Only ``auth_token``, ``auth_access_token`` and ``device_id`` are read, and
    only when present; a present ``null`` does overwrite. ``auth_mode`` and the
    ``app_*`` keys are read and ignored.
    """
    overlay: dict[str, str | None] = {}
    for key in STORE_TOKEN_KEYS:
        if key in store:
            value = store[key]
            overlay[key] = value if isinstance(value, str) and value else None
    return overlay


def store_is_stale(store: Mapping[str, Any], entry_token: str, mode: AuthMode) -> bool:
    """Optional hardening (PROTOCOL A §5.3): does the store belong to another token?

    A SLICER store whose ``auth_access_token`` differs from the pasted token,
    or a WEB or ANDROID store whose ``auth_token`` differs from it.
    """
    key = "auth_access_token" if mode is AuthMode.SLICER else "auth_token"
    stored = store.get(key)
    return isinstance(stored, str) and bool(stored) and stored != entry_token


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def _token_from_json(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in _TOKEN_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return _token_from_json(value.get("anycubic_cloud"))


def extract_token(text: str) -> str | None:
    """Pull a token out of pasted text or a Slicer Next config file.

    In order (BEHAVIOUR §5.8 step 1): a JWT anywhere in the text; a JSON
    object's ``access_token``, ``XX-Token``, ``token`` or ``auth_token`` (also
    nested under ``anycubic_cloud``); a ``key: "value"`` fragment with those
    keys; else the text stripped of wrapping brackets and quotes, rejected
    (``None``) if it contains whitespace.
    """
    if (match := _JWT_RE.search(text)) is not None:
        return match.group(0)
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if (token := _token_from_json(decoded)) is not None:
        return token
    if (fragment := _FRAGMENT_RE.search(text)) is not None:
        return fragment.group(2)
    stripped = text.strip(_WRAPPERS)
    if not stripped or any(char.isspace() for char in stripped):
        return None
    return stripped


# --------------------------------------------------------------------------
# Claims (not verified)
# --------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Claims read from a JWT without verifying it (PROTOCOL A §2.1)."""

    exp: int | None = None
    token_type: str | None = None
    issuer: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def seconds_left(self, now: float | None = None) -> float | None:
        """Seconds until ``exp`` (negative when expired); ``None`` without ``exp``."""
        if self.exp is None:
            return None
        return self.exp - (time.time() if now is None else now)

    def is_expired(self, now: float | None = None) -> bool:
        """``exp`` ≤ now. A token without a readable ``exp`` never expires here."""
        left = self.seconds_left(now)
        return left is not None and left <= 0

    @property
    def is_wrong_type(self) -> bool:
        """The ``tokenType`` claim exists and is not ``access-token``."""
        return self.token_type is not None and self.token_type != ACCESS_TOKEN_TYPE


def decode_claims(token: str) -> TokenClaims | None:
    """Decode a JWT's payload without verifying it; ``None`` if it is not a JWT."""
    parts = token.split(".")
    if len(parts) < 2 or not token.startswith("eyJ"):
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    token_type = payload.get("tokenType")
    issuer = payload.get("iss")
    return TokenClaims(
        exp=int(exp)
        if isinstance(exp, int | float) and not isinstance(exp, bool)
        else None,
        token_type=token_type if isinstance(token_type, str) else None,
        issuer=issuer if isinstance(issuer, str) else None,
        raw=payload,
    )


# --------------------------------------------------------------------------
# Signature check against the JWKS
# --------------------------------------------------------------------------


class SignatureStatus(StrEnum):
    """Outcome of :func:`verify_token_signature`."""

    VALID = "valid"
    TRIMMED = "trimmed"
    """Valid after cutting an over-long signature; use the returned token."""
    INVALID = "invalid"
    CORRUPTED = "corrupted"
    """The issuer is present but there is no signature segment."""
    KEYS_UNAVAILABLE = "keys_unavailable"
    """The key set could not be fetched or is empty: accept the token."""
    NOT_CHECKED = "not_checked"
    """Not a JWT from the checked issuer: nothing to verify."""


@dataclass(frozen=True, slots=True, repr=False)
class SignatureCheck:
    status: SignatureStatus
    token: str

    @property
    def acceptable(self) -> bool:
        """Every outcome except INVALID and CORRUPTED lets the login go ahead."""
        return self.status not in (SignatureStatus.INVALID, SignatureStatus.CORRUPTED)

    def __repr__(self) -> str:
        return f"SignatureCheck(status={self.status.value})"


def signature_length(key: RSAPublicKey) -> int:
    """Base64url length of an RS256 signature for ``key``.

    ceil(bits / 8) bytes, which is ceil(bytes x 8 / 6) characters.
    """
    size_bytes = math.ceil(key.key_size / 8)
    return math.ceil(size_bytes * 8 / 6)


def trim_signature(token: str, key: RSAPublicKey) -> str:
    """Cut an over-long signature to the key's length (PROTOCOL A §2.1)."""
    parts = token.split(".")
    if len(parts) != 3:
        return token
    length = signature_length(key)
    if len(parts[2]) <= length:
        return token
    return ".".join((parts[0], parts[1], parts[2][:length]))


def _header_alg(segment: str) -> str | None:
    """The ``alg`` of a JWT header; only RS256 is ever accepted."""
    try:
        header = json.loads(_b64url_decode(segment))
    except (ValueError, binascii.Error):
        return None
    alg = header.get("alg") if isinstance(header, dict) else None
    return alg if isinstance(alg, str) else None


def _verify(token: str, key: RSAPublicKey) -> bool:
    header, payload, sig = token.split(".")
    try:
        key.verify(
            _b64url_decode(sig),
            f"{header}.{payload}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True


def jwks_public_keys(jwks: object) -> list[RSAPublicKey]:
    """RSA public keys from a JWKS document; unusable entries are skipped."""
    keys: list[RSAPublicKey] = []
    if not isinstance(jwks, Mapping) or not isinstance(jwks.get("keys"), list):
        return keys
    for entry in jwks["keys"]:
        if not isinstance(entry, Mapping) or entry.get("kty") != "RSA":
            continue
        try:
            n = int.from_bytes(_b64url_decode(str(entry["n"])), "big")
            e = int.from_bytes(_b64url_decode(str(entry["e"])), "big")
            keys.append(rsa.RSAPublicNumbers(e, n).public_key())
        except (KeyError, ValueError, binascii.Error):
            continue
    return keys


def check_token_signature(token: str, keys: list[RSAPublicKey]) -> SignatureCheck:
    """Verify ``token`` against ``keys`` without network access.

    The header's ``kid`` is not used to pick a key: it may differ from the
    published key's ``kid`` and the key still verifies (PROTOCOL A §2.1).
    """
    claims = decode_claims(token)
    if claims is None or claims.issuer != JWKS_ISSUER:
        return SignatureCheck(SignatureStatus.NOT_CHECKED, token)
    parts = token.split(".")
    if len(parts) != 3 or not parts[2]:
        return SignatureCheck(SignatureStatus.CORRUPTED, token)
    if _header_alg(parts[0]) != "RS256":
        return SignatureCheck(SignatureStatus.INVALID, token)
    if not keys:
        return SignatureCheck(SignatureStatus.KEYS_UNAVAILABLE, token)
    for key in keys:
        if _verify(token, key):
            return SignatureCheck(SignatureStatus.VALID, token)
    for key in keys:
        trimmed = trim_signature(token, key)
        if trimmed != token and _verify(trimmed, key):
            return SignatureCheck(SignatureStatus.TRIMMED, trimmed)
    return SignatureCheck(SignatureStatus.INVALID, token)


async def fetch_jwks(
    session: aiohttp.ClientSession, url: str = JWKS_URL, timeout: float = JWKS_TIMEOUT
) -> list[RSAPublicKey]:
    """Fetch the auth domain's keys with a browser-like user agent.

    Returns an empty list when the keys cannot be fetched.
    """
    try:
        async with session.get(
            url,
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            document = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("JWKS unavailable: %s", type(err).__name__)
        return []
    return jwks_public_keys(document)


async def verify_token_signature(
    session: aiohttp.ClientSession,
    token: str,
    *,
    url: str = JWKS_URL,
    timeout: float = JWKS_TIMEOUT,
) -> SignatureCheck:
    """RS256 pre-check of a pasted token (BEHAVIOUR §5.8 step 3).

    Only tokens whose issuer is ``https://uc.makeronline.com`` are checked.
    Keys unavailable or an empty key set (China publishes one) → accept. An
    over-long signature is cut to the key's length and accepted if it then
    verifies; the returned :attr:`SignatureCheck.token` is the one to use.
    """
    claims = decode_claims(token)
    if claims is None or claims.issuer != JWKS_ISSUER:
        return SignatureCheck(SignatureStatus.NOT_CHECKED, token)
    parts = token.split(".")
    if len(parts) != 3 or not parts[2]:
        return SignatureCheck(SignatureStatus.CORRUPTED, token)
    if _header_alg(parts[0]) != "RS256":
        return SignatureCheck(SignatureStatus.INVALID, token)
    keys = await fetch_jwks(session, url, timeout)
    return check_token_signature(token, keys)
