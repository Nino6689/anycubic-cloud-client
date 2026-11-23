"""AWS Signature Version 4 (Beta 6, D27).

Written from AWS's public documentation of SigV4 ("Create a signed AWS API
request" and "Authenticating requests: using query parameters"). Two forms
are needed by the Kinesis Video Streams signalling of the cloud camera:

- :func:`sign_request`: the ``Authorization`` header of a request (the two
  JSON ``POST`` calls to the Kinesis Video control plane);
- :func:`presign_url`: a URL whose query carries the signature (the
  signalling websocket, which cannot send headers).

Nothing here does I/O or logs; the secret key never leaves this module except
inside a derived signature.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

ALGORITHM = "AWS4-HMAC-SHA256"
#: SHA-256 of an empty payload.
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    """An access key, its secret and (for temporary keys) the session token.

    The ``repr`` shows none of them.
    """

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    def __repr__(self) -> str:
        return f"AwsCredentials(temporary={self.session_token is not None})"


def _uri_encode(text: str) -> str:
    """RFC 3986 encoding: everything but ``A-Za-z0-9-_.~`` is escaped."""
    return quote(text, safe="-_.~")


def _canonical_uri(path: str) -> str:
    """Each segment encoded once (the path is already decoded here)."""
    if not path:
        return "/"
    return "/".join(_uri_encode(segment) for segment in path.split("/"))


def _canonical_query(pairs: list[tuple[str, str]]) -> str:
    encoded = sorted((_uri_encode(k), _uri_encode(v)) for k, v in pairs)
    return "&".join(f"{k}={v}" for k, v in encoded)


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """``(canonical headers, signed headers)``: names lowercased, values
    trimmed with inner runs of spaces collapsed, sorted by name."""
    folded: dict[str, str] = {}
    for name, value in headers.items():
        key = name.strip().lower()
        clean = " ".join(str(value).strip().split())
        folded[key] = f"{folded[key]},{clean}" if key in folded else clean
    names = sorted(folded)
    canonical = "".join(f"{name}:{folded[name]}\n" for name in names)
    return canonical, ";".join(names)


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    """``kSigning`` for one day, region and service."""
    key = _hmac(f"AWS4{secret}".encode(), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def _stamp(now: datetime | None) -> tuple[str, str]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ"), moment.strftime("%Y%m%d")


def _signature(
    canonical_request: str,
    *,
    amz_date: str,
    scope: str,
    credentials: AwsCredentials,
    region: str,
    service: str,
) -> str:
    digest = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = f"{ALGORITHM}\n{amz_date}\n{scope}\n{digest}"
    key = signing_key(credentials.secret_access_key, amz_date[:8], region, service)
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def canonical_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload_hash: str,
    *,
    extra_query: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """``(canonical request, signed headers)`` of a request."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True) + (extra_query or [])
    canonical_headers, signed = _canonical_headers(headers)
    text = "\n".join(
        (
            method.upper(),
            _canonical_uri(parts.path),
            _canonical_query(query),
            canonical_headers,
            signed,
            payload_hash,
        )
    )
    return text, signed


def sign_request(
    method: str,
    url: str,
    *,
    region: str,
    service: str,
    credentials: AwsCredentials,
    headers: Mapping[str, str] | None = None,
    body: bytes = b"",
    now: datetime | None = None,
) -> dict[str, str]:
    """The headers to send: the caller's, ``Host``, ``X-Amz-Date``, the
    session token when there is one, and ``Authorization``.

    Every header passed is signed, as are the ones added here.
    """
    amz_date, date = _stamp(now)
    out = dict(headers or {})
    if not any(name.lower() == "host" for name in out):
        out["Host"] = urlsplit(url).netloc
    out["X-Amz-Date"] = amz_date
    if credentials.session_token:
        out["X-Amz-Security-Token"] = credentials.session_token
    payload_hash = hashlib.sha256(body).hexdigest()
    request, signed = canonical_request(method, url, out, payload_hash)
    scope = f"{date}/{region}/{service}/aws4_request"
    signature = _signature(
        request,
        amz_date=amz_date,
        scope=scope,
        credentials=credentials,
        region=region,
        service=service,
    )
    out["Authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return out


def presign_url(
    url: str,
    *,
    region: str,
    service: str,
    credentials: AwsCredentials,
    expires: int,
    method: str = "GET",
    payload_hash: str = EMPTY_PAYLOAD_HASH,
    now: datetime | None = None,
) -> str:
    """``url`` with the SigV4 query parameters added; only ``host`` is signed.

    The session token, when there is one, joins the query *before* signing.
    """
    amz_date, date = _stamp(now)
    parts = urlsplit(url)
    scope = f"{date}/{region}/{service}/aws4_request"
    added = [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential", f"{credentials.access_key_id}/{scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(int(expires))),
        ("X-Amz-SignedHeaders", "host"),
    ]
    if credentials.session_token:
        added.append(("X-Amz-Security-Token", credentials.session_token))
    request, _signed = canonical_request(
        method, url, {"host": parts.netloc}, payload_hash, extra_query=added
    )
    signature = _signature(
        request,
        amz_date=amz_date,
        scope=scope,
        credentials=credentials,
        region=region,
        service=service,
    )
    query = parse_qsl(parts.query, keep_blank_values=True) + added
    query.append(("X-Amz-Signature", signature))
    path = parts.path or "/"
    return f"{parts.scheme}://{parts.netloc}{path}?{_canonical_query(query)}"
