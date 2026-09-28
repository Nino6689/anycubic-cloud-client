"""SDP helpers for the Agora signalling (PROTOCOL D §1.7).

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

Changes: no third-party SDP library (a small parser covers what the answer
needs, including ``rtcp-fb`` lines and inline candidates), typed for
``mypy --strict``, and pure functions instead of handler methods.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type JSON = dict[str, Any]

_DIRECTIONS = frozenset({"sendrecv", "sendonly", "recvonly", "inactive"})


def _new_media(value: str) -> JSON:
    parts = value.split()
    return {
        "type": parts[0] if parts else "",
        "port": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 9,
        "protocol": parts[2] if len(parts) > 2 else "",
        "payloads": " ".join(parts[3:]),
        "rtp": [],
        "fmtp": [],
        "rtcpFb": [],
        "ext": [],
        "fingerprints": [],
        "candidates": [],
    }


def _attribute(target: JSON, session: JSON, line: str) -> None:
    name, _, value = line.partition(":")
    if name in _DIRECTIONS:
        target["direction"] = name
        return
    match name:
        case "ice-ufrag":
            target["iceUfrag"] = value
        case "ice-pwd":
            target["icePwd"] = value
        case "setup":
            target["setup"] = value
        case "mid":
            target["mid"] = value
        case "extmap-allow-mixed":
            session["extmapAllowMixed"] = True
        case "fingerprint":
            parts = value.split()
            if len(parts) >= 2:
                target.setdefault("fingerprints", []).append(
                    {"hash": parts[0], "fingerprint": parts[1]}
                )
        case "rtpmap":
            payload, _, codec = value.partition(" ")
            pieces = codec.split("/")
            if payload.isdigit() and pieces[0]:
                target.setdefault("rtp", []).append(
                    {
                        "payload": int(payload),
                        "codec": pieces[0],
                        "rate": int(pieces[1])
                        if len(pieces) > 1 and pieces[1].isdigit()
                        else 90000,
                        "encoding": pieces[2] if len(pieces) > 2 else None,
                    }
                )
        case "fmtp":
            payload, _, config = value.partition(" ")
            if payload.isdigit():
                target.setdefault("fmtp", []).append(
                    {"payload": int(payload), "config": config}
                )
        case "rtcp-fb":
            parts = value.split()
            if len(parts) >= 2 and parts[0].isdigit():
                target.setdefault("rtcpFb", []).append(
                    {
                        "payload": int(parts[0]),
                        "type": parts[1],
                        "subtype": parts[2] if len(parts) > 2 else None,
                    }
                )
        case "extmap":
            parts = value.split()
            if len(parts) >= 2:
                entry = parts[0].split("/", 1)[0]
                if entry.isdigit():
                    target.setdefault("ext", []).append(
                        {"value": int(entry), "uri": parts[1]}
                    )
        case "group":
            parts = value.split()
            if parts:
                session.setdefault("groups", []).append(
                    {"type": parts[0], "mids": " ".join(parts[1:])}
                )
        case "candidate":
            target.setdefault("candidates", []).append(value)


def parse_sdp(sdp: str) -> JSON:
    """Parse the parts of an SDP that the Agora exchange uses."""
    parsed: JSON = {"media": []}
    media: JSON | None = None
    for raw in sdp.splitlines():
        line = raw.strip()
        kind, sep, value = line.partition("=")
        if not sep or len(kind) != 1:
            continue
        if kind == "m":
            media = _new_media(value)
            parsed["media"].append(media)
        elif kind == "a":
            _attribute(media if media is not None else parsed, parsed, value)
        elif kind == "v":
            parsed["version"] = value
    return parsed


def candidate_to_ortc(candidate: str) -> JSON | None:
    """An SDP candidate line as ORTC.

    ``candidate:<foundation> <component> <protocol> <priority> <ip> <port>
    typ <type>``.
    """
    parts = candidate.strip().removeprefix("a=").removeprefix("candidate:").split()
    if len(parts) < 8:
        return None
    try:
        return {
            "foundation": parts[0],
            "ip": parts[4],
            "port": int(parts[5]),
            "priority": int(parts[3]),
            "protocol": parts[2],
            "type": parts[7],
        }
    except ValueError:
        return None


def _fingerprints(entries: Sequence[Mapping[str, Any]]) -> list[JSON]:
    return [
        {"hashFunction": entry.get("hash"), "fingerprint": entry.get("fingerprint")}
        for entry in entries
    ]


def offer_to_ortc(parsed: JSON, candidates: Sequence[str] = ()) -> JSON:
    """The ORTC description ``join_v3`` expects, built from a parsed offer."""
    ice: JSON = {}
    dtls: JSON = {}
    found: list[str] = []
    for media in parsed["media"]:
        if not ice and "iceUfrag" in media:
            ice = {"iceUfrag": media.get("iceUfrag"), "icePwd": media.get("icePwd")}
        if not dtls and media.get("fingerprints"):
            dtls = {"fingerprints": _fingerprints(media["fingerprints"])}
        found.extend(media.get("candidates", []))
    if not ice and "iceUfrag" in parsed:
        ice = {"iceUfrag": parsed.get("iceUfrag"), "icePwd": parsed.get("icePwd")}
    if not dtls and parsed.get("fingerprints"):
        dtls = {"fingerprints": _fingerprints(parsed["fingerprints"])}
    dtls["role"] = "client"
    converted = [
        entry
        for entry in (candidate_to_ortc(c) for c in (*found, *candidates))
        if entry is not None
    ]
    if converted:
        ice["candidates"] = converted

    send: dict[str, list[JSON]] = {
        "audioCodecs": [],
        "audioExtensions": [],
        "videoCodecs": [],
        "videoExtensions": [],
    }
    recv: dict[str, list[JSON]] = {key: [] for key in send}
    for media in parsed["media"]:
        codecs = []
        for rtp in media.get("rtp", []):
            payload = rtp["payload"]
            codec: JSON = {
                "payloadType": payload,
                "rtpMap": {
                    "encodingName": rtp.get("codec"),
                    "clockRate": rtp.get("rate"),
                    "encodingParameters": rtp.get("encoding"),
                },
                "rtcpFeedbacks": [
                    {"type": fb.get("type"), "parameter": fb.get("subtype")}
                    for fb in media.get("rtcpFb", [])
                    if fb.get("payload") == payload
                ],
                "fmtp": {"parameters": {}},
            }
            for fmtp in media.get("fmtp", []):
                if fmtp.get("payload") != payload:
                    continue
                for part in str(fmtp.get("config", "")).split(";"):
                    key, sep, value = part.partition("=")
                    if sep:
                        codec["fmtp"]["parameters"][key.strip()] = value.strip()
            codecs.append(codec)
        extensions = [
            {"entry": ext.get("value"), "extensionName": ext.get("uri")}
            for ext in media.get("ext", [])
        ]
        direction = media.get("direction", "sendrecv")
        targets = (
            [send]
            if direction == "sendonly"
            else [recv]
            if direction == "recvonly"
            else [send, recv]
        )
        for target in targets:
            if media.get("type") == "video":
                target["videoCodecs"].extend(codecs)
                target["videoExtensions"].extend(extensions)
            elif media.get("type") == "audio":
                target["audioCodecs"].extend(codecs)
                target["audioExtensions"].extend(extensions)
    return {
        "iceParameters": ice,
        "dtlsParameters": dtls,
        "rtpCapabilities": {"send": send, "recv": recv},
        "version": "2",
    }


@dataclass(frozen=True, slots=True)
class VideoStream:
    """A publisher's video stream as the gateway announces it."""

    uid: int
    ssrc: int
    rtx_ssrc: int | None = None
    cname: str | None = None


@dataclass(slots=True)
class AnswerInput:
    """Everything the answer SDP is built from."""

    offer: JSON
    ortc: JSON
    extra_fingerprints: list[str] = field(default_factory=list)
    video: VideoStream | None = None


def _answer_direction(offer_direction: str) -> str:
    return {"sendonly": "recvonly", "recvonly": "sendonly", "sendrecv": "sendrecv"}.get(
        offer_direction, "inactive"
    )


def _codec_lines(codecs: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for codec in codecs:
        payload = codec.get("payloadType")
        rtp_map = codec.get("rtpMap") or {}
        name = rtp_map.get("encodingName", "")
        rate = rtp_map.get("clockRate", 90000)
        params = rtp_map.get("encodingParameters")
        lines.append(
            f"a=rtpmap:{payload} {name}/{rate}/{params}"
            if params
            else f"a=rtpmap:{payload} {name}/{rate}"
        )
        for feedback in codec.get("rtcpFeedbacks") or []:
            parameter = feedback.get("parameter")
            kind = feedback.get("type")
            lines.append(
                f"a=rtcp-fb:{payload} {kind} {parameter}"
                if parameter
                else f"a=rtcp-fb:{payload} {kind}"
            )
        parameters = (codec.get("fmtp") or {}).get("parameters") or {}
        if parameters:
            joined = ";".join(f"{key}={value}" for key, value in parameters.items())
            lines.append(f"a=fmtp:{payload} {joined}")
    return lines


def _candidate_lines(candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for index, candidate in enumerate(candidates):
        line = (
            f"a=candidate:{candidate.get('foundation', f'candidate{index}')} 1 "
            f"{candidate.get('protocol', 'udp')} "
            f"{candidate.get('priority', 2103266323)} "
            f"{candidate.get('ip', '')} {candidate.get('port', 0)} "
            f"typ {candidate.get('type', 'host')}"
        )
        if candidate.get("generation") is not None:
            line += f" generation {candidate['generation']}"
        lines.append(line)
    return lines


def _ssrc_lines(video: VideoStream | None) -> list[str]:
    if video is None:
        return []
    cname = video.cname or "agora"
    lines = [
        "a=msid:agora agora-video",
        f"a=ssrc:{video.ssrc} cname:{cname}",
        f"a=ssrc:{video.ssrc} msid:agora agora-video",
        f"a=ssrc:{video.ssrc} mslabel:agora",
        f"a=ssrc:{video.ssrc} label:agora-video",
    ]
    if video.rtx_ssrc is not None:
        lines.append(f"a=ssrc-group:FID {video.ssrc} {video.rtx_ssrc}")
        lines.append(f"a=ssrc:{video.rtx_ssrc} cname:{cname}")
    return lines


def _fingerprint(ortc: Mapping[str, Any], extra: Sequence[str]) -> str:
    for entry in (ortc.get("dtlsParameters") or {}).get("fingerprints") or []:
        value = entry.get("fingerprint")
        if value:
            algorithm = entry.get("hashFunction") or entry.get("algorithm") or "sha-256"
            return f"{algorithm} {value}"
    for value in extra:
        parts = value.split()
        return value if len(parts) == 2 else f"sha-256 {value}"
    return ""


def build_answer_sdp(answer: AnswerInput) -> str | None:
    """The browser's answer, from the gateway's ORTC reply (PROTOCOL D §1.7).

    The gateway is ICE-lite. The publisher's video SSRC is declared when it
    is known, because the browser cannot renegotiate later.
    """
    ortc = answer.ortc
    ice = ortc.get("iceParameters") or {}
    capabilities = ortc.get("rtpCapabilities") or {}
    caps = (
        capabilities.get("sendrecv")
        or capabilities.get("recv")
        or capabilities.get("send")
        or capabilities
    )
    fingerprint = _fingerprint(ortc, answer.extra_fingerprints)
    media_sections = answer.offer.get("media") or []
    if not fingerprint or not media_sections:
        return None
    ufrag = ice.get("iceUfrag") or secrets.token_hex(4)
    pwd = ice.get("icePwd") or secrets.token_hex(16)
    groups = answer.offer.get("groups") or []
    mids = (
        groups[0].get("mids")
        if groups
        else " ".join(str(m.get("mid", i)) for i, m in enumerate(media_sections))
    )
    lines = [
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=AgoraGateway",
        "t=0 0",
        f"a=group:BUNDLE {mids}",
        "a=ice-lite",
    ]
    if answer.offer.get("extmapAllowMixed"):
        lines.append("a=extmap-allow-mixed")
    lines.append("a=msid-semantic: WMS")
    candidates = _candidate_lines(ice.get("candidates") or [])
    for index, media in enumerate(media_sections):
        kind = media.get("type", "audio")
        codecs = caps.get(f"{kind}Codecs") or []
        answer_ext = caps.get(f"{kind}Extensions") or []
        offer_ext = {ext.get("uri"): ext.get("value") for ext in media.get("ext", [])}
        payloads = " ".join(str(c.get("payloadType")) for c in codecs) or media.get(
            "payloads", ""
        )
        lines += [
            f"m={kind} 9 UDP/TLS/RTP/SAVPF {payloads}",
            "c=IN IP4 127.0.0.1",
            "a=rtcp:9 IN IP4 0.0.0.0",
            f"a=ice-ufrag:{ufrag}",
            f"a=ice-pwd:{pwd}",
            "a=ice-options:trickle",
            f"a=fingerprint:{fingerprint}",
            "a=setup:active",
            f"a=mid:{media.get('mid', str(index))}",
        ]
        lines += candidates
        lines += [
            f"a=extmap:{offer_ext[name]} {name}"
            for ext in answer_ext
            if (name := ext.get("extensionName")) in offer_ext
        ]
        lines += [
            f"a={_answer_direction(media.get('direction', 'sendonly'))}",
            "a=rtcp-mux",
            "a=rtcp-rsize",
        ]
        lines += _codec_lines(codecs)
        if kind == "video":
            lines += _ssrc_lines(answer.video)
    return "\r\n".join(lines) + "\r\n"
