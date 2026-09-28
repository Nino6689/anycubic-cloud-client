"""Exceptions raised by anycubic-cloud-client.

Every exception derives from :class:`AnycubicCloudError`. The classes are kept
distinct on purpose: only :class:`CredentialsRejectedError` may lead a caller
to ask the user for a new token (PROTOCOL A §4.4). Transport, parse,
maintenance and printer-record failures never do.
"""

from __future__ import annotations

from enum import StrEnum


class AnycubicCloudError(Exception):
    """Base class for every error raised by this library."""


class SecretsInvalidError(AnycubicCloudError, ValueError):
    """The :class:`~anycubic_cloud_client.CloudSecrets` given are unusable.

    Raised when the object is built, before any network traffic.
    """


class RejectReason(StrEnum):
    """Why the cloud refused the credentials (PROTOCOL A §4.3 rows 1-3)."""

    INVALID = "invalid"
    """Any invalid token (``User does not exist``, or no user record)."""
    EXPIRED = "expired"
    """The server revoked the session behind the access token."""
    WRONG_TOKEN_TYPE = "wrong_token_type"  # noqa: S105 - not a secret
    """Every mode failed and the JWT's ``tokenType`` is not ``access-token``."""


class CredentialsRejectedError(AnycubicCloudError):
    """The cloud refused the token, after every retry and fallback.

    This is the only error that justifies re-authentication.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: RejectReason = RejectReason.INVALID,
        server_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.server_message = server_message


class ServiceUnavailableError(AnycubicCloudError):
    """Transport error, timeout, non-JSON answer or ``msg`` = ``request error``.

    Transient: server maintenance or rate limiting (PROTOCOL A §4.4).
    """


class UnexpectedResponseError(AnycubicCloudError):
    """The cloud answered, but without the expected data shape."""


class PrinterRemovedError(AnycubicCloudError):
    """The cloud reports the printer deleted (``code`` 1007).

    Switching a printer to LAN Mode causes this. It is not an authentication
    failure (BEHAVIOUR B8).
    """


class OrderRefusedError(AnycubicCloudError):
    """An order was answered with null ``data``; ``server_message`` says why."""

    def __init__(self, message: str, *, server_message: str | None = None) -> None:
        super().__init__(message)
        self.server_message = server_message


class CloudFileNotFoundError(AnycubicCloudError):
    """An order was answered ``No file found``: the cloud file does not exist.

    Never a success (2.x counted three of these as one, PROTOCOL D §2.5.4).
    """


class NoCameraCredentialsError(AnycubicCloudError):
    """The camera-open order returned no credentials, even after a fresh login.

    Either the printer has no camera, or another Anycubic session (slicer or
    phone app) holds the account (PROTOCOL A §5.2).
    """


class UploadError(AnycubicCloudError):
    """A cloud upload failed (empty file, storage error, claim failed...)."""


class StorageFullError(UploadError):
    """The account's cloud storage has less free space than the file needs."""


class SlotMappingError(AnycubicCloudError, ValueError):
    """The ACE slot list does not fit the file or the printer (PROTOCOL D §2.4)."""


class GcodeMetadataError(AnycubicCloudError, ValueError):
    """The G-code header lacks the colour list or filament figures (D §2.6)."""


class RenameFailedError(AnycubicCloudError):
    """The cloud did not store the requested printer name (PROTOCOL D §5.4)."""


class MqttError(AnycubicCloudError):
    """Base class of the cloud MQTT errors."""


class MqttNotAllowedError(MqttError):
    """Cloud MQTT is impossible for this client.

    Web-mode tokens cannot log in, and the account needs an e-mail or mobile
    number and a user token (PROTOCOL A §2.12).
    """


class MqttConnectionError(MqttError):
    """The broker could not be reached (DNS, TCP, TLS, timeout, wrong port)."""


class MqttAuthError(MqttError):
    """The broker refused the login (CONNACK not authorised).

    Usually a revoked or stale token (PROTOCOL C §6.15).
    """


class MqttNotConnectedError(MqttError):
    """The cloud MQTT link is not up (and is not coming up in time)."""


class AgoraError(AnycubicCloudError):
    """The Agora camera signalling failed."""
