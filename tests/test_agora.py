"""Agora signalling (PROTOCOL D §1.7) against a fake access point and gateway."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import aiohttp
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from anycubic_cloud_client import AgoraCameraSession, AgoraError, CameraCredentials
from anycubic_cloud_client import agora as agora_module
from anycubic_cloud_client.agora import (
    build_ap_request,
    check_salt,
    choose_server,
    encryption_fields,
    find_video_streams,
    open_camera_session,
    parse_ap_reply,
    wrap_channel_secret,
)
from anycubic_cloud_client.agora_sdp import (
    AnswerInput,
    VideoStream,
    build_answer_sdp,
    candidate_to_ortc,
    offer_to_ortc,
    parse_sdp,
)

from .conftest import Call, FakeResponse, FakeSession, aiohttp_session
from .payloads import CAMERA_REPLY

OFFER = "\r\n".join(
    [
        "v=0",
        "o=- 4611731400430051336 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0 1",
        "a=extmap-allow-mixed",
        "a=msid-semantic: WMS",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "c=IN IP4 0.0.0.0",
        "a=ice-ufrag:ufrag1",
        "a=ice-pwd:pwd1pwd1pwd1pwd1pwd1pwd1",
        "a=ice-options:trickle",
        "a=fingerprint:sha-256 AA:BB:CC",
        "a=setup:actpass",
        "a=mid:0",
        "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level",
        "a=recvonly",
        "a=rtcp-mux",
        "a=rtpmap:111 opus/48000/2",
        "a=rtcp-fb:111 transport-cc",
        "a=fmtp:111 minptime=10;useinbandfec=1",
        "a=candidate:1 1 udp 2122260223 192.0.2.10 50000 typ host generation 0",
        "m=video 9 UDP/TLS/RTP/SAVPF 96 97",
        "c=IN IP4 0.0.0.0",
        "a=ice-ufrag:ufrag1",
        "a=ice-pwd:pwd1pwd1pwd1pwd1pwd1pwd1",
        "a=fingerprint:sha-256 AA:BB:CC",
        "a=setup:actpass",
        "a=mid:1",
        "a=extmap:3 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
        "a=extmap:4/sendrecv urn:3gpp:video-orientation",
        "a=recvonly",
        "a=rtcp-mux",
        "a=rtpmap:96 H264/90000",
        "a=rtcp-fb:96 nack",
        "a=rtcp-fb:96 nack pli",
        "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
        "a=rtpmap:97 rtx/90000",
        "a=fmtp:97 apt=96",
        "a=ssrc:1 cname:x",
        "",
    ]
)


def credentials(**changes: Any) -> CameraCredentials:
    data = json.loads(json.dumps(CAMERA_REPLY["data"]))
    data["shengwang"].update(changes)
    result = CameraCredentials.from_data(data)
    assert result is not None
    return result


@pytest.fixture(scope="module")
def sdk_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=1024)


def pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


# -- encryption fields -------------------------------------------------------------------


def test_encryption_fields(sdk_key: rsa.RSAPrivateKey) -> None:
    fields = encryption_fields(credentials(), pem(sdk_key))
    assert list(fields) == ["aes_mode", "aes_secret", "aes_encrypt", "aes_salt"]
    assert fields["aes_mode"] == "aes-256-gcm2"
    assert fields["aes_encrypt"] is True
    assert (
        fields["aes_salt"] == CAMERA_REPLY["data"]["shengwang"]["encryption_kdf_salt"]
    )
    plain = sdk_key.decrypt(
        base64.b64decode(fields["aes_secret"]),
        padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
        ),
    )
    assert plain == b"0123456789abcdef0123456789abcdef"  # raw UTF-8, never hex-decoded
    assert "aesmode" not in fields
    assert "aespassword" not in fields


def test_open_channel_has_no_encryption_fields() -> None:
    assert encryption_fields(credentials(encryption_mode="none"), None) == {}
    assert encryption_fields(credentials(encryption_mode=""), None) == {}


def test_encryption_errors(sdk_key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(AgoraError, match="SDK public key"):
        encryption_fields(credentials(), None)
    with pytest.raises(AgoraError, match="key or salt"):
        encryption_fields(credentials(encryption_key=""), pem(sdk_key))
    with pytest.raises(AgoraError, match="not base64"):
        check_salt("***")
    with pytest.raises(AgoraError, match="32 bytes"):
        check_salt(base64.b64encode(b"short").decode())
    with pytest.raises(AgoraError, match="does not parse"):
        wrap_channel_secret("k", b"garbage")
    ec_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    with pytest.raises(AgoraError, match="RSA"):
        wrap_channel_secret("k", ec_pem)


# -- access point -------------------------------------------------------------------------


def ap_reply(**buffer_changes: Any) -> dict[str, Any]:
    buffer = {
        "code": 0,
        "flag": 4096,
        "uid": 6001,
        "cid": 77,
        "cname": "channel",
        "cert": "TICKET",
        "detail": {"19": "sha-256 11:22;sha-256 33:44", "23": "region"},
        "edges_services": [
            {"ip": "203.0.113.5", "port": 4701},
            {"ip": "203.0.113.6", "port": 4702},
        ],
    }
    buffer.update(buffer_changes)
    return {
        "enter_ts": 1790000000000,
        "opid": 5,
        "detail": {"base": 1},
        "response_body": [
            {"buffer": {"code": 1}},
            {"buffer": {"code": 0, "flag": 4194310, "edges_services": []}},
            {"buffer": buffer},
        ],
    }


def test_build_ap_request() -> None:
    request = build_ap_request(credentials(), sid="SID")
    assert request["appid"] == "FAKEAGORAAPPID"
    assert request["sid"] == "SID"
    (body,) = request["request_bodies"]
    assert body["uri"] == 22
    buffer = body["buffer"]
    assert buffer["cname"] == CAMERA_REPLY["data"]["shengwang"]["channel"]
    assert buffer["key"] == "007fakertctoken"
    assert buffer["uid"] == 6001
    assert buffer["service_ids"] == [11, 26]
    assert buffer["detail"] == {
        "6": "6001",
        "11": "CN,GLOBAL",
        "17": "1",
        "22": "CN,GLOBAL",
    }


def test_parse_ap_reply() -> None:
    reply = parse_ap_reply(ap_reply())
    assert reply.cid == 77
    assert reply.uid == 6001
    assert reply.flag == 4096
    first, second = reply.gateways
    assert first.url == "wss://203-0-113-5.edge.agora.io:4701"
    assert first.fingerprint == "sha-256 11:22"
    assert second.fingerprint == "sha-256 33:44"
    assert first.ticket == "TICKET"
    ap = reply.ap_response(first)
    assert ap["cert"] == ap["ticket"] == "TICKET"
    assert ap["detail"]["base"] == 1
    assert ap["server_ts"] == 1790000000000
    edge_ticket = parse_ap_reply(
        ap_reply(
            edges_services=[{"ip": "1.2.3.4", "port": 1, "ticket": "OWN"}, {"ip": ""}]
        )
    )
    assert [g.ticket for g in edge_ticket.gateways] == ["OWN"]


@pytest.mark.parametrize(
    "reply",
    [{"response_body": [{"buffer": {"code": 3}}, "junk"]}, ap_reply(edges_services=[])],
)
def test_parse_ap_reply_errors(reply: dict[str, Any]) -> None:
    with pytest.raises(AgoraError):
        parse_ap_reply(reply)


AP1 = "https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2"
AP2 = "https://webrtc2-2.ap.sd-rtn.com/api/v2/transpond/webrtc?v=2"
AP3 = "https://webrtc2-ap-web-3.agora.io/api/v2/transpond/webrtc?v=2"
AP4 = "https://webrtc2-4.ap.sd-rtn.com/api/v2/transpond/webrtc?v=2"


class SlowResponse(FakeResponse):
    async def json(self, content_type: str | None = "application/json") -> Any:
        await asyncio.sleep(10)
        return {}


@pytest.fixture
def fast_ap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agora_module, "AP_BACKUP_DELAY", 0.01)


async def test_choose_server_first_answer(http: FakeSession, fast_ap: None) -> None:
    http.add("POST", AP1, SlowResponse())
    http.add("POST", AP2, ap_reply())
    reply = await choose_server(aiohttp_session(http), credentials())
    assert reply.cid == 77
    call = http.calls_to(AP2)[0]
    assert isinstance(call.kwargs["data"], aiohttp.FormData)
    assert call.kwargs["timeout"].total == 10


async def test_choose_server_backups(http: FakeSession, fast_ap: None) -> None:
    http.add("POST", AP1, FakeResponse(status=500))
    http.add("POST", AP2, FakeResponse([1]))
    http.add("POST", AP3, aiohttp.ClientConnectionError())
    http.add("POST", AP4, ap_reply())
    reply = await choose_server(aiohttp_session(http), credentials())
    assert reply.gateways


async def test_choose_server_backups_after_delay(
    http: FakeSession, fast_ap: None
) -> None:
    http.add("POST", AP1, SlowResponse())
    http.add("POST", AP2, SlowResponse())
    http.add("POST", AP3, ap_reply())
    http.add("POST", AP4, SlowResponse())
    assert (await choose_server(aiohttp_session(http), credentials())).cid == 77


async def test_choose_server_all_fail(http: FakeSession, fast_ap: None) -> None:
    for url in (AP1, AP2, AP3, AP4):
        http.add("POST", url, FakeResponse(status=403))
    with pytest.raises(AgoraError, match="answered 403"):
        await choose_server(aiohttp_session(http), credentials())


async def test_choose_server_timeout(
    http: FakeSession, fast_ap: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agora_module, "AP_TOTAL_TIMEOUT", 0.05)
    for url in (AP1, AP2, AP3, AP4):
        http.add("POST", url, SlowResponse())
    with pytest.raises(AgoraError, match="in time"):
        await choose_server(aiohttp_session(http), credentials())


# -- SDP ----------------------------------------------------------------------------------


def test_parse_offer_and_ortc() -> None:
    parsed = parse_sdp(OFFER)
    audio, video = parsed["media"]
    assert parsed["extmapAllowMixed"] is True
    assert parsed["groups"] == [{"type": "BUNDLE", "mids": "0 1"}]
    assert audio["direction"] == "recvonly"
    assert video["mid"] == "1"
    assert video["rtp"][0] == {
        "payload": 96,
        "codec": "H264",
        "rate": 90000,
        "encoding": None,
    }
    assert audio["rtp"][0]["encoding"] == "2"
    assert video["ext"][1] == {"value": 4, "uri": "urn:3gpp:video-orientation"}
    ortc = offer_to_ortc(parsed, ["candidate:9 1 tcp 5 198.51.100.1 9 typ host", "bad"])
    assert ortc["iceParameters"]["iceUfrag"] == "ufrag1"
    assert [c["ip"] for c in ortc["iceParameters"]["candidates"]] == [
        "192.0.2.10",
        "198.51.100.1",
    ]
    assert ortc["dtlsParameters"] == {
        "fingerprints": [{"hashFunction": "sha-256", "fingerprint": "AA:BB:CC"}],
        "role": "client",
    }
    recv = ortc["rtpCapabilities"]["recv"]
    h264 = recv["videoCodecs"][0]
    assert h264["rtcpFeedbacks"] == [
        {"type": "nack", "parameter": None},
        {"type": "nack", "parameter": "pli"},
    ]
    assert h264["fmtp"]["parameters"]["packetization-mode"] == "1"
    assert recv["audioExtensions"][0]["entry"] == 1
    assert ortc["rtpCapabilities"]["send"]["videoCodecs"] == []


def test_session_level_ice_and_directions() -> None:
    sdp = "\n".join(
        [
            "v=0",
            "a=ice-ufrag:top",
            "a=ice-pwd:toppwd",
            "a=fingerprint:sha-256 FF",
            "m=video 9 X 96",
            "a=rtpmap:96 VP8/90000",
            "a=sendonly",
            "m=audio 9 X 0",
            "a=rtpmap:0 PCMU/8000",
            "a=inactive",
            "m=application 9 X 5",
            "a=rtpmap:x broken",
            "a=fmtp:x",
            "a=rtcp-fb:x y",
            "a=extmap:z foo",
            "a=extmap:onlyone",
            "a=fingerprint:onlyone",
            "garbage",
            "xx=nope",
        ]
    )
    parsed = parse_sdp(sdp)
    ortc = offer_to_ortc(parsed)
    assert ortc["iceParameters"] == {"iceUfrag": "top", "icePwd": "toppwd"}
    assert ortc["dtlsParameters"]["fingerprints"][0]["fingerprint"] == "FF"
    assert ortc["rtpCapabilities"]["send"]["videoCodecs"][0]["payloadType"] == 96
    assert ortc["rtpCapabilities"]["recv"]["videoCodecs"] == []
    assert (
        ortc["rtpCapabilities"]["recv"]["audioCodecs"][0]["rtpMap"]["clockRate"] == 8000
    )
    assert parse_sdp("m=")["media"][0]["type"] == ""


def test_candidate_conversion() -> None:
    assert candidate_to_ortc("a=candidate:1 1 udp 5 1.2.3.4 99 typ srflx") == {
        "foundation": "1",
        "ip": "1.2.3.4",
        "port": 99,
        "priority": 5,
        "protocol": "udp",
        "type": "srflx",
    }
    assert candidate_to_ortc("candidate:1 1 udp x 1.2.3.4 99 typ host") is None
    assert candidate_to_ortc("short") is None


GATEWAY_ORTC = {
    "iceParameters": {
        "iceUfrag": "gw",
        "icePwd": "gwpwd",
        "candidates": [
            {
                "foundation": "f",
                "ip": "203.0.113.5",
                "port": 4701,
                "priority": 1,
                "protocol": "udp",
                "type": "host",
                "generation": 0,
            },
            {},
        ],
    },
    "dtlsParameters": {
        "fingerprints": [{"hashFunction": "sha-256", "fingerprint": "DE:AD"}]
    },
    "rtpCapabilities": {
        "recv": {
            "audioCodecs": [
                {
                    "payloadType": 111,
                    "rtpMap": {
                        "encodingName": "opus",
                        "clockRate": 48000,
                        "encodingParameters": 2,
                    },
                }
            ],
            "videoCodecs": [
                {
                    "payloadType": 96,
                    "rtpMap": {"encodingName": "H264", "clockRate": 90000},
                    "rtcpFeedbacks": [
                        {"type": "nack"},
                        {"type": "nack", "parameter": "pli"},
                    ],
                    "fmtp": {"parameters": {"packetization-mode": "1"}},
                }
            ],
            "videoExtensions": [
                {
                    "entry": 3,
                    "extensionName": "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
                },
                {"entry": 9, "extensionName": "urn:unknown"},
            ],
        }
    },
}


def test_answer_sdp() -> None:
    answer = build_answer_sdp(
        AnswerInput(
            offer=parse_sdp(OFFER),
            ortc=GATEWAY_ORTC,
            video=VideoStream(uid=5002, ssrc=1111, rtx_ssrc=2222, cname="pub"),
        )
    )
    assert answer is not None
    assert answer.endswith("\r\n")
    lines = answer.split("\r\n")
    assert lines[:6] == [
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=AgoraGateway",
        "t=0 0",
        "a=group:BUNDLE 0 1",
        "a=ice-lite",
    ]
    assert "a=extmap-allow-mixed" in lines
    assert "m=audio 9 UDP/TLS/RTP/SAVPF 111" in lines
    assert "m=video 9 UDP/TLS/RTP/SAVPF 96" in lines
    assert "a=fingerprint:sha-256 DE:AD" in lines
    assert "a=setup:active" in lines
    assert "a=sendonly" in lines  # answers the browser's recvonly
    assert "a=rtpmap:111 opus/48000/2" in lines
    assert "a=rtcp-fb:96 nack pli" in lines
    assert "a=rtcp-fb:96 nack" in lines
    assert "a=fmtp:96 packetization-mode=1" in lines
    assert (
        "a=extmap:3 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time" in lines
    )
    assert not any("urn:unknown" in line for line in lines)
    assert "a=candidate:f 1 udp 1 203.0.113.5 4701 typ host generation 0" in lines
    assert "a=ssrc:1111 cname:pub" in lines
    assert "a=ssrc-group:FID 1111 2222" in lines


def test_answer_sdp_fallbacks() -> None:
    ortc = {"iceParameters": {}, "dtlsParameters": {}, "rtpCapabilities": {}}
    offer = parse_sdp("v=0\nm=video 9 X 96\na=sendrecv\nm=audio 9 X 0\na=inactive\n")
    answer = build_answer_sdp(
        AnswerInput(
            offer=offer,
            ortc=ortc,
            extra_fingerprints=["AB:CD"],
            video=VideoStream(uid=1, ssrc=5),
        )
    )
    assert answer is not None
    assert "a=fingerprint:sha-256 AB:CD" in answer
    assert "a=group:BUNDLE 0 1" in answer
    assert "a=sendrecv" in answer
    assert "a=inactive" in answer
    assert "a=ssrc:5 cname:agora" in answer
    assert "FID" not in answer
    full = build_answer_sdp(
        AnswerInput(offer=offer, ortc=ortc, extra_fingerprints=["sha-1 EE"])
    )
    assert full is not None
    assert "a=fingerprint:sha-1 EE" in full
    assert build_answer_sdp(AnswerInput(offer=offer, ortc=ortc)) is None
    assert build_answer_sdp(AnswerInput(offer={"media": []}, ortc=GATEWAY_ORTC)) is None


def test_find_video_streams() -> None:
    payload = {
        "users": [
            {"uid": 5002, "ssrcId": 1111, "video": True, "cname": "c"},
            {"uid": 5002, "ssrcId": 1111, "video": True, "cname": "c"},
            {"uid": 7, "ssrcId": 8, "stream_type": "audio"},
            {"nested": [{"uid": 9, "ssrcId": 10, "rtxSsrcId": 11}]},
            {"uid": "x", "ssrcId": 1, "video": True},
        ]
    }
    streams = find_video_streams(payload)
    assert streams == [
        VideoStream(uid=5002, ssrc=1111, rtx_ssrc=None, cname="c"),
        VideoStream(uid=9, ssrc=10, rtx_ssrc=11, cname=None),
    ]


# -- the gateway session ----------------------------------------------------------------


class FakeWS:
    def __init__(self, script: list[Any]) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        for item in script:
            self.queue.put_nowait(item)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def push(self, item: Any) -> None:
        self.queue.put_nowait(item)

    async def send_str(self, text: str) -> None:
        if self.closed:
            raise aiohttp.ClientConnectionError("closed")
        self.sent.append(json.loads(text))

    async def receive(self) -> aiohttp.WSMessage:
        item = await self.queue.get()
        if item is None:
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, item, None)
        if isinstance(item, aiohttp.WSMsgType):
            return aiohttp.WSMessage(item, b"", None)
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(item), None)

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)

    def types(self) -> list[str]:
        return [m["_type"] for m in self.sent]


def joined(**extra: Any) -> dict[str, Any]:
    return {
        "_id": "1",
        "_result": "success",
        "_message": {"ortc": GATEWAY_ORTC, **extra},
    }


STREAM = {
    "_type": "on_add_video_stream",
    "_message": {
        "uid": 5002,
        "ssrcId": 1111,
        "rtxSsrcId": 2222,
        "cname": "pub",
        "video": True,
    },
}


def gateway_session(
    http: FakeSession, scripts: list[Any], **kwargs: Any
) -> tuple[AgoraCameraSession, list[FakeWS]]:
    sockets: list[FakeWS] = []
    remaining = list(scripts)

    def factory(url: str) -> FakeWS:
        script = remaining.pop(0)
        if isinstance(script, BaseException):
            raise script
        ws = FakeWS(script)
        sockets.append(ws)
        return ws

    http.ws_factory = factory
    http.add("POST", AP1, ap_reply())
    http.add("POST", AP2, ap_reply())
    session = AgoraCameraSession(
        aiohttp_session(http), credentials(encryption_mode="none"), **kwargs
    )
    return session, sockets


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agora_module, "PING_INTERVAL", 0.01)
    monkeypatch.setattr(agora_module, "VIDEO_STREAM_WAIT", 0.05)


async def test_full_session(http: FakeSession, quick: None) -> None:
    closed: list[str] = []
    session, sockets = gateway_session(
        http,
        [
            [
                {"_type": "on_user_online", "_message": {"uid": 5002}},
                "not json",
                "[1]",
                joined(users=[{"uid": 5002, "ssrcId": 1111, "video": True}]),
            ]
        ],
        on_close=closed.append,
    )
    session.add_ice_candidate("candidate:9 1 udp 5 198.51.100.7 9 typ host")
    session.add_ice_candidate("")
    answer = await session.answer(OFFER, "ha-session-1")
    assert "a=ssrc:1111 cname:agora" in answer
    assert session.is_active
    ws = sockets[0]
    join = ws.sent[0]
    assert join["_type"] == "join_v3"
    body = join["_message"]
    assert body["session_id"] == "ha-session-1"
    assert body["channel_key"] == "007fakertctoken"
    assert body["sdk_version"] == "4.24.0"
    assert body["role"] == "host"
    assert body["mode"] == "live"
    assert body["codec"] == "h264"
    assert body["ap_response"]["cert"] == "TICKET"
    assert "aes_mode" not in body
    assert body["ortc"]["iceParameters"]["candidates"][-1]["ip"] == "198.51.100.7"
    assert ws.sent[1]["_type"] == "set_client_role"
    assert ws.sent[1]["_message"]["role"] == "host"
    assert ws.sent[1]["_message"]["level"] == 0
    subscribe = ws.sent[2]
    assert subscribe["_type"] == "subscribe"
    assert subscribe["_message"]["stream_id"] == 5002
    assert subscribe["_message"]["ssrcId"] == 1111
    assert subscribe["_message"]["twcc"] is True
    assert subscribe["_message"]["rtx"] is True
    session.add_ice_candidate("candidate:late")  # dropped after the join
    await asyncio.sleep(0.05)
    assert "ping" in ws.types()
    ws.push({"_type": "on_token_privilege_will_expire"})
    ws.push({"_type": "on_remove_video_stream"})
    ws.push({"_type": "error", "_message": {"code": 17}})
    ws.push(STREAM)  # already subscribed: no second subscribe
    ws.push({"_type": "on_token_privilege_did_expire"})
    for _ in range(50):
        if closed:
            break
        await asyncio.sleep(0.01)
    assert closed == ["the camera token expired"]
    assert ws.closed
    assert not session.is_active
    assert ws.types().count("subscribe") == 1
    await session.close()
    with pytest.raises(AgoraError, match="already been used"):
        await session.answer(OFFER, "again")


async def test_waits_for_the_announced_stream(http: FakeSession, quick: None) -> None:
    session, _sockets = gateway_session(
        http,
        [
            [
                joined(),
                {"_type": "on_user_offline"},
                {
                    "_type": "on_add_video_stream",
                    "_message": {"uid": 9999, "ssrcId": 1, "video": True},
                },
                {
                    "_type": "on_add_video_stream",
                    "_message": {"uid": 5002, "video": False},
                },
                STREAM,
            ]
        ],
    )
    answer = await session.answer(OFFER, "s")
    assert "a=ssrc-group:FID 1111 2222" in answer
    assert "a=ssrc:1 " not in answer  # another publisher's stream was ignored
    await session.close()


async def test_answer_without_a_stream_after_8s(
    http: FakeSession, quick: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="anycubic_cloud_client")
    session, _ = gateway_session(http, [[joined()]])
    answer = await session.answer(OFFER, "s")
    assert "a=ssrc:" not in answer
    assert "not publishing yet" in caplog.text
    await session.close()


async def test_next_gateway_after_failures(http: FakeSession, quick: None) -> None:
    session, sockets = gateway_session(
        http,
        [aiohttp.ClientConnectionError("refused"), [joined(), STREAM]],
    )
    assert await session.answer(OFFER, "s")
    assert len(sockets) == 1
    await session.close()


async def test_all_gateways_fail(http: FakeSession, quick: None) -> None:
    closed: list[str] = []
    session, sockets = gateway_session(
        http,
        [[{"_result": "failed", "_message": "bad token"}], [None]],
        on_close=closed.append,
    )
    with pytest.raises(AgoraError, match="No Agora gateway accepted"):
        await session.answer(OFFER, "s")
    assert all(ws.closed for ws in sockets)
    assert closed == ["closed"]


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ({"_type": "on_crypt_error"}, "key or salt"),
        ({"_type": "error", "_message": {"error_code": 2028}}, "2028"),
        ({"_type": "error", "_message": {"code": 2024}}, "2024"),
        ({"_type": "on_p2p_lost"}, "lost"),
    ],
)
async def test_fatal_events_during_join(
    http: FakeSession, quick: None, event: dict[str, Any], message: str
) -> None:
    session, sockets = gateway_session(http, [[event], [joined()]])
    with pytest.raises(AgoraError, match=message):
        await session.answer(OFFER, "s")
    assert len(sockets) == 1  # no other gateway is tried
    assert not session.is_active


async def test_join_reply_problems(http: FakeSession, quick: None) -> None:
    bad_ortc = {"_result": "success", "_message": {"ortc": "x"}}
    no_answer = {"_result": "success", "_message": {"ortc": {"iceParameters": {}}}}
    session, _ = gateway_session(http, [[bad_ortc], [no_answer, STREAM]])
    no_fingerprints = ap_reply(detail={})
    http.routes[("POST", AP1)] = [no_fingerprints]
    http.routes[("POST", AP2)] = [no_fingerprints]
    with pytest.raises(AgoraError, match="Could not build the SDP answer"):
        await session.answer(OFFER, "s")


async def test_socket_closed_while_waiting_for_the_stream(
    http: FakeSession, quick: None
) -> None:
    session, _ = gateway_session(
        http, [[joined(), aiohttp.WSMsgType.ERROR], [joined(), None]]
    )
    with pytest.raises(AgoraError, match="No Agora gateway accepted"):
        await session.answer(OFFER, "s")


async def test_offer_without_media(http: FakeSession) -> None:
    session = AgoraCameraSession(
        aiohttp_session(http), credentials(encryption_mode="none")
    )
    with pytest.raises(AgoraError, match="no media"):
        await session.answer("v=0\r\n", "s")
    assert http.calls == []


async def test_encrypted_join_carries_the_four_fields(
    http: FakeSession, quick: None, sdk_key: rsa.RSAPrivateKey
) -> None:
    sockets: list[FakeWS] = []

    def factory(url: str) -> FakeWS:
        ws = FakeWS([joined(), STREAM])
        sockets.append(ws)
        return ws

    http.ws_factory = factory
    http.add("POST", AP1, ap_reply())
    http.add("POST", AP2, ap_reply())
    session, answer = await open_camera_session(
        aiohttp_session(http),
        credentials(),
        OFFER,
        "s",
        candidates=["candidate:1 1 udp 5 192.0.2.1 9 typ host"],
        sdk_public_key_pem=pem(sdk_key),
    )
    assert answer
    body = sockets[0].sent[0]["_message"]
    assert body["aes_mode"] == "aes-256-gcm2"
    assert body["aes_encrypt"] is True
    assert "aes_secret" in body
    assert "aes_salt" in body
    await session.close()


async def test_message_loop_socket_error(http: FakeSession, quick: None) -> None:
    closed: list[str] = []
    session, sockets = gateway_session(
        http, [[joined(), STREAM]], on_close=closed.append
    )
    await session.answer(OFFER, "s")
    sockets[0].push(aiohttp.ClientConnectionError("gone"))
    for _ in range(50):
        if closed:
            break
        await asyncio.sleep(0.01)
    assert closed == ["socket error: ClientConnectionError"]


async def test_gateway_closes_after_join(http: FakeSession, quick: None) -> None:
    closed: list[str] = []
    session, sockets = gateway_session(
        http, [[joined(), STREAM]], on_close=closed.append
    )
    await session.answer(OFFER, "s")
    sockets[0].push(None)
    for _ in range(50):
        if closed:
            break
        await asyncio.sleep(0.01)
    assert closed == ["the gateway closed the socket"]


def test_call_helper_path() -> None:
    assert Call("GET", "https://x/y", {}).path == "https://x/y"
