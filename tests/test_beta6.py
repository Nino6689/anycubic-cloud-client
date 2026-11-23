"""Beta 6 of hass-anycubic-next (D26-D28): slice-parameter replies, the
Kinesis Video camera and the printer's video reports.

Nothing here reaches AWS: SigV4 is checked against AWS's published examples
and a fixed-clock known answer, and the signalling against a fake websocket.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import pytest

from anycubic_cloud_client import (
    AgoraCameraSession,
    AuthMode,
    AwsCredentials,
    CameraCredentials,
    CloudSecrets,
    IceCandidate,
    KinesisCredentials,
    KinesisError,
    KinesisViewerSession,
    NoCameraCredentialsError,
    PrintUpdate,
    VideoUpdate,
    parse_cloud_message,
    presign_url,
    sign_request,
)
from anycubic_cloud_client import kinesis as kinesis_module
from anycubic_cloud_client.kinesis import (
    control_endpoint,
    decode_payload,
    encode_payload,
    status_error,
)
from anycubic_cloud_client.sigv4 import canonical_request, signing_key

from .conftest import USER_INFO, FakeResponse, FakeSession, envelope, make_client
from .payloads import CAMERA_REPLY, MACHINE_TYPE, PRINTER_KEY

PUBLIC = f"anycubic/anycubicCloud/v1/printer/public/{MACHINE_TYPE}/{PRINTER_KEY}"

# -- SigV4 against AWS's published examples ------------------------------------------

DOC_KEY = AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
DOC_TIME = datetime(2015, 8, 30, 12, 36, tzinfo=UTC)


def test_sigv4_get_vanilla() -> None:
    """The SigV4 test suite's ``get-vanilla`` request."""
    headers = sign_request(
        "GET",
        "https://example.amazonaws.com/",
        region="us-east-1",
        service="service",
        credentials=DOC_KEY,
        now=DOC_TIME,
    )
    assert headers["X-Amz-Date"] == "20150830T123600Z"
    assert headers["Host"] == "example.amazonaws.com"
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
        "aws4_request, SignedHeaders=host;x-amz-date, Signature="
        "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )
    assert "X-Amz-Security-Token" not in headers


def test_sigv4_iam_list_users() -> None:
    """The ``ListUsers`` example of AWS's "signed request" guide."""
    headers = sign_request(
        "GET",
        "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        region="us-east-1",
        service="iam",
        credentials=DOC_KEY,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        now=DOC_TIME,
    )
    assert headers["Authorization"].endswith(
        "SignedHeaders=content-type;host;x-amz-date, Signature="
        "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_sigv4_presigned_s3_example() -> None:
    """The presigned-URL example of AWS's S3 query-authentication guide."""
    url = presign_url(
        "https://examplebucket.s3.amazonaws.com/test.txt",
        region="us-east-1",
        service="s3",
        credentials=AwsCredentials(
            "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        ),
        expires=86400,
        payload_hash="UNSIGNED-PAYLOAD",
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    query = dict(parse_qsl(urlsplit(url).query))
    assert query["X-Amz-Signature"] == (
        "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )
    assert query["X-Amz-Credential"] == (
        "AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request"
    )


def test_sigv4_helpers() -> None:
    assert repr(DOC_KEY) == "AwsCredentials(temporary=False)"
    assert "EXAMPLEKEY" not in repr(AwsCredentials("a", "EXAMPLEKEY", "tok"))
    assert len(signing_key("s", "20150830", "us-east-1", "iam")) == 32
    text, signed = canonical_request(
        "post", "https://h/a b/", {"X-B": " two   words ", "x-b": "x", "Host": "h"}, "p"
    )
    assert text.splitlines()[:2] == ["POST", "/a%20b/"]
    assert "x-b:two words,x\n" in text
    assert signed == "host;x-b"
    empty, _ = canonical_request("GET", "https://h", {"host": "h"}, "p")
    assert empty.splitlines()[1] == "/"


def test_sigv4_session_token_is_signed_header() -> None:
    temporary = AwsCredentials("ASIAFAKE", "secret", "SESSION")
    headers = sign_request(
        "POST",
        "https://kinesisvideo.eu-west-3.amazonaws.com/describeSignalingChannel",
        region="eu-west-3",
        service="kinesisvideo",
        credentials=temporary,
        headers={"Content-Type": "application/json"},
        body=b"{}",
        now=DOC_TIME,
    )
    assert headers["X-Amz-Security-Token"] == "SESSION"
    assert (
        "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token"
        in (headers["Authorization"])
    )
    # Fixed-clock known answer (regression guard for the signing above).
    assert (
        headers["Authorization"]
        == sign_request(
            "POST",
            "https://kinesisvideo.eu-west-3.amazonaws.com/describeSignalingChannel",
            region="eu-west-3",
            service="kinesisvideo",
            credentials=temporary,
            headers={"Content-Type": "application/json"},
            body=b"{}",
            now=DOC_TIME,
        )["Authorization"]
    )


def test_presigned_signalling_url() -> None:
    """D27 step 3: ``host`` only, the token in the query before signing,
    299 s, the ARN and client id signed too."""
    temporary = AwsCredentials("ASIAFAKE", "secret", "SESSION/TOKEN+=")
    url = presign_url(
        "wss://v-1234.kinesisvideo.eu-west-3.amazonaws.com/"
        "?X-Amz-ChannelARN=arn%3Aaws%3Akinesisvideo%3Aeu-west-3%3A1%3Achannel%2Fk%2F1"
        "&X-Amz-ClientId=ha-1",
        region="eu-west-3",
        service="kinesisvideo",
        credentials=temporary,
        expires=299,
        now=DOC_TIME,
    )
    parts = urlsplit(url)
    assert parts.scheme == "wss"
    assert parts.path == "/"
    query = dict(parse_qsl(parts.query))
    assert query["X-Amz-ChannelARN"] == "arn:aws:kinesisvideo:eu-west-3:1:channel/k/1"
    assert query["X-Amz-ClientId"] == "ha-1"
    assert query["X-Amz-Expires"] == "299"
    assert query["X-Amz-SignedHeaders"] == "host"
    assert query["X-Amz-Security-Token"] == "SESSION/TOKEN+="
    assert (
        query["X-Amz-Credential"]
        == "ASIAFAKE/20150830/eu-west-3/kinesisvideo/aws4_request"
    )
    # The signature is the one over the canonical request with every
    # parameter but the signature itself.
    unsigned = {k: v for k, v in query.items() if k != "X-Amz-Signature"}
    request, _ = canonical_request(
        "GET",
        "wss://v-1234.kinesisvideo.eu-west-3.amazonaws.com/",
        {"host": parts.netloc},
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        extra_query=list(unsigned.items()),
    )
    scope = "20150830/eu-west-3/kinesisvideo/aws4_request"
    string = (
        f"AWS4-HMAC-SHA256\n20150830T123600Z\n{scope}\n"
        + hashlib.sha256(request.encode()).hexdigest()
    )
    key = signing_key("secret", "20150830", "eu-west-3", "kinesisvideo")
    expected = hmac.new(key, string.encode(), hashlib.sha256).hexdigest()
    assert query["X-Amz-Signature"] == expected


# -- D27: the camera-open order --------------------------------------------------------

ORDER = "/work/operation/sendOrder"
EXCHANGE = "/v3/public/loginWithAccessToken"
USER = "/user/profile/userInfo"
TOKEN_BLOCK = {
    "region": "eu-west-3",
    "tmpSecretId": "ASIAFAKEKEYID000",
    "tmpSecretKey": "fakeSecretKey/000",
    "sessionToken": "fakeSessionToken000",
    "expiration": 1790000000,
}


def kvs_reply(**block: Any) -> dict[str, Any]:
    return envelope({"msgid": "m", "token": {**TOKEN_BLOCK, **block}})


def test_kinesis_credentials() -> None:
    credentials = KinesisCredentials.from_data({"token": TOKEN_BLOCK})
    assert credentials is not None
    assert credentials.region == "eu-west-3"
    assert credentials.access_key_id == "ASIAFAKEKEYID000"
    assert credentials.expiration == 1790000000
    text = repr(credentials)
    for secret in ("ASIAFAKEKEYID000", "fakeSecretKey/000", "fakeSessionToken000"):
        assert secret not in text
    assert KinesisCredentials.from_data({}) is None
    assert KinesisCredentials.from_data({"token": "opaque"}) is None
    for missing in ("region", "tmpSecretId", "tmpSecretKey", "sessionToken"):
        block = {k: v for k, v in TOKEN_BLOCK.items() if k != missing}
        assert KinesisCredentials.from_data({"token": block}) is None
    no_expiry = {k: v for k, v in TOKEN_BLOCK.items() if k != "expiration"}
    parsed = KinesisCredentials.from_data({"token": no_expiry})
    assert parsed is not None
    assert parsed.expiration is None


async def test_agora_wins_over_token(http: FakeSession, secrets: CloudSecrets) -> None:
    both = {**CAMERA_REPLY, "data": {**CAMERA_REPLY["data"], "token": TOKEN_BLOCK}}
    http.add("POST", ORDER, both)
    credentials = await make_client(http, secrets).open_camera_stream(5)
    assert isinstance(credentials, CameraCredentials)


async def test_token_only_is_kinesis(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("POST", ORDER, kvs_reply())
    c = make_client(http, secrets, user_token="OLD", access_token="access")
    credentials = await c.open_camera_stream(5)
    assert isinstance(credentials, KinesisCredentials)
    assert len(http.calls) == 1  # a usable block: no re-login


async def test_open_camera_refuses_kinesis(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", ORDER, kvs_reply())
    with pytest.raises(NoCameraCredentialsError, match="Kinesis"):
        await make_client(http, secrets, mode=AuthMode.WEB).open_camera(5)


async def test_incomplete_token_counts_as_none(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", ORDER, kvs_reply(sessionToken=None))
    http.add("POST", EXCHANGE, envelope({"token": "FRESH"}))
    http.add("GET", USER, envelope(USER_INFO))
    c = make_client(http, secrets, user_token="OLD", access_token="access")
    with pytest.raises(NoCameraCredentialsError, match="incomplete") as info:
        await c.open_camera_stream(5)
    assert "another Anycubic session" not in str(info.value)
    assert len(http.calls_to(ORDER)) == 2


async def test_incomplete_then_kinesis_after_login(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", ORDER, kvs_reply(region=""), kvs_reply())
    http.add("POST", EXCHANGE, envelope({"token": "FRESH"}))
    http.add("GET", USER, envelope(USER_INFO))
    c = make_client(http, secrets, user_token="OLD", access_token="access")
    assert isinstance(await c.open_camera_stream(5), KinesisCredentials)


async def test_neither_block_message(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("POST", ORDER, envelope({"msgid": "m", "token": "opaque"}))
    c = make_client(http, secrets, mode=AuthMode.WEB)
    with pytest.raises(NoCameraCredentialsError, match="No camera credentials"):
        await c.open_camera_stream(5)
    http2 = FakeSession()
    http2.add("POST", ORDER, envelope(None))
    with pytest.raises(NoCameraCredentialsError, match="No camera credentials"):
        await make_client(http2, secrets, mode=AuthMode.WEB).open_camera_stream(5)


async def test_failed_login_keeps_incomplete_message(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", ORDER, kvs_reply(tmpSecretKey=None))
    http.add("POST", EXCHANGE, envelope(None, msg="User does not exist"))
    http.add("GET", USER, envelope(None))
    c = make_client(http, secrets, user_token="OLD", access_token="access")
    with pytest.raises(NoCameraCredentialsError, match="incomplete"):
        await c.open_camera_stream(5)


def test_agora_session_still_imported() -> None:
    assert AgoraCameraSession is not None


# -- D26 and D28: messages -----------------------------------------------------------

SLICE_PARAM_REPLY = {
    "type": "print",
    "action": "getSliceParam",
    "state": "done",
    "code": 200,
    "msg": "",
    "msgid": "x",
    "data": {
        "ams_settings": {"use_ams": True},
        "file_key": "abc123",
        "taskid": "778899",
        "slice_param": {
            "bed_temperature": 60,
            "brim_type": "auto_brim",
            "filament_retract_length": 0.8,
            "filament_type": ";PLA",
            "fill_density": "15%",
            "layer_height": 0.2,
            "paint_infos": [
                {
                    "filament_used": 12.5,
                    "material_type": "PLA",
                    "paint_color": [255, 0, 0],
                    "paint_index": 0,
                },
                {
                    "filament_used": 3.25,
                    "material_type": "PLA",
                    "paint_color": [0, 0, 255],
                    "paint_index": 1,
                },
            ],
            "perimeter_speed": 200,
            "printer_settings_id": "Anycubic Kobra 3",
            "remain_time": 60,
            "size_x": 1,
            "size_y": 2,
            "size_z": 3,
            "support_material_auto": True,
            "temperature": 210,
            "travel_speed": 300,
            "something_new": {"nested": [1, 2]},
        },
    },
}


def test_slice_param_reply_is_read_quietly(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    message = parse_cloud_message(f"{PUBLIC}/print/report", SLICE_PARAM_REPLY)
    assert message is not None
    assert message.understood
    assert message.fault is None
    update = message.update
    assert isinstance(update, PrintUpdate)
    assert update.task_id == 778899
    assert update.slice_param is not None
    infos = update.slice_param.paint_infos
    assert [(i.paint_index, i.paint_color, i.filament_used) for i in infos] == [
        (0, (255, 0, 0), 12.5),
        (1, (0, 0, 255), 3.25),
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


@pytest.mark.parametrize(
    "state", ["initSuccess", "pushStarted", "pushStopped", "pushFailed", "initFailed"]
)
def test_video_reports(state: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    message = parse_cloud_message(
        f"{PUBLIC}/video/report",
        {
            "type": "video",
            "action": "startCapture",
            "state": state,
            "code": 200,
            "data": None,
        },
    )
    assert message is not None
    assert message.understood
    assert message.update == VideoUpdate(state)
    assert message.update.pushing is (state == "pushStarted")
    assert message.update.failed is (state in ("pushFailed", "initFailed"))
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


def test_unknown_video_report() -> None:
    message = parse_cloud_message(
        f"{PUBLIC}/video/report",
        {"type": "video", "action": "startCapture", "state": "warp"},
    )
    assert message is not None
    assert not message.understood


# -- D27: the signalling flow against a fake websocket ----------------------------------

CREDENTIALS = KinesisCredentials(
    region="eu-west-3",
    access_key_id="ASIAFAKEKEYID000",
    secret_access_key="fakeSecretKey/000",
    session_token="fakeSessionToken000",
)
CONTROL = "https://kinesisvideo.eu-west-3.amazonaws.com"
ARN = "arn:aws:kinesisvideo:eu-west-3:111122223333:channel/printerkey/1"
WSS = "wss://v-abcd.kinesisvideo.eu-west-3.amazonaws.com"
OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\n"
ANSWER = "v=0\r\no=- 9 9 IN IP4 0.0.0.0\r\n"


class FakeKvsSocket:
    """A signalling websocket: the test pushes frames, the session sends."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.sent_event = asyncio.Event()

    def push(self, item: Any) -> None:
        self.queue.put_nowait(item)

    def push_message(self, kind: str, body: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"messageType": kind, "senderClientId": ""}
        if body is not None:
            message["messagePayload"] = encode_payload(body)
        self.push(json.dumps(message))

    async def send_str(self, text: str) -> None:
        if self.closed:
            raise aiohttp.ClientConnectionError("closed")
        self.sent.append(json.loads(text))
        self.sent_event.set()

    def actions(self) -> list[str]:
        return [m["action"] for m in self.sent]

    def payloads(self, action: str) -> list[Any]:
        return [
            decode_payload(m["messagePayload"])
            for m in self.sent
            if m["action"] == action
        ]

    def __aiter__(self) -> FakeKvsSocket:
        return self

    async def __anext__(self) -> aiohttp.WSMessage:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, aiohttp.WSMsgType):
            return aiohttp.WSMessage(item, None, None)
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, item, None)

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


class Recorder:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.candidates: list[IceCandidate] = []
        self.errors: list[str] = []
        self.closed: list[str] = []
        self.events: list[str] = []
        self.done = asyncio.Event()

    def answer(self, sdp: str) -> None:
        self.answers.append(sdp)
        self.events.append("answer")

    def candidate(self, candidate: IceCandidate) -> None:
        self.candidates.append(candidate)
        self.events.append("candidate")

    def error(self, message: str) -> None:
        self.errors.append(message)
        self.done.set()

    def close(self, reason: str) -> None:
        self.closed.append(reason)
        self.done.set()


def aws(body: Any, status: int = 200) -> FakeResponse:
    """An AWS JSON reply (read as text, as the session does)."""
    return FakeResponse(text=json.dumps(body), status=status)


def kvs_http(socket: FakeKvsSocket | None = None) -> FakeSession:
    http = FakeSession()
    http.add(
        "POST",
        f"{CONTROL}/describeSignalingChannel",
        aws({"ChannelInfo": {"ChannelARN": ARN, "ChannelName": PRINTER_KEY}}),
    )
    http.add(
        "POST",
        f"{CONTROL}/getSignalingChannelEndpoint",
        aws(
            {
                "ResourceEndpointList": [
                    {"Protocol": "HTTPS", "ResourceEndpoint": "https://r-abcd.example"},
                    {"Protocol": "WSS", "ResourceEndpoint": WSS},
                ]
            }
        ),
    )
    if socket is not None:
        http.ws_factory = lambda _url: socket
    return http


def kvs_session(
    http: FakeSession, recorder: Recorder, **kwargs: Any
) -> KinesisViewerSession:
    options: dict[str, Any] = {
        "push_wait": 0.05,
        "answer_timeout": 0.05,
        "client_id": "ha-test",
        "clock": lambda: DOC_TIME,
    }
    options.update(kwargs)
    return KinesisViewerSession(
        http,  # type: ignore[arg-type]
        CREDENTIALS,
        PRINTER_KEY,
        on_answer=recorder.answer,
        on_candidate=recorder.candidate,
        on_error=recorder.error,
        on_close=recorder.close,
        **options,
    )


async def wait_for(predicate: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110 - polling a plain predicate
            await asyncio.sleep(0.005)


async def test_full_signalling_flow(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    socket = FakeKvsSocket()
    http = kvs_http(socket)
    recorder = Recorder()
    session = kvs_session(http, recorder, push_wait=5)
    # A browser candidate before the offer waits for it.
    session.add_ice_candidate(
        IceCandidate("candidate:1 1 udp 1 192.0.2.1 9 typ host", "0", 0)
    )
    session.add_ice_candidate(IceCandidate(""))  # end of candidates: not sent
    await session.start(OFFER)
    assert session.is_active

    # The control-plane calls: signed JSON, the right bodies.
    describe, endpoint = http.calls[0], http.calls[1]
    assert json.loads(describe.raw_body) == {"ChannelName": PRINTER_KEY}
    assert json.loads(endpoint.raw_body) == {
        "ChannelARN": ARN,
        "SingleMasterChannelEndpointConfiguration": {
            "Protocols": ["WSS", "HTTPS"],
            "Role": "VIEWER",
        },
    }
    for call in (describe, endpoint):
        headers = call.headers
        assert headers["Authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=ASIAFAKEKEYID000/20150830/eu-west-3/"
            "kinesisvideo/aws4_request"
        )
        assert headers["X-Amz-Security-Token"] == "fakeSessionToken000"
        assert headers["Content-Type"] == "application/json"
    url = http.calls[2].url
    query = dict(parse_qsl(urlsplit(url).query))
    assert url.startswith(WSS + "/?")
    assert query["X-Amz-ChannelARN"] == ARN
    assert query["X-Amz-ClientId"] == "ha-test"
    assert query["X-Amz-Expires"] == "299"
    assert query["X-Amz-Security-Token"] == "fakeSessionToken000"

    # Nothing is offered before the printer pushes.
    await asyncio.sleep(0.02)
    assert socket.sent == []
    session.notify_video_state("initSuccess")
    session.notify_video_state("pushStarted")
    await wait_for(lambda: len(socket.sent) >= 2)
    assert socket.actions() == ["SDP_OFFER", "ICE_CANDIDATE"]
    assert socket.payloads("SDP_OFFER") == [{"type": "offer", "sdp": OFFER}]
    assert socket.payloads("ICE_CANDIDATE") == [
        {
            "candidate": "candidate:1 1 udp 1 192.0.2.1 9 typ host",
            "sdpMid": "0",
            "sdpMLineIndex": 0,
        }
    ]

    # A printer candidate before the answer is held until after it.
    socket.push("")  # keep-alive
    socket.push("not json")
    socket.push("[1]")
    socket.push_message(
        "ICE_CANDIDATE",
        {"candidate": "candidate:early", "sdpMid": "0", "sdpMLineIndex": 0},
    )
    socket.push_message("STATUS_RESPONSE", None)
    socket.push(
        json.dumps(
            {"messageType": "STATUS_RESPONSE", "statusResponse": {"statusCode": "200"}}
        )
    )
    socket.push_message("SDP_ANSWER", {"type": "answer", "sdp": ANSWER})
    socket.push_message("SDP_ANSWER", {"type": "answer", "sdp": "again"})
    socket.push_message(
        "ICE_CANDIDATE",
        {"candidate": "candidate:late", "sdpMid": "0", "sdpMLineIndex": 0},
    )
    socket.push_message("ICE_CANDIDATE", {"sdpMid": "0"})  # no candidate: dropped
    socket.push_message("GO_AWAY", {})
    await wait_for(lambda: len(recorder.candidates) == 2)
    assert recorder.answers == [ANSWER]
    assert recorder.events == ["answer", "candidate", "candidate"]
    assert [c.candidate for c in recorder.candidates] == [
        "candidate:early",
        "candidate:late",
    ]
    assert session.answered
    assert session.offers_sent == 1

    # A browser candidate after the offer goes straight out.
    session.add_ice_candidate(IceCandidate("candidate:2", None, None))
    await wait_for(lambda: len(socket.sent) == 3)
    assert socket.payloads("ICE_CANDIDATE")[-1] == {
        "candidate": "candidate:2",
        "sdpMid": None,
        "sdpMLineIndex": None,
    }
    await session.close()
    assert socket.closed
    assert recorder.closed == ["closed"]
    assert recorder.errors == []
    session.add_ice_candidate(IceCandidate("candidate:3"))  # ignored once closed
    await session.close()  # idempotent

    # No credential reaches a log line.
    text = caplog.text
    for secret in ("ASIAFAKEKEYID000", "fakeSecretKey/000", "fakeSessionToken000"):
        assert secret not in text


async def test_offer_goes_without_push_after_the_wait() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(kvs_http(socket), recorder, answer_timeout=5)
    await session.start(OFFER)
    await wait_for(lambda: socket.actions() == ["SDP_OFFER"])
    await session.close()


async def test_offer_resent_while_unanswered_then_fails() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(
        kvs_http(socket), recorder, video_state="pushStarted", offer_attempts=3
    )
    await session.start(OFFER)
    await asyncio.wait_for(recorder.done.wait(), 2)
    assert socket.actions() == ["SDP_OFFER"] * 3
    assert len(recorder.errors) == 1
    assert "did not answer" in recorder.errors[0]
    assert recorder.closed == ["failed"]
    assert not session.is_active


async def test_resend_stops_once_answered() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(
        kvs_http(socket), recorder, video_state="pushStarted", answer_timeout=0.1
    )
    await session.start(OFFER)
    await wait_for(lambda: len(socket.sent) == 1)
    await asyncio.sleep(0.12)  # first wait expires: the offer goes again
    await wait_for(lambda: len(socket.sent) == 2)
    socket.push_message("SDP_ANSWER", {"type": "answer", "sdp": ANSWER})
    await wait_for(lambda: recorder.answers == [ANSWER])
    await asyncio.sleep(0.25)
    assert session.offers_sent == 2
    assert recorder.errors == []
    await session.close()


@pytest.mark.parametrize("state", ["initFailed", "pushFailed"])
async def test_printer_failure_fails_the_stream(state: str) -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(kvs_http(socket), recorder, push_wait=5)
    await session.start(OFFER)
    session.notify_video_state(state)
    await asyncio.wait_for(recorder.done.wait(), 2)
    assert recorder.errors == [f"The printer reported {state}"]
    assert socket.sent == []
    assert socket.closed


async def test_printer_failed_before_start() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(kvs_http(socket), recorder, video_state="initFailed")
    with pytest.raises(KinesisError, match="initFailed"):
        await session.start(OFFER)
    assert socket.closed


async def test_status_response_error() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(
        kvs_http(socket), recorder, video_state="pushStarted", answer_timeout=5
    )
    await session.start(OFFER)
    await wait_for(lambda: len(socket.sent) == 1)
    socket.push(
        json.dumps(
            {
                "messageType": "STATUS_RESPONSE",
                "statusResponse": {
                    "correlationId": "1",
                    "errorType": "InvalidArgumentException",
                    "statusCode": "400",
                    "description": "bad offer from ASIAFAKEKEYID000",
                },
            }
        )
    )
    await asyncio.wait_for(recorder.done.wait(), 2)
    assert recorder.errors == [
        "Amazon Kinesis Video refused the camera: InvalidArgumentException: "
        "bad offer from **REDACTED**"
    ]


def test_status_error_shapes() -> None:
    assert status_error({"statusResponse": {"statusCode": 200}}) is None
    assert status_error({}) is None
    assert status_error({"statusCode": "500"}) == "status 500"
    assert status_error({"statusCode": "x", "errorType": "E"}) == "E"
    assert status_error({"statusCode": None}) is None


async def test_socket_closes_before_answer() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(
        kvs_http(socket), recorder, video_state="pushStarted", answer_timeout=5
    )
    await session.start(OFFER)
    await wait_for(lambda: len(socket.sent) == 1)
    socket.push(None)
    await asyncio.wait_for(recorder.done.wait(), 2)
    assert recorder.errors == ["The camera's signalling connection closed"]


async def test_socket_error_frame_after_answer_just_ends() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(
        kvs_http(socket), recorder, video_state="pushStarted", answer_timeout=5
    )
    await session.start(OFFER)
    socket.push_message("SDP_ANSWER", {"type": "answer", "sdp": ANSWER})
    await wait_for(lambda: recorder.answers == [ANSWER])
    socket.push(aiohttp.WSMsgType.ERROR)
    await asyncio.wait_for(recorder.done.wait(), 2)
    assert recorder.errors == []
    assert recorder.closed == ["signalling closed"]


async def test_socket_raises() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(
        kvs_http(socket), recorder, video_state="pushStarted", answer_timeout=5
    )
    await session.start(OFFER)
    socket.push(aiohttp.ClientConnectionError("reset"))
    await asyncio.wait_for(recorder.done.wait(), 2)
    assert recorder.errors == ["The camera's signalling connection closed"]


async def test_answer_without_sdp_ignored() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = kvs_session(
        kvs_http(socket), recorder, video_state="pushStarted", answer_timeout=5
    )
    await session.start(OFFER)
    socket.push_message("SDP_ANSWER", {"type": "answer"})
    socket.push(json.dumps({"messageType": "SDP_ANSWER", "messagePayload": "!!"}))
    await asyncio.sleep(0.05)
    assert recorder.answers == []
    await session.close()


async def test_send_failure_fails_the_stream() -> None:
    socket = FakeKvsSocket()
    socket.closed = True  # send_str raises
    recorder = Recorder()
    session = kvs_session(kvs_http(socket), recorder, video_state="pushStarted")
    await session.start(OFFER)
    await asyncio.wait_for(recorder.done.wait(), 2)
    assert recorder.errors == ["Could not send the camera offer"]


async def test_aws_refusal_is_aws_message() -> None:
    http = FakeSession()
    http.add(
        "POST",
        f"{CONTROL}/describeSignalingChannel",
        FakeResponse(
            text=json.dumps(
                {"message": "The security token included in the request is expired"}
            ),
            status=403,
        ),
    )
    recorder = Recorder()
    session = kvs_session(http, recorder)
    with pytest.raises(KinesisError) as info:
        await session.start(OFFER)
    assert str(info.value) == (
        "Amazon Kinesis Video refused the camera: "
        "The security token included in the request is expired"
    )
    assert recorder.closed == ["failed"]
    with pytest.raises(KinesisError, match="already been used"):
        await session.start(OFFER)


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (FakeResponse(text="<html>", status=500), "HTTP 500"),
        (FakeResponse(text="[]", status=404), "HTTP 404"),
        (
            FakeResponse(
                text=json.dumps({"Message": "nope ASIAFAKEKEYID000"}), status=400
            ),
            r"nope \*\*REDACTED\*\*",
        ),
        (FakeResponse(text="", status=200), "no signalling channel"),
        (aiohttp.ClientConnectionError("down"), "Could not reach"),
    ],
)
async def test_describe_failures(response: Any, match: str) -> None:
    http = FakeSession()
    http.add("POST", f"{CONTROL}/describeSignalingChannel", response)
    with pytest.raises(KinesisError, match=match):
        await kvs_session(http, Recorder()).start(OFFER)


async def test_no_wss_endpoint() -> None:
    http = FakeSession()
    http.add(
        "POST",
        f"{CONTROL}/describeSignalingChannel",
        aws({"ChannelInfo": {"ChannelARN": ARN}}),
    )
    http.add(
        "POST",
        f"{CONTROL}/getSignalingChannelEndpoint",
        aws(
            {
                "ResourceEndpointList": [
                    None,
                    {"Protocol": "HTTPS", "ResourceEndpoint": "x"},
                ]
            }
        ),
    )
    with pytest.raises(KinesisError, match="no websocket endpoint"):
        await kvs_session(http, Recorder()).start(OFFER)


async def test_websocket_refused() -> None:
    http = kvs_http()

    def refuse(_url: str) -> Any:
        raise aiohttp.WSServerHandshakeError(
            request_info=None,  # type: ignore[arg-type]
            history=(),
            status=403,
        )

    http.ws_factory = refuse
    with pytest.raises(KinesisError, match=r"signalling connection \(403\)"):
        await kvs_session(http, Recorder()).start(OFFER)

    def unreachable(_url: str) -> Any:
        raise aiohttp.ClientConnectionError("down")

    http2 = kvs_http()
    http2.ws_factory = unreachable
    with pytest.raises(KinesisError, match="Could not reach the camera"):
        await kvs_session(http2, Recorder()).start(OFFER)


async def test_bad_inputs() -> None:
    with pytest.raises(KinesisError, match="empty offer"):
        await kvs_session(kvs_http(), Recorder()).start("")
    with pytest.raises(KinesisError, match="unknown AWS region"):
        control_endpoint("eu-west-3.evil.example/")
    assert control_endpoint("ap-southeast-2") == (
        "https://kinesisvideo.ap-southeast-2.amazonaws.com"
    )
    assert decode_payload(None) is None
    assert decode_payload(base64.b64encode(b"[1]").decode()) is None
    assert IceCandidate.from_json({"candidate": "c", "sdpMLineIndex": True}) == (
        IceCandidate("c", None, None)
    )
    assert kinesis_module.new_client_id().startswith("ha-")


async def test_default_clock_and_client_id() -> None:
    socket = FakeKvsSocket()
    recorder = Recorder()
    session = KinesisViewerSession(
        kvs_http(socket),  # type: ignore[arg-type]
        CREDENTIALS,
        PRINTER_KEY,
        on_answer=recorder.answer,
        on_candidate=recorder.candidate,
        on_error=recorder.error,
    )
    assert session.client_id.startswith("ha-")
    await session.start(OFFER)
    await session.close()
    assert session.closed
