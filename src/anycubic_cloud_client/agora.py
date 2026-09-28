"""Agora WebRTC signalling for the cloud camera (PROTOCOL D §1.7).

Signalling only: the browser is the WebRTC peer. This module takes the
credentials of the camera-open order and the browser's SDP offer, asks
Agora's access point for edge gateways, joins the channel over the gateway's
websocket and returns the SDP answer. No media passes through here.

Adapted from the Agora client of ``homeassistant_petkit``
(https://github.com/Jezza34000/homeassistant_petkit), used under its licence:

    MIT License

    Copyright (c) 2024 - 2026  @Jezza34000

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

Changes from PetKit: aiohttp websockets instead of ``websockets``; the
Anycubic values of PROTOCOL D §1.7 (host role, SDK 4.24.0, raced access
points, ``CN,GLOBAL``); the channel encryption fields PetKit does not need;
the publisher's SSRC awaited before answering; candidates after the join
dropped; no token renewal (Anycubic's tokens cannot be renewed).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .agora_sdp import (
    AnswerInput,
    VideoStream,
    build_answer_sdp,
    offer_to_ortc,
    parse_sdp,
)
from .errors import AgoraError

if TYPE_CHECKING:
    from .models import CameraCredentials

_LOGGER = logging.getLogger(__name__)

#: The ``agora-rtc-sdk-ng`` version Anycubic's slicer ships (PROTOCOL D §1.7).
SDK_VERSION = "4.24.0"
#: A current desktop Chrome user agent, claimed in the join message.
BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
#: Access points: the first pair is raced, the backups join after 1 s.
AP_PRIMARY = ("webrtc2-ap-web-1.agora.io", "webrtc2-2.ap.sd-rtn.com")
AP_BACKUP = ("webrtc2-ap-web-3.agora.io", "webrtc2-4.ap.sd-rtn.com")
AP_PATH = "/api/v2/transpond/webrtc?v=2"
AP_BACKUP_DELAY = 1.0
AP_REQUEST_TIMEOUT = 10.0
AP_TOTAL_TIMEOUT = 20.0
#: Area code sent as details 11 and 22; role detail 17 = "1" (host).
AREA_CODE = "CN,GLOBAL"
SERVICE_CHOOSE_SERVER = 11
SERVICE_PROXY_FALLBACK = 26
AP_URI = 22
#: Reply flag of the choose-server buffer.
FLAG_CHOOSE_SERVER = 4096
#: Timeouts of the gateway exchange.
EDGE_CONNECT_TIMEOUT = 10.0
JOIN_TIMEOUT = 15.0
VIDEO_STREAM_WAIT = 8.0
PING_INTERVAL = 3.0
#: Agora's own public key from its Web SDK: ``agora-rtc-sdk-ng`` 4.24.0 imports
#: it as SPKI for RSA-OAEP to wrap the channel key (PROTOCOL D §1.7, "Agora's
#: key"; Q6 in docs/QUESTIONS.md). Public protocol data, not an Anycubic
#: credential. SPKI, DER, base64; ``sdk_public_key_pem`` overrides it.
AGORA_SDK_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDCMnXAHkKIGAM+x4N22gCI+WyuSTM9ztkT"
    "3uYslTT2PuKmZfPzhH6kVdO7PTjGCOZnAsyb3oTtWat0KcxQ4jxvqQV+HvYl3iI1Yd4vl2c3"
    "qRMJPLtRDfNxa2Mcxgq7e9aEUibzdd0st+OJAy3tOj/Y0aVyxQiYDz3vqa6bP29adwIDAQAB"
)
#: Gateway errors worth naming.
ERROR_ILLEGAL_AES_PASSWORD = 2028
ERROR_INVALID_REJOIN_TOKEN = 2024

type JSON = dict[str, Any]
type CloseListener = Callable[[str], None]


class _SessionEnded(AgoraError):
    """The gateway ended the session: no other gateway is tried."""


# --------------------------------------------------------------------------
# Channel encryption (PROTOCOL D §1.7)
# --------------------------------------------------------------------------


def wrap_channel_secret(
    encryption_key: str, sdk_public_key_pem: bytes | None = None
) -> str:
    """``aes_secret``: the channel key's raw UTF-8 bytes (never hex-decoded)
    encrypted with RSA-OAEP (SHA-256, MGF1-SHA-256, no label) under the SDK's
    public key, base64-encoded.

    The key is Agora's (:data:`AGORA_SDK_PUBLIC_KEY`) unless a PEM override is
    given.
    """
    try:
        if sdk_public_key_pem is None:
            key = serialization.load_der_public_key(
                base64.b64decode(AGORA_SDK_PUBLIC_KEY)
            )
        else:
            key = serialization.load_pem_public_key(sdk_public_key_pem)
    except ValueError as err:
        raise AgoraError("The Agora SDK public key does not parse") from err
    if not isinstance(key, rsa.RSAPublicKey):
        raise AgoraError("The Agora SDK public key must be an RSA key")
    ciphertext = key.encrypt(
        encryption_key.encode(),
        padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
        ),
    )
    return base64.b64encode(ciphertext).decode("ascii")


def check_salt(salt: str) -> str:
    """The KDF salt must decode to exactly 32 bytes; it is sent unchanged."""
    try:
        decoded = base64.b64decode(salt, validate=True)
    except (ValueError, binascii.Error) as err:
        raise AgoraError("The camera's encryption salt is not base64") from err
    if len(decoded) != 32:
        raise AgoraError("The camera's encryption salt is not 32 bytes")
    return salt


def encryption_fields(
    credentials: CameraCredentials, sdk_public_key_pem: bytes | None = None
) -> JSON:
    """The four join fields for an encrypted channel; none for an open one.

    ``sdk_public_key_pem`` is an optional override of Agora's key.

    The SDK's internal names (``aesmode`` ...) must not appear on the wire.
    """
    mode = credentials.wire_encryption_mode
    if mode is None:
        return {}
    if not credentials.encryption_key or not credentials.encryption_kdf_salt:
        raise AgoraError(
            "The camera channel is encrypted but its key or salt is missing"
        )
    return {
        "aes_mode": mode,
        "aes_secret": wrap_channel_secret(
            credentials.encryption_key, sdk_public_key_pem
        ),
        "aes_encrypt": True,
        "aes_salt": check_salt(credentials.encryption_kdf_salt),
    }


# --------------------------------------------------------------------------
# Access point ("choose server")
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EdgeGateway:
    """One edge gateway from the access point's reply."""

    ip: str
    port: int
    ticket: str
    fingerprint: str | None = None

    @property
    def url(self) -> str:
        """``wss://<ip with dashes>.edge.agora.io:<port>`` (wildcard certificate)."""
        return f"wss://{self.ip.replace('.', '-')}.edge.agora.io:{self.port}"


@dataclass(frozen=True, slots=True)
class AccessPointReply:
    """The parsed choose-server buffer."""

    code: int
    uid: int
    cid: int
    cname: str
    ticket: str
    flag: int
    detail: Mapping[str, Any]
    server_ts: int
    opid: int
    gateways: tuple[EdgeGateway, ...] = field(default=())

    def ap_response(self, gateway: EdgeGateway) -> JSON:
        """``ap_response`` for ``join_v3``, with the dialled edge's ticket."""
        return {
            "code": self.code,
            "server_ts": self.server_ts,
            "uid": self.uid,
            "cid": self.cid,
            "cname": self.cname,
            "detail": dict(self.detail),
            "flag": self.flag,
            "opid": self.opid,
            "cert": gateway.ticket,
            "ticket": gateway.ticket,
        }


def build_ap_request(credentials: CameraCredentials, *, sid: str | None = None) -> JSON:
    """The JSON of the access point's single ``request`` form field."""
    return {
        "appid": credentials.app_id,
        "client_ts": int(time.time() * 1000),
        "opid": secrets.randbelow(10**12),
        "sid": sid or secrets.token_hex(16).upper(),
        "request_bodies": [
            {
                "uri": AP_URI,
                "buffer": {
                    "cname": credentials.channel,
                    "detail": {
                        "6": str(credentials.client_uid),
                        "11": AREA_CODE,
                        "17": "1",
                        "22": AREA_CODE,
                    },
                    "key": credentials.rtc_token,
                    "service_ids": [SERVICE_CHOOSE_SERVER, SERVICE_PROXY_FALLBACK],
                    "uid": credentials.client_uid,
                },
            }
        ],
    }


def parse_ap_reply(reply: Mapping[str, Any]) -> AccessPointReply:
    """Pick the choose-server buffer and its gateways out of the reply."""
    chosen: Mapping[str, Any] | None = None
    for item in reply.get("response_body") or []:
        buffer = item.get("buffer") if isinstance(item, Mapping) else None
        if not isinstance(buffer, Mapping) or buffer.get("code", -1) != 0:
            continue
        if chosen is None or buffer.get("flag") == FLAG_CHOOSE_SERVER:
            chosen = buffer
    if chosen is None:
        raise AgoraError("The Agora access point returned no usable server")
    detail = {**(reply.get("detail") or {}), **(chosen.get("detail") or {})}
    ticket = str(chosen.get("cert", ""))
    fingerprints = [
        part.strip()
        for part in str(detail.get("19", "") or "").split(";")
        if part.strip()
    ]
    gateways = []
    for index, edge in enumerate(chosen.get("edges_services") or []):
        if not isinstance(edge, Mapping) or not edge.get("ip") or not edge.get("port"):
            continue
        gateways.append(
            EdgeGateway(
                ip=str(edge["ip"]),
                port=int(edge["port"]),
                ticket=str(edge.get("ticket") or ticket),
                fingerprint=fingerprints[index] if index < len(fingerprints) else None,
            )
        )
    if not gateways:
        raise AgoraError("The Agora access point returned no edge gateway")
    if "23" in detail:
        _LOGGER.debug("Agora access point suggests another region for later lookups")
    return AccessPointReply(
        code=0,
        uid=int(chosen.get("uid", 0)),
        cid=int(chosen.get("cid", 0)),
        cname=str(chosen.get("cname", "")),
        ticket=ticket,
        flag=int(chosen.get("flag", 0)),
        detail=detail,
        server_ts=int(reply.get("enter_ts") or time.time() * 1000),
        opid=int(reply.get("opid", 0)),
        gateways=tuple(gateways),
    )


async def _post_ap(
    session: aiohttp.ClientSession, host: str, request: Mapping[str, Any]
) -> AccessPointReply:
    form = aiohttp.FormData()
    form.add_field("request", json.dumps(request), content_type="application/json")
    async with session.post(
        f"https://{host}{AP_PATH}",
        data=form,
        timeout=aiohttp.ClientTimeout(total=AP_REQUEST_TIMEOUT),
    ) as response:
        if response.status != 200:
            raise AgoraError(f"Agora access point {host} answered {response.status}")
        reply = await response.json(content_type=None)
    if not isinstance(reply, Mapping):
        raise AgoraError(f"Agora access point {host} answered no object")
    return parse_ap_reply(reply)


async def choose_server(
    session: aiohttp.ClientSession, credentials: CameraCredentials
) -> AccessPointReply:
    """Ask Agora's access points for edge gateways (PROTOCOL D §1.7).

    The two primary hosts are raced; the two backups join after 1 s. Each
    request may take 10 s, the whole lookup 20 s.
    """
    request = build_ap_request(credentials)
    tasks = {
        asyncio.create_task(_post_ap(session, host, request)) for host in AP_PRIMARY
    }
    backups_started = False
    errors: list[BaseException] = []
    try:
        async with asyncio.timeout(AP_TOTAL_TIMEOUT):
            while True:
                if not tasks:
                    if backups_started:
                        break
                    done: set[asyncio.Task[AccessPointReply]] = set()
                else:
                    done, tasks = await asyncio.wait(
                        tasks,
                        timeout=None if backups_started else AP_BACKUP_DELAY,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                for task in done:
                    if (error := task.exception()) is None:
                        return task.result()
                    errors.append(error)
                if not backups_started and (not done or not tasks):
                    backups_started = True
                    tasks |= {
                        asyncio.create_task(_post_ap(session, host, request))
                        for host in AP_BACKUP
                    }
    except TimeoutError as err:
        raise AgoraError("The Agora access points did not answer in time") from err
    finally:
        for task in tasks:
            task.cancel()
    detail = f": {errors[-1]}" if errors else ""
    raise AgoraError(f"No Agora access point answered{detail}")


# --------------------------------------------------------------------------
# The gateway session
# --------------------------------------------------------------------------


def _message(kind: str, body: Mapping[str, Any] | None = None) -> str:
    payload: JSON = {"_id": secrets.token_hex(3), "_type": kind}
    if body is not None:
        payload["_message"] = dict(body)
    return json.dumps(payload)


def find_video_streams(payload: object) -> list[VideoStream]:
    """Video streams already listed anywhere in a join reply."""
    found: list[VideoStream] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            stream = _video_stream(node)
            if stream is not None and stream not in found:
                found.append(stream)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return found


def _video_stream(node: Mapping[str, Any]) -> VideoStream | None:
    uid, ssrc = node.get("uid"), node.get("ssrcId")
    marked = (
        node.get("video") is True
        or node.get("stream_type") == "video"
        or node.get("rtxSsrcId") is not None
    )
    if not marked or not isinstance(uid, int) or not isinstance(ssrc, int):
        return None
    rtx = node.get("rtxSsrcId")
    cname = node.get("cname")
    return VideoStream(
        uid=uid,
        ssrc=ssrc,
        rtx_ssrc=rtx if isinstance(rtx, int) else None,
        cname=cname if isinstance(cname, str) else None,
    )


class AgoraCameraSession:
    """One viewing session: offer in, answer out, then keep the socket alive.

    Encrypted channels work without any key from the caller: the library
    ships Agora's public key from ``agora-rtc-sdk-ng`` 4.24.0
    (:data:`AGORA_SDK_PUBLIC_KEY`). ``sdk_public_key_pem`` (PEM, SPKI) is an
    optional override of it, never required.

    Usage::

        session = AgoraCameraSession(http, await cloud.open_camera(printer_id))
        answer = await session.answer(offer_sdp, session_id)
        ...
        await session.close()
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        credentials: CameraCredentials,
        *,
        sdk_public_key_pem: bytes | None = None,
        on_close: CloseListener | None = None,
    ) -> None:
        self._http = http
        self._credentials = credentials
        self._sdk_key = sdk_public_key_pem
        self._on_close = on_close
        self._candidates: list[str] = []
        self._joined = False
        self._closed = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._streams: dict[int, VideoStream] = {}
        self._subscribed: set[tuple[int, int]] = set()
        self._stream_seen = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def is_active(self) -> bool:
        return self._joined and not self._closed

    def add_ice_candidate(self, candidate: str) -> None:
        """A browser candidate. Kept before the join, dropped after it: the
        gateway has no verb to add one (it is ICE-lite)."""
        if self._joined:
            _LOGGER.debug("Dropping an ICE candidate that arrived after the join")
            return
        if candidate:
            self._candidates.append(candidate)

    async def answer(self, offer_sdp: str, session_id: str) -> str:
        """Join the channel and return the browser's SDP answer."""
        if self._closed or self._joined:
            raise AgoraError("This camera session has already been used")
        encryption = encryption_fields(self._credentials, self._sdk_key)
        offer = parse_sdp(offer_sdp)
        if not offer["media"]:
            raise AgoraError("The browser's offer has no media section")
        ortc = offer_to_ortc(offer, self._candidates)
        reply = await choose_server(self._http, self._credentials)
        last_error: Exception | None = None
        for gateway in reply.gateways:
            try:
                return await self._join(
                    gateway, reply, offer, ortc, session_id, encryption
                )
            except _SessionEnded as err:
                await self.close()
                raise AgoraError(str(err)) from err
            except AgoraError as err:
                last_error = err
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
                last_error = err
            _LOGGER.debug("Agora gateway failed: %s", last_error)
            await self._drop_socket()
        await self.close()
        raise AgoraError(f"No Agora gateway accepted the join: {last_error}")

    def _join_message(
        self,
        gateway: EdgeGateway,
        reply: AccessPointReply,
        ortc: JSON,
        session_id: str,
        encryption: JSON,
    ) -> JSON:
        credentials = self._credentials
        return {
            "p2p_id": 1,
            "session_id": session_id,
            "app_id": credentials.app_id,
            "channel_key": credentials.rtc_token,
            "channel_name": credentials.channel,
            "sdk_version": SDK_VERSION,
            "browser": BROWSER,
            "process_id": f"process-{secrets.token_hex(4)}-{secrets.token_hex(2)}-"
            f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(6)}",
            "mode": "live",
            "codec": "h264",
            "role": "host",
            "has_changed_gateway": False,
            "ap_response": reply.ap_response(gateway),
            "extend": "",
            "details": {},
            "features": {"rejoin": True},
            "attributes": {
                "userAttributes": {
                    "enableAudioMetadata": False,
                    "enableAudioPts": False,
                    "enablePublishedUserList": True,
                    "maxSubscription": 50,
                    "enableUserLicenseCheck": True,
                    "enableRTX": True,
                    "enableInstantVideo": False,
                    "enableDataStream2": False,
                    "enableAutFeedback": True,
                    "enableUserAutoRebalanceCheck": True,
                    "enableXR": True,
                    "enableLossbasedBwe": True,
                    "enableAutCC": True,
                    "enablePreallocPC": False,
                    "enablePubTWCC": False,
                    "enableSubTWCC": True,
                    "enablePubRTX": True,
                    "enableSubRTX": True,
                }
            },
            "join_ts": int(time.time() * 1000),
            "ortc": ortc,
            **encryption,
        }

    async def _join(
        self,
        gateway: EdgeGateway,
        reply: AccessPointReply,
        offer: JSON,
        ortc: JSON,
        session_id: str,
        encryption: JSON,
    ) -> str:
        async with asyncio.timeout(EDGE_CONNECT_TIMEOUT):
            ws = await self._http.ws_connect(gateway.url, heartbeat=None)
        self._ws = ws
        await ws.send_str(
            _message(
                "join_v3",
                self._join_message(gateway, reply, ortc, session_id, encryption),
            )
        )
        async with asyncio.timeout(JOIN_TIMEOUT):
            while True:
                event = await self._receive(ws)
                if event is None:
                    raise AgoraError("The gateway closed the socket during the join")
                if event.get("_result") == "success" and "_message" in event:
                    joined = event["_message"]
                    break
                if event.get("_result") == "failed":
                    raise AgoraError(
                        f"The gateway refused the join: {event.get('_message')}"
                    )
                await self._handle_event(event)
        answer_ortc = joined.get("ortc") if isinstance(joined, Mapping) else None
        if not isinstance(answer_ortc, Mapping):
            raise AgoraError("The join reply carried no ORTC description")
        self._joined = True
        await self._send(
            "set_client_role",
            {"role": "host", "level": 0, "client_ts": int(time.time() * 1000)},
        )
        for stream in find_video_streams(joined):
            await self._add_stream(stream)
        if not self._stream_seen.is_set():
            await self._wait_for_stream(ws)
        answer = build_answer_sdp(
            AnswerInput(
                offer=offer,
                ortc=dict(answer_ortc),
                extra_fingerprints=[gateway.fingerprint] if gateway.fingerprint else [],
                video=self._primary_stream(),
            )
        )
        if answer is None:
            raise AgoraError("Could not build the SDP answer from the join reply")
        self._start(ws)
        return answer

    async def _wait_for_stream(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Wait up to 8 s for the publisher's video stream (its SSRC must be in
        the first answer)."""
        try:
            async with asyncio.timeout(VIDEO_STREAM_WAIT):
                while not self._stream_seen.is_set():
                    event = await self._receive(ws)
                    if event is None:
                        raise AgoraError("The gateway closed the socket")
                    await self._handle_event(event)
        except TimeoutError:
            _LOGGER.info(
                "No video stream announced within %ss; the printer is probably "
                "not publishing yet",
                VIDEO_STREAM_WAIT,
            )

    def _primary_stream(self) -> VideoStream | None:
        publisher = self._credentials.publisher_uid
        if publisher is not None and publisher in self._streams:
            return self._streams[publisher]
        return next(iter(self._streams.values()), None)

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> JSON | None:
        while True:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    event = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    return event
                continue
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                return None

    async def _send(self, kind: str, body: Mapping[str, Any] | None = None) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_str(_message(kind, body))

    async def _add_stream(self, stream: VideoStream) -> None:
        publisher = self._credentials.publisher_uid
        if publisher is not None and stream.uid != publisher:
            _LOGGER.debug("Ignoring a video stream from another publisher")
            return
        self._streams[stream.uid] = stream
        if (stream.uid, stream.ssrc) not in self._subscribed:
            self._subscribed.add((stream.uid, stream.ssrc))
            await self._send(
                "subscribe",
                {
                    "stream_id": stream.uid,
                    "stream_type": "video",
                    "mode": "live",
                    "codec": "h264",
                    "p2p_id": 1,
                    "twcc": True,
                    "rtx": True,
                    "extend": "",
                    "ssrcId": stream.ssrc,
                },
            )
        self._stream_seen.set()

    async def _handle_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("_type")
        body = event.get("_message")
        body = body if isinstance(body, Mapping) else {}
        match kind:
            case "on_add_video_stream":
                stream = (
                    _video_stream({**body, "video": True})
                    if body.get("video")
                    else None
                )
                if stream is not None:
                    await self._add_stream(stream)
            case "on_remove_video_stream":
                _LOGGER.debug("Agora: the publisher removed its video stream")
            case "on_user_online" | "on_user_offline":
                _LOGGER.debug("Agora: %s", kind)
            case "on_crypt_error":
                raise _SessionEnded("the camera channel's key or salt was refused")
            case "on_token_privilege_will_expire":
                _LOGGER.info(
                    "Agora: the camera token expires soon (it cannot be renewed)"
                )
            case "on_token_privilege_did_expire":
                raise _SessionEnded("the camera token expired")
            case "on_p2p_lost":
                raise _SessionEnded("the camera connection was lost")
            case "error":
                code = body.get("error_code", body.get("code"))
                if code == ERROR_ILLEGAL_AES_PASSWORD:
                    raise _SessionEnded("the gateway refused the channel key (2028)")
                if code == ERROR_INVALID_REJOIN_TOKEN:
                    raise _SessionEnded("the rejoin token is invalid (2024)")
                _LOGGER.debug("Agora gateway error %s", code)

    def _start(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        for coro in (self._message_loop(ws), self._ping_loop()):
            task = asyncio.get_running_loop().create_task(coro)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _message_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        reason = "the gateway closed the socket"
        try:
            while (event := await self._receive(ws)) is not None:
                await self._handle_event(event)
        except AgoraError as err:
            reason = str(err)
        except (aiohttp.ClientError, OSError) as err:
            reason = f"socket error: {type(err).__name__}"
        _LOGGER.debug("Agora session ended: %s", reason)
        await self._finish(reason)

    async def _ping_loop(self) -> None:
        with contextlib.suppress(aiohttp.ClientError, OSError, ConnectionError):
            while not self._closed:
                await asyncio.sleep(PING_INTERVAL)
                await self._send("ping")

    async def _drop_socket(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(aiohttp.ClientError, OSError):
                await ws.close()

    async def _finish(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        await self._drop_socket()
        if self._on_close is not None:
            self._on_close(reason)

    async def close(self) -> None:
        """End the session (the browser left, or the entity was removed)."""
        await self._finish("closed")
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def open_camera_session(
    http: aiohttp.ClientSession,
    credentials: CameraCredentials,
    offer_sdp: str,
    session_id: str,
    *,
    candidates: Sequence[str] = (),
    sdk_public_key_pem: bytes | None = None,
    on_close: CloseListener | None = None,
) -> tuple[AgoraCameraSession, str]:
    """Convenience: build a session, feed early candidates, return it and the answer."""
    session = AgoraCameraSession(
        http, credentials, sdk_public_key_pem=sdk_public_key_pem, on_close=on_close
    )
    for candidate in candidates:
        session.add_ice_candidate(candidate)
    return session, await session.answer(offer_sdp, session_id)
