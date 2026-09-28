"""Request signing: nonces, the signature and the header set (PROTOCOL A §3)."""

from __future__ import annotations

import hashlib
import struct
import time
import uuid

from .regions import BROWSER_USER_AGENT, AuthMode, NonceStyle, Region

#: Alphabet of the packed (Android) nonce: 0-9, a-z, A-Z.
PACKED_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
#: The packed nonce masks with 61, not modulo 62 (PROTOCOL A §3.3).
PACKED_MASK = 61


def md5_hex(text: str) -> str:
    """Lowercase hex MD5 of UTF-8 text."""
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def uuid_nonce(value: uuid.UUID | None = None) -> str:
    """WEB and SLICER nonce: a version-1 UUID in its 36-character text form."""
    return str(value if value is not None else uuid.uuid1())


def _encode_packed(number: int) -> str:
    chars = ["0"] * 11
    for position in range(10, -1, -1):
        if number in (0, -1):
            break
        chars[position] = PACKED_ALPHABET[number & PACKED_MASK]
        number >>= 6  # arithmetic shift: Python keeps the sign
    return "".join(chars)


def packed_nonce(value: uuid.UUID | None = None) -> str:
    """ANDROID nonce: 22 characters packed from a version-1 UUID.

    The UUID's bytes are read as two signed 64-bit big-endian integers and
    each is encoded into 11 characters (PROTOCOL A §3.3).
    """
    raw = (value if value is not None else uuid.uuid1()).bytes
    high, low = struct.unpack(">qq", raw)
    return _encode_packed(high) + _encode_packed(low)


def make_nonce(style: NonceStyle) -> str:
    return packed_nonce() if style is NonceStyle.PACKED else uuid_nonce()


def signature(
    app_id: str, timestamp: str, app_version: str, app_secret: str, nonce: str
) -> str:
    """``md5_hex(app_id ‖ timestamp ‖ version ‖ app_secret ‖ nonce ‖ app_id)``.

    The method, path, query and body are not signed (PROTOCOL A §3.4).
    """
    return md5_hex(app_id + timestamp + app_version + app_secret + nonce + app_id)


def timestamp_ms() -> str:
    """Unix time in milliseconds, as a decimal string."""
    return str(int(time.time() * 1000))


def make_android_device_id() -> str:
    """Fallback Android device id: 33 lowercase hex characters.

    The first 33 characters of two version-1 UUIDs written without hyphens
    and joined (PROTOCOL A §2.4).
    """
    return (uuid.uuid1().hex + uuid.uuid1().hex)[:33]


def build_headers(
    *,
    app_id: str,
    app_secret: str,
    profile_mode: AuthMode,
    current_mode: AuthMode,
    region: Region,
    token: str | None,
    device_id: str | None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> dict[str, str]:
    """The signed header set, in the order 2.x builds it (PROTOCOL A §3.2).

    ``profile_mode`` sets the per-mode values (device type, ``Xx-Is-Cn``,
    version, nonce style) and ``current_mode`` decides the browser
    ``User-Agent`` and ``Origin``: after the web fallback the two differ and
    the mixed set is sent, as 2.x does (Q1 in docs/QUESTIONS.md).

    ``token`` is omitted (no ``XX-Token``) for the token exchange only.
    """
    profile = profile_mode.profile
    stamp = timestamp if timestamp is not None else timestamp_ms()
    once = nonce if nonce is not None else make_nonce(profile.nonce_style)
    headers = {
        "Xx-Device-Type": profile.device_type,
        "Xx-Is-Cn": profile.is_cn,
        "Xx-Nonce": once,
        "Xx-Signature": signature(app_id, stamp, profile.version, app_secret, once),
        "Xx-Timestamp": stamp,
        "Xx-Version": profile.version,
        "Content-Type": "application/json",
    }
    if profile_mode is AuthMode.ANDROID and device_id:
        headers["XX-Device-Id"] = device_id
    if token is not None:
        headers["XX-Token"] = token
    headers["XX-LANGUAGE"] = "US"
    if current_mode is AuthMode.WEB:
        headers["User-Agent"] = BROWSER_USER_AGENT
        headers["Origin"] = region.endpoints.origin
    return headers
