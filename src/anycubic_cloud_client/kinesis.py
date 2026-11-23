"""Amazon Kinesis Video Streams WebRTC signalling for the cloud camera (Beta 6, D27).

Some accounts' camera-open order answers with temporary AWS credentials
(:class:`~.models.KinesisCredentials`) instead of an Agora block. The
printer is then the MASTER of a Kinesis Video signalling channel named after
its ``key``, and a viewer joins it as VIEWER. This module is that viewer's
signalling, written from AWS's public documentation of the Kinesis Video
Streams WebRTC signalling protocol:

1. ``DescribeSignalingChannel`` → the channel ARN;
2. ``GetSignalingChannelEndpoint`` (role VIEWER) → the ``WSS`` endpoint;
3. a SigV4-presigned websocket to that endpoint;
4. ``SDP_OFFER`` out, ``SDP_ANSWER`` and ``ICE_CANDIDATE`` back, the
   browser's ``ICE_CANDIDATE`` out.

Signalling only: the browser is the WebRTC peer and no media passes here.
The credentials never reach a log line or an error message.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import aiohttp

from .errors import KinesisError
from .sigv4 import AwsCredentials, presign_url, sign_request

if TYPE_CHECKING:
    from .models import KinesisCredentials

_LOGGER = logging.getLogger(__name__)

SERVICE = "kinesisvideo"
#: Lifetime of the presigned websocket URL, in seconds.
URL_EXPIRES = 299
#: How long the offer waits for the printer's ``pushStarted`` (D28).
PUSH_WAIT = 5.0
#: How long one offer waits for an answer before it is sent again.
ANSWER_TIMEOUT = 5.0
#: How many times the offer is sent in all while no answer comes back.
OFFER_ATTEMPTS = 3
#: Timeout of each control-plane call.
HTTP_TIMEOUT = 10.0

#: The printer's ``video`` states (D28).
PUSH_STARTED = "pushStarted"
FAILED_STATES = frozenset({"initFailed", "pushFailed"})

_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]")

type AnswerListener = Callable[[str], None]
type CandidateListener = Callable[["IceCandidate"], None]
type ErrorListener = Callable[[str], None]
type CloseListener = Callable[[str], None]
type Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class IceCandidate:
    """One ICE candidate, in the browser's ``RTCIceCandidateInit`` shape."""

    candidate: str
    sdp_mid: str | None = None
    sdp_mline_index: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "sdpMid": self.sdp_mid,
            "sdpMLineIndex": self.sdp_mline_index,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> IceCandidate | None:
        candidate = data.get("candidate")
        if not isinstance(candidate, str):
            return None
        mid = data.get("sdpMid")
        index = data.get("sdpMLineIndex")
        return cls(
            candidate,
            mid if isinstance(mid, str) else None,
            index if isinstance(index, int) and not isinstance(index, bool) else None,
        )


def control_endpoint(region: str) -> str:
    """The Kinesis Video control plane of ``region``."""
    if not _REGION.fullmatch(region):
        raise KinesisError("The camera credentials name an unknown AWS region")
    return f"https://kinesisvideo.{region}.amazonaws.com"


def aws_credentials(credentials: KinesisCredentials) -> AwsCredentials:
    return AwsCredentials(
        credentials.access_key_id,
        credentials.secret_access_key,
        credentials.session_token,
    )


def encode_payload(body: Mapping[str, Any]) -> str:
    """A signalling ``messagePayload``: base64 of compact JSON."""
    text = json.dumps(body, separators=(",", ":"))
    return base64.b64encode(text.encode()).decode()


def decode_payload(payload: object) -> Mapping[str, Any] | None:
    """A received ``messagePayload``, or ``None`` when unreadable."""
    if not isinstance(payload, str):
        return None
    try:
        decoded = json.loads(base64.b64decode(payload, validate=False))
    except (binascii.Error, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def new_client_id() -> str:
    """A viewer client id: letters, digits, ``_.-``; unique per session."""
    return f"ha-{secrets.token_hex(8)}"


def _scrub(text: str, credentials: KinesisCredentials) -> str:
    """AWS's message with any credential in it removed (defence in depth)."""
    for secret in (
        credentials.access_key_id,
        credentials.secret_access_key,
        credentials.session_token,
    ):
        if secret:
            text = text.replace(secret, "**REDACTED**")
    return text


def status_error(message: Mapping[str, Any]) -> str | None:
    """The error a ``STATUS_RESPONSE`` reports, ``None`` when it is not one."""
    nested = message.get("statusResponse")
    body: Mapping[str, Any] = nested if isinstance(nested, Mapping) else message
    error_type = body.get("errorType")
    status = body.get("statusCode")
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        code = None
    if not error_type and (code is None or code == 200):
        return None
    description = body.get("description")
    parts = [str(p) for p in (error_type, description) if p]
    if not parts:
        parts = [f"status {code}"]
    return ": ".join(parts)


class KinesisViewerSession:
    """One viewing session as a Kinesis Video VIEWER.

    :meth:`start` opens the signalling websocket and returns; the offer is
    then sent from a background task once the printer has started pushing
    (or :data:`PUSH_WAIT` has passed), and sent again while no answer at all
    has come back, :data:`OFFER_ATTEMPTS` times in all. The answer goes to
    ``on_answer``, then the printer's candidates to ``on_candidate``. A
    failure after :meth:`start` goes once to ``on_error`` and ends the
    session; ``on_close`` is told whenever the session ends.

    Usage::

        session = KinesisViewerSession(http, credentials, printer_key,
                                       on_answer=..., on_candidate=...,
                                       on_error=...)
        await session.start(offer_sdp)
        session.add_ice_candidate(IceCandidate(...))
        session.notify_video_state("pushStarted")
        ...
        await session.close()
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        credentials: KinesisCredentials,
        channel_name: str,
        *,
        on_answer: AnswerListener,
        on_candidate: CandidateListener,
        on_error: ErrorListener,
        on_close: CloseListener | None = None,
        video_state: str | None = None,
        client_id: str | None = None,
        push_wait: float = PUSH_WAIT,
        answer_timeout: float = ANSWER_TIMEOUT,
        offer_attempts: int = OFFER_ATTEMPTS,
        clock: Clock | None = None,
    ) -> None:
        self._http = http
        self._credentials = credentials
        self._aws = aws_credentials(credentials)
        self._channel = channel_name
        self._client_id = client_id or new_client_id()
        self._on_answer = on_answer
        self._on_candidate = on_candidate
        self._on_error = on_error
        self._on_close = on_close
        self._push_wait = push_wait
        self._answer_timeout = answer_timeout
        self._offer_attempts = max(1, offer_attempts)
        self._clock = clock
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._opened = False
        self._closed = False
        self._offer_sent = False
        self._answered = asyncio.Event()
        self._pushing = asyncio.Event()
        self._video_state: str | None = None
        self._pending_out: list[IceCandidate] = []
        self._pending_in: list[IceCandidate] = []
        self.offers_sent = 0
        """How many times the offer went out (for tests and diagnostics)."""
        self.notify_video_state(video_state)

    # -- public ---------------------------------------------------------------

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def is_active(self) -> bool:
        return self._started and not self._closed

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def answered(self) -> bool:
        return self._answered.is_set()

    def notify_video_state(self, state: str | None) -> None:
        """The printer's latest ``video`` report state (D28)."""
        self._video_state = state
        if state == PUSH_STARTED:
            self._pushing.set()
        elif state in FAILED_STATES:
            self._pushing.set()  # stop waiting: the driver sees the failure
            # While start() runs it reports the failure itself, once.
            if self._opened and not self._closed:
                self._spawn(self._fail(f"The printer reported {state}"))

    def add_ice_candidate(self, candidate: IceCandidate) -> None:
        """A browser candidate: sent once the offer has gone, kept before."""
        if self._closed or not candidate.candidate:
            return
        if not self._offer_sent:
            self._pending_out.append(candidate)
            return
        self._spawn(self._send_candidate(candidate))

    async def start(self, offer_sdp: str) -> None:
        """Find the channel, open the signalling socket, start the offer.

        Raises :class:`KinesisError` when AWS refuses (an expired token, an
        unknown channel...) with AWS's own message.
        """
        if self._started or self._closed:
            raise KinesisError("This camera session has already been used")
        if not offer_sdp:
            raise KinesisError("The browser sent an empty offer")
        self._started = True
        try:
            arn = await self._describe_channel()
            endpoint = await self._viewer_endpoint(arn)
            query = urlencode(
                {"X-Amz-ChannelARN": arn, "X-Amz-ClientId": self._client_id},
                quote_via=quote,
            )
            url = presign_url(
                f"{endpoint.rstrip('/')}/?{query}",
                region=self._credentials.region,
                service=SERVICE,
                credentials=self._aws,
                expires=URL_EXPIRES,
                now=self._clock() if self._clock else None,
            )
            try:
                ws = await self._http.ws_connect(url, heartbeat=None)
            except aiohttp.WSServerHandshakeError as err:
                raise KinesisError(
                    f"AWS refused the camera's signalling connection ({err.status})"
                ) from None
            except (aiohttp.ClientError, OSError, TimeoutError) as err:
                raise KinesisError(
                    "Could not reach the camera's signalling service: "
                    f"{type(err).__name__}"
                ) from None
        except KinesisError:
            await self._finish("failed")
            raise
        if self.closed:
            # Closed while opening: never install the socket (no reader
            # would ever end, and close() has already run).
            with contextlib.suppress(aiohttp.ClientError, OSError):
                await ws.close()
            raise KinesisError("The camera session was closed while it opened")
        self._ws = ws
        if self._video_state in FAILED_STATES:
            await self._finish("failed")
            raise KinesisError(f"The printer reported {self._video_state}")
        self._opened = True
        self._spawn(self._read_loop(ws))
        self._spawn(self._drive(offer_sdp))

    async def close(self) -> None:
        """End the session (the browser left, or the entity was removed)."""
        await self._finish("closed")
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- control plane ------------------------------------------------------------

    async def _call(self, operation: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        url = f"{control_endpoint(self._credentials.region)}/{operation}"
        data = json.dumps(body, separators=(",", ":")).encode()
        headers = sign_request(
            "POST",
            url,
            region=self._credentials.region,
            service=SERVICE,
            credentials=self._aws,
            headers={"Content-Type": "application/json"},
            body=data,
            now=self._clock() if self._clock else None,
        )
        try:
            async with self._http.post(
                url,
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
            ) as response:
                text = await response.text()
                status = response.status
        except (aiohttp.ClientError, OSError, TimeoutError) as err:
            raise KinesisError(
                f"Could not reach Amazon Kinesis Video: {type(err).__name__}"
            ) from None
        try:
            reply = json.loads(text) if text else {}
        except ValueError:
            reply = {}
        if not isinstance(reply, Mapping):
            reply = {}
        if status != 200:
            message = reply.get("message") or reply.get("Message") or f"HTTP {status}"
            raise KinesisError(
                f"Amazon Kinesis Video refused the camera: "
                f"{_scrub(str(message), self._credentials)}"
            )
        return reply

    async def _describe_channel(self) -> str:
        reply = await self._call(
            "describeSignalingChannel", {"ChannelName": self._channel}
        )
        info = reply.get("ChannelInfo")
        arn = info.get("ChannelARN") if isinstance(info, Mapping) else None
        if not isinstance(arn, str) or not arn:
            raise KinesisError("Amazon Kinesis Video named no signalling channel")
        return arn

    async def _viewer_endpoint(self, arn: str) -> str:
        reply = await self._call(
            "getSignalingChannelEndpoint",
            {
                "ChannelARN": arn,
                "SingleMasterChannelEndpointConfiguration": {
                    "Protocols": ["WSS", "HTTPS"],
                    "Role": "VIEWER",
                },
            },
        )
        endpoints = reply.get("ResourceEndpointList")
        for entry in endpoints if isinstance(endpoints, list) else ():
            if not isinstance(entry, Mapping):
                continue
            url = entry.get("ResourceEndpoint")
            if entry.get("Protocol") == "WSS" and isinstance(url, str) and url:
                return url
        raise KinesisError("Amazon Kinesis Video named no websocket endpoint")

    # -- signalling ------------------------------------------------------------

    async def _send(self, action: str, body: Mapping[str, Any]) -> None:
        ws = self._ws
        if ws is None or self._closed:
            return
        message = {"action": action, "messagePayload": encode_payload(body)}
        await ws.send_str(json.dumps(message, separators=(",", ":")))

    async def _send_candidate(self, candidate: IceCandidate) -> None:
        with contextlib.suppress(aiohttp.ClientError, OSError, ConnectionError):
            await self._send("ICE_CANDIDATE", candidate.to_json())

    async def _drive(self, offer_sdp: str) -> None:
        """Wait for the push, send the offer, resend it while unanswered."""
        if not self._pushing.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._pushing.wait(), self._push_wait)
        if self._closed or self._video_state in FAILED_STATES:
            return
        offer = {"type": "offer", "sdp": offer_sdp}
        for _attempt in range(self._offer_attempts):
            try:
                await self._send("SDP_OFFER", offer)
            except (aiohttp.ClientError, OSError, ConnectionError):
                await self._fail("Could not send the camera offer")
                return
            self.offers_sent += 1
            if not self._offer_sent:
                self._offer_sent = True
                pending, self._pending_out = self._pending_out, []
                for candidate in pending:
                    await self._send_candidate(candidate)
            try:
                await asyncio.wait_for(self._answered.wait(), self._answer_timeout)
            except TimeoutError:
                if self.closed:
                    return
                _LOGGER.debug("No answer to the camera offer yet; sending it again")
                continue
            return
        await self._fail(
            "The printer did not answer the camera offer; "
            "is it online and signed in to the account?"
        )

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for frame in ws:
                if frame.type is aiohttp.WSMsgType.TEXT:
                    self._handle_text(frame.data)
                elif frame.type is aiohttp.WSMsgType.ERROR:
                    break
                if self._closed:
                    return
        except (aiohttp.ClientError, OSError, ConnectionError):
            pass
        if not self._closed:
            if self.answered:
                await self._finish("signalling closed")
            else:
                await self._fail("The camera's signalling connection closed")

    def _handle_text(self, text: str) -> None:
        if not text or not text.strip():
            return  # keep-alive
        try:
            message = json.loads(text)
        except ValueError:
            _LOGGER.debug("Unreadable signalling message")
            return
        if not isinstance(message, Mapping):
            return
        kind = message.get("messageType")
        if kind == "SDP_ANSWER":
            self._handle_answer(decode_payload(message.get("messagePayload")))
        elif kind == "ICE_CANDIDATE":
            body = decode_payload(message.get("messagePayload"))
            candidate = IceCandidate.from_json(body) if body is not None else None
            if candidate is None:
                return
            if self.answered:
                self._on_candidate(candidate)
            else:
                self._pending_in.append(candidate)
        elif kind == "STATUS_RESPONSE":
            error = status_error(message)
            if error is not None:
                self._spawn(
                    self._fail(
                        "Amazon Kinesis Video refused the camera: "
                        f"{_scrub(error, self._credentials)}"
                    )
                )
        else:
            _LOGGER.debug("Ignoring a signalling message of type %s", kind)

    def _handle_answer(self, body: Mapping[str, Any] | None) -> None:
        if self.answered:
            return  # a resent offer may be answered twice
        sdp = body.get("sdp") if body is not None else None
        if not isinstance(sdp, str) or not sdp:
            _LOGGER.debug("Ignoring an answer without an SDP")
            return
        self._answered.set()
        self._on_answer(sdp)
        pending, self._pending_in = self._pending_in, []
        for candidate in pending:
            self._on_candidate(candidate)

    # -- ending -------------------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fail(self, message: str) -> None:
        if self._closed:
            return
        self._on_error(message)
        await self._finish("failed")

    async def _finish(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._pushing.set()
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(aiohttp.ClientError, OSError):
                await ws.close()
        if self._on_close is not None:
            self._on_close(reason)


__all__ = [
    "ANSWER_TIMEOUT",
    "OFFER_ATTEMPTS",
    "PUSH_WAIT",
    "URL_EXPIRES",
    "IceCandidate",
    "KinesisViewerSession",
    "control_endpoint",
    "decode_payload",
    "encode_payload",
    "status_error",
]
