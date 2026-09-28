"""CloudSecrets, regions, modes and request signing (PROTOCOL A §0-§3)."""

from __future__ import annotations

import hashlib
import re
import uuid

import pytest

from anycubic_cloud_client import AuthMode, CloudSecrets, Region, SecretsInvalidError
from anycubic_cloud_client.regions import NonceStyle
from anycubic_cloud_client.signing import (
    build_headers,
    make_android_device_id,
    make_nonce,
    md5_hex,
    packed_nonce,
    signature,
    timestamp_ms,
    uuid_nonce,
)

from .conftest import FAKE_APP_ID, FAKE_APP_SECRET, Material

# -- CloudSecrets -------------------------------------------------------------


def _kwargs(material: Material) -> dict[str, object]:
    return {
        "app_id": FAKE_APP_ID,
        "app_secret": FAKE_APP_SECRET,
        "client_id_web": "FAKEWEBCLIENTID00001",
        "client_id_app": "FAKEAPPCLIENTID00002",
        "mqtt_ca_pem": material.ca_pem,
        "mqtt_client_cert_pem": material.cert_pem,
        "mqtt_client_key_pem": material.key_pem,
    }


def test_secrets_repr_shows_no_value(secrets: CloudSecrets) -> None:
    text = repr(secrets)
    assert FAKE_APP_ID not in text
    assert FAKE_APP_SECRET not in text
    assert "BEGIN" not in text
    assert "app_id=<set>" in text
    assert str(secrets) == text


def test_secrets_accept_str_pem(material: Material) -> None:
    kwargs = _kwargs(material)
    kwargs["mqtt_ca_pem"] = material.ca_pem.decode()
    secrets = CloudSecrets(**kwargs)  # type: ignore[arg-type]
    assert secrets.mqtt_ca_pem == material.ca_pem
    assert secrets.ca_public_key().key_size == 2048


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_id", ""),
        ("app_secret", "   "),
        ("client_id_web", None),
        ("client_id_app", 5),
        ("mqtt_ca_pem", b""),
        (
            "mqtt_ca_pem",
            b"-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----",
        ),
        ("mqtt_client_cert_pem", b"garbage"),
        ("mqtt_client_key_pem", b"garbage"),
        ("mqtt_client_key_pem", 42),
    ],
)
def test_secrets_invalid(material: Material, field: str, value: object) -> None:
    kwargs = _kwargs(material)
    kwargs[field] = value
    with pytest.raises(SecretsInvalidError):
        CloudSecrets(**kwargs)  # type: ignore[arg-type]


def test_secrets_ca_must_be_rsa(material: Material) -> None:
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ec")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    kwargs = _kwargs(material)
    kwargs["mqtt_ca_pem"] = cert.public_bytes(serialization.Encoding.PEM)
    with pytest.raises(SecretsInvalidError, match="RSA"):
        CloudSecrets(**kwargs)  # type: ignore[arg-type]


# -- regions and modes ----------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "region"),
    [
        ("china", Region.CHINA),
        ("  China ", Region.CHINA),
        ("international", Region.INTERNATIONAL),
        ("global", Region.INTERNATIONAL),
        ("", Region.INTERNATIONAL),
        (None, Region.INTERNATIONAL),
        (3, Region.INTERNATIONAL),
    ],
)
def test_region_resolution(stored: object, region: Region) -> None:
    assert Region.resolve(stored) is region


def test_region_endpoints() -> None:
    intl = Region.INTERNATIONAL.endpoints
    assert intl.api_root == "https://cloud-universe.anycubic.com/p/p/workbench/api"
    assert (
        intl.url("/user/profile/userInfo")
        == "https://cloud-universe.anycubic.com/p/p/workbench/api/user/profile/userInfo"
    )
    assert intl.origin == "https://uc.makeronline.com"
    assert intl.mqtt_host == "mqtt-universe.anycubic.com"
    assert intl.mqtt_port == 8883
    assert intl.mqtt_check_hostname is True
    assert intl.image_base == "https://workbentch.s3.us-east-2.amazonaws.com/"
    china = Region.CHINA.endpoints
    assert china.base_url == "https://cloud-platform.anycubicloud.com/"
    assert china.origin == "https://uc.makeronline.cn"
    assert china.mqtt_host == "mqtt.anycubicloud.com"
    assert china.mqtt_check_hostname is False
    with pytest.raises(ValueError, match="start with"):
        intl.url("user/profile/userInfo")


@pytest.mark.parametrize(
    ("stored", "mode"),
    [
        (1, AuthMode.WEB),
        (2, AuthMode.ANDROID),
        (3, AuthMode.SLICER),
        ("3", AuthMode.SLICER),
        (None, AuthMode.WEB),
        (7, AuthMode.WEB),
        (True, AuthMode.WEB),
        ("x", AuthMode.WEB),
    ],
)
def test_auth_mode_resolution(stored: object, mode: AuthMode) -> None:
    assert AuthMode.resolve(stored) is mode


def test_mode_profiles() -> None:
    web, android, slicer = (m.profile for m in AuthMode)
    assert (web.device_type, web.is_cn, web.version) == ("web", "1", "1.0.0")
    assert (android.device_type, android.is_cn, android.version) == (
        "android",
        "0",
        "1.4.8",
    )
    assert (slicer.device_type, slicer.is_cn, slicer.version) == ("pcf", "1", "V3.0.0")
    assert web.nonce_style is NonceStyle.UUID
    assert android.nonce_style is NonceStyle.PACKED
    assert slicer.nonce_style is NonceStyle.UUID
    assert not AuthMode.WEB.supports_mqtt
    assert AuthMode.ANDROID.profile.mqtt_role == "app"
    assert AuthMode.SLICER.profile.mqtt_role == "pcf"


# -- signing --------------------------------------------------------------------


def test_signature_matches_the_stated_formula() -> None:
    # PROTOCOL A §3.4: md5(app_id ‖ timestamp ‖ version ‖ secret ‖ nonce ‖ app_id)
    nonce = "5f0c6e2a-9c1d-11f1-8b7e-0242ac120002"
    expected = hashlib.md5(
        (
            FAKE_APP_ID
            + "1759050000000"
            + "V3.0.0"
            + FAKE_APP_SECRET
            + nonce
            + FAKE_APP_ID
        ).encode(),
        usedforsecurity=False,
    ).hexdigest()
    assert (
        signature(FAKE_APP_ID, "1759050000000", "V3.0.0", FAKE_APP_SECRET, nonce)
        == expected
    )
    assert re.fullmatch(r"[0-9a-f]{32}", expected)
    assert md5_hex("abc") == "900150983cd24fb0d6963f7d28e17f72"


def test_uuid_nonce_is_version_1_text() -> None:
    nonce = uuid_nonce()
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-1[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", nonce
    )
    fixed = uuid.UUID("5f0c6e2a-9c1d-11f1-8b7e-0242ac120002")
    assert uuid_nonce(fixed) == "5f0c6e2a-9c1d-11f1-8b7e-0242ac120002"
    assert len(make_nonce(NonceStyle.UUID)) == 36


_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _expected_packed(value: uuid.UUID) -> str:
    """Independent rendering of PROTOCOL A §3.3 (two's complement by hand)."""

    def signed(raw: bytes) -> int:
        number = int.from_bytes(raw, "big")
        return number - (1 << 64) if number >= 1 << 63 else number

    def encode(number: int) -> str:
        out = ["0"] * 11
        position = 10
        while position >= 0 and number not in (0, -1):
            out[position] = _ALPHABET[number & 61]
            number = number // 64  # floor division == arithmetic shift by 6
            position -= 1
        return "".join(out)

    raw = value.bytes
    return encode(signed(raw[:8])) + encode(signed(raw[8:]))


@pytest.mark.parametrize(
    "value",
    [
        uuid.UUID("5f0c6e2a-9c1d-11f1-8b7e-0242ac120002"),
        uuid.UUID("ffffffff-ffff-1fff-bfff-ffffffffffff"),
        uuid.UUID("00000000-0000-1000-8000-000000000001"),
        uuid.UUID("80000000-0000-1000-0000-000000000000"),
    ],
)
def test_packed_nonce_matches_the_stated_encoding(value: uuid.UUID) -> None:
    nonce = packed_nonce(value)
    assert len(nonce) == 22
    assert nonce == _expected_packed(value)
    # the mask is 61: only 32 of the 62 characters can appear
    allowed = {_ALPHABET[i & 61] for i in range(64)}
    assert set(nonce) <= allowed | {"0"}
    assert len(allowed) == 32


def test_packed_nonce_fresh() -> None:
    assert len(packed_nonce()) == 22
    assert len(make_nonce(NonceStyle.PACKED)) == 22


def test_timestamp_and_device_id() -> None:
    assert re.fullmatch(r"\d{13}", timestamp_ms())
    device = make_android_device_id()
    assert re.fullmatch(r"[0-9a-f]{33}", device)


def _headers(
    profile: AuthMode, current: AuthMode, token: str | None = "TOK", **kw: object
) -> dict[str, str]:
    return build_headers(
        app_id=FAKE_APP_ID,
        app_secret=FAKE_APP_SECRET,
        profile_mode=profile,
        current_mode=current,
        region=Region.INTERNATIONAL,
        token=token,
        device_id=kw.get("device_id"),  # type: ignore[arg-type]
        nonce="n-o-n-c-e",
        timestamp="1759050000000",
    )


def test_slicer_headers_in_order() -> None:
    headers = _headers(AuthMode.SLICER, AuthMode.SLICER)
    assert list(headers) == [
        "Xx-Device-Type",
        "Xx-Is-Cn",
        "Xx-Nonce",
        "Xx-Signature",
        "Xx-Timestamp",
        "Xx-Version",
        "Content-Type",
        "XX-Token",
        "XX-LANGUAGE",
    ]
    assert headers["Xx-Device-Type"] == "pcf"
    assert headers["Xx-Is-Cn"] == "1"
    assert headers["Xx-Version"] == "V3.0.0"
    assert headers["Content-Type"] == "application/json"
    assert headers["XX-Token"] == "TOK"
    assert headers["XX-LANGUAGE"] == "US"
    assert headers["Xx-Signature"] == signature(
        FAKE_APP_ID, "1759050000000", "V3.0.0", FAKE_APP_SECRET, "n-o-n-c-e"
    )


def test_web_and_android_headers() -> None:
    web = _headers(AuthMode.WEB, AuthMode.WEB)
    assert web["Xx-Device-Type"] == "web"
    assert web["User-Agent"].startswith("Mozilla/5.0 (Macintosh")
    assert web["Origin"] == "https://uc.makeronline.com"
    android = _headers(AuthMode.ANDROID, AuthMode.ANDROID, device_id="dev-1")
    assert android["Xx-Is-Cn"] == "0"
    assert android["Xx-Version"] == "1.4.8"
    assert android["XX-Device-Id"] == "dev-1"
    assert "User-Agent" not in android
    no_token = _headers(AuthMode.SLICER, AuthMode.SLICER, token=None)
    assert "XX-Token" not in no_token


def test_mixed_headers_after_web_fallback() -> None:
    # PROTOCOL A §2.8: the per-mode values stay; UA and Origin follow the mode.
    headers = _headers(AuthMode.SLICER, AuthMode.WEB)
    assert headers["Xx-Device-Type"] == "pcf"
    assert headers["Xx-Version"] == "V3.0.0"
    assert "User-Agent" in headers
    assert "Origin" in headers


def test_fresh_nonce_and_timestamp_per_call() -> None:
    first = build_headers(
        app_id=FAKE_APP_ID,
        app_secret=FAKE_APP_SECRET,
        profile_mode=AuthMode.ANDROID,
        current_mode=AuthMode.ANDROID,
        region=Region.CHINA,
        token="t",
        device_id=None,
    )
    assert len(first["Xx-Nonce"]) == 22
    assert "XX-Device-Id" not in first
    assert first["Xx-Signature"] == signature(
        FAKE_APP_ID, first["Xx-Timestamp"], "1.4.8", FAKE_APP_SECRET, first["Xx-Nonce"]
    )
