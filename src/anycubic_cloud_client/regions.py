"""Regions and auth modes (PROTOCOL A §1, §2.3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

#: The workbench API path, joined to the base URL (PROTOCOL A §1.3).
API_PATH = "p/p/workbench/api"


class Region(StrEnum):
    """The two separate Anycubic deployments (PROTOCOL A §1.1)."""

    INTERNATIONAL = "international"
    CHINA = "china"

    @classmethod
    def resolve(cls, value: object) -> Region:
        """Resolve any stored value; unknown, absent or wrong-typed is international."""
        if isinstance(value, str):
            text = value.strip().lower()
            for region in cls:
                if region.value == text:
                    return region
        return cls.INTERNATIONAL

    @property
    def endpoints(self) -> RegionEndpoints:
        return _ENDPOINTS[self]


@dataclass(frozen=True, slots=True)
class RegionEndpoints:
    """Hosts and settings of one region (PROTOCOL A §1.2, C §1.1)."""

    base_domain: str
    auth_domain: str
    mqtt_host: str
    mqtt_port: int
    mqtt_check_hostname: bool
    image_base: str

    @property
    def base_url(self) -> str:
        """``https://<base domain>/`` with exactly one trailing slash."""
        return f"https://{self.base_domain}/"

    @property
    def api_root(self) -> str:
        """Base URL + ``p/p/workbench/api`` (no trailing slash)."""
        return self.base_url + API_PATH

    @property
    def origin(self) -> str:
        """The ``Origin`` header sent in web mode."""
        return f"https://{self.auth_domain}"

    def url(self, path: str) -> str:
        """Request URL for an endpoint path starting with ``/``."""
        if not path.startswith("/"):
            raise ValueError("endpoint paths start with '/'")
        return self.api_root + path


_IMAGE_BASE = "https://workbentch.s3.us-east-2.amazonaws.com/"  # sic, required

_ENDPOINTS: dict[Region, RegionEndpoints] = {
    Region.INTERNATIONAL: RegionEndpoints(
        base_domain="cloud-universe.anycubic.com",
        auth_domain="uc.makeronline.com",
        mqtt_host="mqtt-universe.anycubic.com",
        mqtt_port=8883,
        mqtt_check_hostname=True,
        image_base=_IMAGE_BASE,
    ),
    # China's API path, MQTT port and image base are assumed to match the
    # international ones (INTEGRATION-SPEC §11, Q5 in docs/QUESTIONS.md). Its
    # broker certificate does not name the host, so only the hostname check
    # is waived; chain verification stays on (PROTOCOL C §1.1).
    Region.CHINA: RegionEndpoints(
        base_domain="cloud-platform.anycubicloud.com",
        auth_domain="uc.makeronline.cn",
        mqtt_host="mqtt.anycubicloud.com",
        mqtt_port=8883,
        mqtt_check_hostname=False,
        image_base=_IMAGE_BASE,
    ),
}

#: JWKS of the auth domain. The pre-check always uses the international URL
#: (PROTOCOL A §1.4).
JWKS_URL = "https://uc.makeronline.com/.well-known/jwks"

#: Issuer whose tokens get the local signature pre-check (BEHAVIOUR §5.8).
JWKS_ISSUER = "https://uc.makeronline.com"

#: Browser user agent: web mode, the JWKS fetch (403 otherwise) and web uploads.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class NonceStyle(StrEnum):
    """How ``Xx-Nonce`` is formed (PROTOCOL A §3.3)."""

    UUID = "uuid"
    PACKED = "packed"


@dataclass(frozen=True, slots=True)
class ModeProfile:
    """Per-mode header values (PROTOCOL A §2.3)."""

    device_type: str
    is_cn: str
    version: str
    nonce_style: NonceStyle
    mqtt_role: str | None
    """MQTT app id in the username; ``None`` when MQTT login is impossible."""


class AuthMode(IntEnum):
    """Sign-in modes, stored as ints (PROTOCOL A §2.3)."""

    WEB = 1
    ANDROID = 2
    SLICER = 3

    @classmethod
    def resolve(cls, value: object) -> AuthMode:
        """Resolve a stored mode; absent or unknown values mean WEB."""
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                return cls(value)
            except ValueError:
                pass
        return cls.WEB

    @property
    def profile(self) -> ModeProfile:
        return _PROFILES[self]

    @property
    def supports_mqtt(self) -> bool:
        """SLICER and ANDROID may log in to the cloud MQTT; WEB never can."""
        return self.profile.mqtt_role is not None


_PROFILES: dict[AuthMode, ModeProfile] = {
    AuthMode.WEB: ModeProfile("web", "1", "1.0.0", NonceStyle.UUID, None),
    AuthMode.ANDROID: ModeProfile("android", "0", "1.4.8", NonceStyle.PACKED, "app"),
    AuthMode.SLICER: ModeProfile("pcf", "1", "V3.0.0", NonceStyle.UUID, "pcf"),
}
