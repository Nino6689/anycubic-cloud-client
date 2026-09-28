"""Cloud MQTT identity, TLS context and topics (PROTOCOL C §1-§2).

Pure functions: nothing here opens a socket.
"""

from __future__ import annotations

import base64
import os
import ssl
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import bcrypt
from cryptography.hazmat.primitives.asymmetric import padding

from .errors import MqttNotAllowedError
from .regions import AuthMode, RegionEndpoints
from .signing import md5_hex

if TYPE_CHECKING:
    from .credentials import CloudSecrets
    from .models import Account

#: Every cloud topic starts with this prefix (PROTOCOL C §2.1).
TOPIC_PREFIX = "anycubic/anycubicCloud/v1"

#: Keep-alive of the cloud link: 20 minutes (PROTOCOL C §1.6).
KEEPALIVE = 1200

#: Suffix the slicer appends before hashing its client id (PROTOCOL C §1.4).
SLICER_CLIENT_ID_SUFFIX = "pcf"

REDACTED = "**REDACTED**"


@dataclass(frozen=True, slots=True, repr=False)
class MqttIdentity:
    """Client id, username and password for one CONNECT (PROTOCOL C §1.4-§1.5).

    The password is randomised (RSA padding or bcrypt salt), so a new
    identity is worked out for every (re)connect. The ``repr`` shows no
    secret.
    """

    client_id: str
    username: str
    password: str

    def __repr__(self) -> str:
        return "MqttIdentity(<redacted>)"


def mqtt_client_id(identity: str, mode: AuthMode) -> str:
    """``md5(identity + "pcf")`` in SLICER mode, ``md5(identity)`` otherwise."""
    if mode is AuthMode.SLICER:
        return md5_hex(identity + SLICER_CLIENT_ID_SUFFIX)
    return md5_hex(identity)


def slicer_password(user_token: str, secrets: CloudSecrets) -> str:
    """Base64 of the user token RSA-encrypted (PKCS#1 v1.5, one block) under
    the pinned CA's public key."""
    try:
        ciphertext = secrets.ca_public_key().encrypt(
            user_token.encode(), padding.PKCS1v15()
        )
    except ValueError as err:
        raise MqttNotAllowedError(
            "The user token does not fit one RSA block of the CA key"
        ) from err
    return base64.b64encode(ciphertext).decode("ascii")


def android_password(user_token: str) -> str:
    """bcrypt, fresh salt at the library defaults, of ``md5_hex(user_token)``."""
    return bcrypt.hashpw(md5_hex(user_token).encode(), bcrypt.gensalt()).decode("ascii")


def username_signature(client_id: str, password: str) -> str:
    """``md5(client_id + password + client_id)``."""
    return md5_hex(client_id + password + client_id)


def build_identity(
    mode: AuthMode,
    user_token: str | None,
    account: Account | None,
    secrets: CloudSecrets,
) -> MqttIdentity:
    """Work out the MQTT login (PROTOCOL A §2.12, C §1.4-§1.5).

    Raises :class:`MqttNotAllowedError`, before any socket is opened, for a
    WEB token, a missing user token, or an account with neither an e-mail nor
    a mobile number.
    """
    role = mode.profile.mqtt_role
    if role is None:
        raise MqttNotAllowedError("Web-mode tokens cannot log in to the cloud MQTT")
    if not user_token:
        raise MqttNotAllowedError("No user token: sign in first")
    if account is None:
        raise MqttNotAllowedError("No account: sign in first")
    identity = account.mqtt_identity
    if identity is None:
        raise MqttNotAllowedError(
            "Unable to build the MQTT client id: the account has neither an "
            "email nor a mobile number"
        )
    client_id = mqtt_client_id(identity, mode)
    if mode is AuthMode.SLICER:
        password = slicer_password(user_token, secrets)
    else:
        password = android_password(user_token)
    username = "|".join(
        ("user", role, account.identifier, username_signature(client_id, password))
    )
    return MqttIdentity(client_id=client_id, username=username, password=password)


def build_tls_context(
    secrets: CloudSecrets, endpoints: RegionEndpoints
) -> ssl.SSLContext:
    """The broker's TLS context (PROTOCOL C §1.2).

    TLS ≥ 1.2; only the pinned CA as trust anchor with the chain always
    verified; the hostname checked per region; strict X.509 off (the CA lacks
    ``keyUsage``); security level 0 (the client certificate is SHA-1
    signed); mutual TLS with the client certificate and key.

    Blocking (it writes the client material to a private temporary file for
    ``load_cert_chain``, which only reads files): run it in an executor.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = endpoints.mqtt_check_hostname
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.set_ciphers("DEFAULT:@SECLEVEL=0")
    context.load_verify_locations(cadata=secrets.mqtt_ca_pem.decode("ascii"))
    handle, path = tempfile.mkstemp(prefix="acc-", suffix=".pem")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(secrets.mqtt_client_cert_pem.rstrip() + b"\n")
            file.write(secrets.mqtt_client_key_pem.rstrip() + b"\n")
        context.load_cert_chain(path)
    finally:
        os.unlink(path)
    return context


# --------------------------------------------------------------------------
# Topics (PROTOCOL C §2)
# --------------------------------------------------------------------------


def user_topics(user_id: int) -> tuple[str, str]:
    """U1 (``slice/report``) and U2 (``fdmslice/report``) for the account."""
    base = f"{TOPIC_PREFIX}/server/app/{user_id}/{md5_hex(str(user_id))}"
    return (f"{base}/slice/report", f"{base}/fdmslice/report")


def printer_topics(machine_type: int, printer_key: str) -> tuple[str, str]:
    """P1 (``printer/app/.../#``) and P2 (``+/public/.../#``) for one printer."""
    return (
        f"{TOPIC_PREFIX}/printer/app/{machine_type}/{printer_key}/#",
        f"{TOPIC_PREFIX}/+/public/{machine_type}/{printer_key}/#",
    )


@dataclass(frozen=True, slots=True)
class TopicInfo:
    """What the routing needs from a topic (PROTOCOL C §2.1, §2.5, §3.5)."""

    segments: tuple[str, ...]

    def _segment(self, index: int) -> str | None:
        return self.segments[index] if len(self.segments) > index else None

    @property
    def origin(self) -> str | None:
        """Segment 3: ``printer``, ``server``, ``web``..."""
        return self._segment(3)

    @property
    def is_user_topic(self) -> bool:
        return self.origin == "server"

    @property
    def printer_key(self) -> str | None:
        """Segment 6 of a printer topic."""
        return None if self.is_user_topic else self._segment(6)

    @property
    def is_response(self) -> bool:
        """Segment 7 is ``response``."""
        return self._segment(7) == "response"

    @property
    def is_ace(self) -> bool:
        """The topic contains ``multiColorBox`` (ACE firmware reports)."""
        return "multiColorBox" in self.segments

    @property
    def ace_box_index(self) -> int:
        """Segment 9 when all digits, else 0."""
        segment = self._segment(9)
        return int(segment) if segment is not None and segment.isdigit() else 0


def parse_topic(topic: str) -> TopicInfo:
    return TopicInfo(tuple(topic.split("/")))


def redact_topic(topic: str) -> str:
    """Hide a topic's printer key (segment 6), and a user topic's account id."""
    segments = topic.split("/")
    if len(segments) > 6:
        segments[6] = REDACTED
        if segments[3] == "server":  # user topics carry the account id too
            segments[5] = REDACTED
    return "/".join(segments)
