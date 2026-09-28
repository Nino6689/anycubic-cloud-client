"""Anycubic's application credentials, supplied by the caller.

This library contains none of them (``docs/CLEAN-ROOM.md``). The caller builds
one :class:`CloudSecrets` and passes it to every client (INTEGRATION-SPEC §2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .errors import SecretsInvalidError

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

_TEXT_FIELDS = ("app_id", "app_secret", "client_id_web", "client_id_app")
_PEM_FIELDS = ("mqtt_ca_pem", "mqtt_client_cert_pem", "mqtt_client_key_pem")


@dataclass(frozen=True, slots=True, repr=False)
class CloudSecrets:
    """Anycubic's app credentials and MQTT TLS material (PROTOCOL A §0.1).

    Validated on creation: every text field must be a non-empty string and
    every PEM field must parse (the CA and client certificate as X.509
    certificates, the key as an unencrypted private key). PEM fields may be
    given as ``bytes`` or ``str``; they are stored as ``bytes``.

    The ``repr`` shows only which fields are set, never their values.
    """

    app_id: str
    app_secret: str
    client_id_web: str
    client_id_app: str
    mqtt_ca_pem: bytes = field(compare=False)
    mqtt_client_cert_pem: bytes = field(compare=False)
    mqtt_client_key_pem: bytes = field(compare=False)

    def __post_init__(self) -> None:
        for name in _TEXT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SecretsInvalidError(
                    f"CloudSecrets.{name} must be a non-empty string"
                )
        for name in _PEM_FIELDS:
            value = getattr(self, name)
            if isinstance(value, str):
                value = value.encode()
                object.__setattr__(self, name, value)
            if not isinstance(value, bytes) or not value.strip():
                raise SecretsInvalidError(f"CloudSecrets.{name} must be PEM text")
        try:
            ca = x509.load_pem_x509_certificate(self.mqtt_ca_pem)
        except ValueError as err:
            raise SecretsInvalidError(
                "CloudSecrets.mqtt_ca_pem does not parse"
            ) from err
        if not isinstance(ca.public_key(), rsa.RSAPublicKey):
            raise SecretsInvalidError("CloudSecrets.mqtt_ca_pem must hold an RSA key")
        try:
            x509.load_pem_x509_certificate(self.mqtt_client_cert_pem)
        except ValueError as err:
            raise SecretsInvalidError(
                "CloudSecrets.mqtt_client_cert_pem does not parse"
            ) from err
        try:
            load_pem_private_key(self.mqtt_client_key_pem, password=None)
        except (ValueError, TypeError) as err:
            raise SecretsInvalidError(
                "CloudSecrets.mqtt_client_key_pem does not parse"
            ) from err

    def __repr__(self) -> str:
        names = ", ".join(f"{name}=<set>" for name in (*_TEXT_FIELDS, *_PEM_FIELDS))
        return f"CloudSecrets({names})"

    __str__ = __repr__

    def ca_public_key(self) -> RSAPublicKey:
        """The pinned CA's RSA public key (it encrypts the slicer MQTT password)."""
        key = x509.load_pem_x509_certificate(self.mqtt_ca_pem).public_key()
        return cast("RSAPublicKey", key)  # checked in __post_init__
