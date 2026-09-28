"""Cloud MQTT: identity, TLS, topics, routing and the link lifecycle (PROTOCOL C)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
from typing import Any

import bcrypt
import paho.mqtt.client as mqtt
import pytest
from cryptography.hazmat.primitives.asymmetric import padding
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from anycubic_cloud_client import (
    Account,
    AuthMode,
    CloudMessage,
    CloudMqttClient,
    CloudSecrets,
    MqttAuthError,
    MqttConnectionError,
    MqttError,
    MqttNotAllowedError,
    MqttNotConnectedError,
    Region,
    build_identity,
    mqtt_client_id,
    parse_topic,
    printer_topics,
    redact_topic,
    user_topics,
)
from anycubic_cloud_client.mqtt_identity import (
    android_password,
    build_tls_context,
    slicer_password,
)
from anycubic_cloud_client.signing import md5_hex

from .conftest import FakeSession, Material, make_client
from .payloads import MACHINE_TYPE, PRINTER_KEY
from .payloads import mqtt as message

EMAIL = "someone@example.invalid"
ACCOUNT = Account(user_id=424242, email=EMAIL, mobile=None)

# -- identity (PROTOCOL A §2.12, C §1.4-§1.5) ------------------------------------------


def test_client_ids() -> None:
    assert mqtt_client_id(EMAIL, AuthMode.SLICER) == md5_hex(EMAIL + "pcf")
    assert mqtt_client_id(EMAIL, AuthMode.ANDROID) == md5_hex(EMAIL)


def test_slicer_identity(secrets: CloudSecrets, material: Material) -> None:
    identity = build_identity(AuthMode.SLICER, "USER-TOKEN", ACCOUNT, secrets)
    assert identity.client_id == md5_hex(EMAIL + "pcf")
    plain = material.ca_key.decrypt(
        base64.b64decode(identity.password), padding.PKCS1v15()
    )
    assert plain == b"USER-TOKEN"
    role, sep_user, sig = identity.username.split("|")[1:]
    assert identity.username.startswith("user|pcf|")
    assert (role, sep_user) == ("pcf", EMAIL)
    assert sig == md5_hex(identity.client_id + identity.password + identity.client_id)
    assert "USER-TOKEN" not in repr(identity)
    assert identity.password not in repr(identity)
    # randomised: a new password every time
    again = build_identity(AuthMode.SLICER, "USER-TOKEN", ACCOUNT, secrets)
    assert again.password != identity.password


def test_android_identity(secrets: CloudSecrets) -> None:
    account = Account(user_id=7, email=None, mobile="+000")
    identity = build_identity(AuthMode.ANDROID, "TOK", account, secrets)
    assert identity.client_id == md5_hex("+000")
    assert identity.username.startswith("user|app|+000|")
    assert identity.password.startswith("$2b$12$")
    assert len(identity.password) == 60
    assert bcrypt.checkpw(md5_hex("TOK").encode(), identity.password.encode())
    assert android_password("TOK") != android_password("TOK")


@pytest.mark.parametrize(
    ("mode", "token", "account", "message_part"),
    [
        (AuthMode.WEB, "t", ACCOUNT, "Web-mode"),
        (AuthMode.SLICER, None, ACCOUNT, "user token"),
        (AuthMode.SLICER, "t", None, "account"),
        (AuthMode.SLICER, "t", Account(user_id=1), "neither an email nor a mobile"),
    ],
)
def test_identity_refusals(
    secrets: CloudSecrets,
    mode: AuthMode,
    token: str | None,
    account: Account | None,
    message_part: str,
) -> None:
    with pytest.raises(MqttNotAllowedError, match=message_part):
        build_identity(mode, token, account, secrets)


def test_token_too_long_for_one_rsa_block(secrets: CloudSecrets) -> None:
    with pytest.raises(MqttNotAllowedError, match="RSA block"):
        slicer_password("x" * 400, secrets)


def test_tls_context(secrets: CloudSecrets) -> None:
    context = build_tls_context(secrets, Region.INTERNATIONAL.endpoints)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT
    assert len(context.get_ca_certs()) == 1  # only the pinned CA
    china = build_tls_context(secrets, Region.CHINA.endpoints)
    assert china.check_hostname is False
    assert china.verify_mode == ssl.CERT_REQUIRED


# -- topics -------------------------------------------------------------------------------


def test_topics() -> None:
    u1, u2 = user_topics(424242)
    user_md5 = md5_hex("424242")
    assert u1 == f"anycubic/anycubicCloud/v1/server/app/424242/{user_md5}/slice/report"
    assert (
        u2 == f"anycubic/anycubicCloud/v1/server/app/424242/{user_md5}/fdmslice/report"
    )
    p1, p2 = printer_topics(20025, "KEY")
    assert p1 == "anycubic/anycubicCloud/v1/printer/app/20025/KEY/#"
    assert p2 == "anycubic/anycubicCloud/v1/+/public/20025/KEY/#"


def test_topic_parsing_and_redaction() -> None:
    info = parse_topic(
        f"anycubic/anycubicCloud/v1/printer/app/1/{PRINTER_KEY}/response"
    )
    assert info.printer_key == PRINTER_KEY
    assert info.is_response
    assert not info.is_user_topic
    assert not info.is_ace
    assert info.ace_box_index == 0
    assert parse_topic("a/b").printer_key is None
    redacted = redact_topic(
        f"anycubic/anycubicCloud/v1/printer/public/1/{PRINTER_KEY}/fan"
    )
    assert PRINTER_KEY not in redacted
    assert "**REDACTED**" in redacted
    user = redact_topic(user_topics(424242)[0])
    assert "424242" not in user
    assert redact_topic("a/b/c") == "a/b/c"


# -- a fake paho client --------------------------------------------------------------------


def rc(identifier: int) -> ReasonCode:
    return ReasonCode(PacketTypes.CONNACK, identifier=identifier)


class FakePaho:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.connack: list[int] = [0]
        self.connect_error: BaseException | None = None
        self.hang = False
        self.credentials: list[tuple[str, str]] = []
        self.subscribed: list[Any] = []
        self.unsubscribed: list[Any] = []
        self.connected_to: tuple[str, int, int] | None = None
        self.insecure: bool | None = None
        self.context: ssl.SSLContext | None = None
        self.reconnect_delay: tuple[int, int] | None = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnects = 0
        self.on_connect: Any = None
        self.on_subscribe: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None

    # configuration
    def username_pw_set(self, username: str, password: str) -> None:
        self.credentials.append((username, password))

    def tls_set_context(self, context: ssl.SSLContext) -> None:
        self.context = context

    def tls_insecure_set(self, value: bool) -> None:
        self.insecure = value

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    # network
    def connect(self, host: str, port: int, keepalive: int) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True
        if not self.hang:
            self.fire_connack()

    def fire_connack(self) -> None:
        code = self.connack.pop(0) if len(self.connack) > 1 else self.connack[0]
        self.on_connect(self, None, None, rc(code), None)

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def subscribe(self, topic: Any, qos: int = 0) -> None:
        self.subscribed.append(topic)
        self.on_subscribe(self, None, 1, [rc(0)], None)

    def unsubscribe(self, topic: Any) -> None:
        self.unsubscribed.append(topic)

    def disconnect(self) -> None:
        self.disconnects += 1
        self.on_disconnect(self, None, None, rc(0), None)

    def drop(self) -> None:
        """An unexpected disconnect."""
        self.on_disconnect(self, None, None, rc(0), None)

    def deliver(self, topic: str, payload: bytes) -> None:
        msg = mqtt.MQTTMessage(topic=topic.encode())
        msg.payload = payload
        self.on_message(self, None, msg)


class Harness:
    def __init__(self, link: CloudMqttClient) -> None:
        self.link = link
        self.clients: list[FakePaho] = []
        self.messages: list[CloudMessage] = []
        self.raw: list[tuple[str, bytes]] = []
        self.events: list[tuple[bool, MqttError | None]] = []
        self.prepare: Any = None
        link.add_message_listener(self.messages.append)
        link.add_raw_listener(lambda t, p: self.raw.append((t, p)))
        link.add_connection_listener(lambda c, e: self.events.append((c, e)))

    def factory(self, client_id: str) -> FakePaho:
        client = FakePaho(client_id)
        if self.prepare is not None:
            self.prepare(client)
        self.clients.append(client)
        return client

    @property
    def paho(self) -> FakePaho:
        return self.clients[-1]


def make_link(
    http: FakeSession,
    secrets: CloudSecrets,
    *,
    mode: AuthMode = AuthMode.SLICER,
    region: Region = Region.INTERNATIONAL,
    account: Account | None = ACCOUNT,
    **kwargs: Any,
) -> Harness:
    cloud = make_client(http, secrets, mode=mode, region=region)
    cloud._account = account
    harness = Harness.__new__(Harness)
    link = CloudMqttClient(
        cloud, client_factory=lambda cid: harness.factory(cid), **kwargs
    )
    harness.__init__(link)  # type: ignore[misc]
    return harness


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def until_started(h: Harness) -> None:
    for _ in range(500):
        if h.clients and h.paho.loop_started:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the client never started")


# -- lifecycle ------------------------------------------------------------------------------


async def test_connect_subscribes_everything(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    h.link.subscribe_printer(PRINTER_KEY, MACHINE_TYPE)
    await h.link.connect()
    paho = h.paho
    assert paho.client_id == md5_hex(EMAIL + "pcf")
    assert paho.connected_to == ("mqtt-universe.anycubic.com", 8883, 1200)
    assert paho.insecure is None  # paho is never put in insecure mode
    assert paho.reconnect_delay == (5, 120)
    assert paho.context is not None
    assert paho.context.check_hostname
    username, _ = paho.credentials[0]
    assert username.startswith(f"user|pcf|{EMAIL}|")
    (topics,) = paho.subscribed
    assert [t for t, _ in topics] == [
        *user_topics(424242),
        *printer_topics(MACHINE_TYPE, PRINTER_KEY),
    ]
    assert all(qos == 0 for _, qos in topics)
    assert h.link.is_connected
    assert h.link.is_running
    assert h.link.last_error is None
    assert h.link.printers == frozenset({PRINTER_KEY})
    await settle()
    assert h.events == [(True, None)]
    await h.link.connect()  # already connected: nothing new
    assert len(h.clients) == 1


async def test_china_waives_only_the_hostname_check(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets, region=Region.CHINA)
    await h.link.connect()
    assert h.paho.insecure is None
    assert h.paho.context is not None
    assert h.paho.context.check_hostname is False
    assert h.paho.connected_to == ("mqtt.anycubicloud.com", 8883, 1200)
    assert h.paho.context is not None
    assert h.paho.context.verify_mode == ssl.CERT_REQUIRED


async def test_connect_without_topics(http: FakeSession, secrets: CloudSecrets) -> None:
    h = make_link(http, secrets, account=Account(user_id=None, email=EMAIL))
    await h.link.connect()
    assert h.link.is_connected
    assert h.paho.subscribed == []


async def test_web_mode_never_opens_a_socket(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets, mode=AuthMode.WEB)
    with pytest.raises(MqttNotAllowedError):
        await h.link.connect()
    assert h.clients == []
    assert isinstance(h.link.last_error, MqttNotAllowedError)


async def test_refused_login_is_a_clear_error(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    h.prepare = lambda c: setattr(c, "connack", [135])
    with pytest.raises(MqttAuthError, match="refused the login"):
        await h.link.connect()
    assert not h.link.is_running
    assert h.paho.loop_stopped
    assert isinstance(h.link.last_error, MqttAuthError)
    # a failed attempt never blocks the next one
    h.prepare = None
    await h.link.connect()
    assert h.link.is_connected
    assert h.link.last_error is None


async def test_other_refusal_is_a_connection_error(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    h.prepare = lambda c: setattr(c, "connack", [136])  # server unavailable
    with pytest.raises(
        MqttConnectionError, match=r"host=mqtt-universe\.anycubic\.com:8883"
    ):
        await h.link.connect()


async def test_socket_error(http: FakeSession, secrets: CloudSecrets) -> None:
    h = make_link(http, secrets)
    h.prepare = lambda c: setattr(c, "connect_error", ConnectionRefusedError("refused"))
    with pytest.raises(MqttConnectionError, match=r"ConnectionRefusedError.*8883"):
        await h.link.connect()
    assert not h.link.is_running


async def test_wrong_port_times_out(http: FakeSession, secrets: CloudSecrets) -> None:
    h = make_link(http, secrets)
    h.prepare = lambda c: setattr(c, "hang", True)
    with pytest.raises(MqttConnectionError, match="timed out"):
        await h.link.connect(timeout=0.05)
    assert h.paho.loop_stopped
    assert not h.link.is_running


async def test_broker_closes_during_connect(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)

    def closing(client: FakePaho) -> None:
        client.hang = True

    h.prepare = closing
    task = asyncio.create_task(h.link.connect(timeout=2))
    await until_started(h)
    h.paho.drop()
    with pytest.raises(MqttConnectionError, match="closed the connection"):
        await task


async def test_cancelled_connect_cleans_up(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    h.prepare = lambda c: setattr(c, "hang", True)
    task = asyncio.create_task(h.link.connect(timeout=5))
    await until_started(h)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not h.link.is_running


async def test_tls_setup_failure(
    http: FakeSession, secrets: CloudSecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> ssl.SSLContext:
        raise ssl.SSLError("bad material")

    monkeypatch.setattr("anycubic_cloud_client.mqtt.build_tls_context", broken)
    h = make_link(http, secrets)
    with pytest.raises(MqttConnectionError, match="TLS setup failed"):
        await h.link.connect()


async def test_new_printer_subscribed_without_reconnect(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    await h.link.connect()
    h.link.subscribe_printer("NEWKEY", 20030)
    assert h.paho.subscribed[-2:] == list(printer_topics(20030, "NEWKEY"))
    h.link.subscribe_printer("NEWKEY", 20030)  # already known: nothing sent
    assert len(h.paho.subscribed) == 3
    h.link.unsubscribe_printer("NEWKEY")
    assert h.paho.unsubscribed == [list(printer_topics(20030, "NEWKEY"))]
    h.link.unsubscribe_printer("NEWKEY")
    assert len(h.paho.unsubscribed) == 1
    assert h.link.printers == frozenset()


async def test_printers_added_while_down_wait_for_connect(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    h.link.subscribe_printer("A", 1)
    h.link.unsubscribe_printer("A")
    assert h.clients == []


async def test_routing(
    http: FakeSession, secrets: CloudSecrets, caplog: pytest.LogCaptureFixture
) -> None:
    h = make_link(http, secrets, debug_messages=True)
    h.link.subscribe_printer(PRINTER_KEY, MACHINE_TYPE)
    await h.link.connect()
    base = f"anycubic/anycubicCloud/v1/printer/public/{MACHINE_TYPE}"
    caplog.set_level(logging.DEBUG, logger="anycubic_cloud_client")
    h.paho.deliver(
        f"{base}/{PRINTER_KEY}/fan/report",
        json.dumps(message("fan", "auto", "done", {"fan_speed_pct": 5})).encode(),
    )
    h.paho.deliver(f"{base}/{PRINTER_KEY}/fan/report", b"not json")
    h.paho.deliver(user_topics(424242)[0], b'{"type": "slice"}')
    h.paho.deliver(
        f"{base}/OTHERKEY/fan/report",
        json.dumps(message("fan", "auto", "done")).encode(),
    )
    h.paho.deliver(
        f"anycubic/anycubicCloud/v1/printer/app/{MACHINE_TYPE}/{PRINTER_KEY}/response",
        b'{"msgid": "x"}',
    )
    h.paho.deliver(
        f"{base}/{PRINTER_KEY}/video/report",
        json.dumps(message("video", "startCapture", "done")).encode(),
    )
    await settle()
    assert len(h.raw) == 6
    assert [m.kind for m in h.messages] == ["fan", "video"]
    assert not h.messages[1].understood
    assert "Message decode error" in caplog.text
    assert PRINTER_KEY not in caplog.text
    assert "424242" not in caplog.text


async def test_listener_errors_are_contained(
    http: FakeSession, secrets: CloudSecrets, caplog: pytest.LogCaptureFixture
) -> None:
    h = make_link(http, secrets)

    def boom(*args: object) -> None:
        raise RuntimeError("listener bug")

    remove_message = h.link.add_message_listener(boom)
    remove_raw = h.link.add_raw_listener(boom)
    remove_conn = h.link.add_connection_listener(boom)
    h.link.subscribe_printer(PRINTER_KEY, MACHINE_TYPE)
    await h.link.connect()
    h.paho.deliver(
        f"anycubic/anycubicCloud/v1/printer/public/{MACHINE_TYPE}/{PRINTER_KEY}/fan",
        json.dumps(message("fan", "auto", "done")).encode(),
    )
    await settle()
    assert len(h.messages) == 1
    assert caplog.text.count("listener bug") >= 3
    remove_message()
    remove_raw()
    remove_conn()
    remove_conn()


async def test_unexpected_disconnect_recomputes_login(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    h.link.subscribe_printer(PRINTER_KEY, MACHINE_TYPE)
    await h.link.connect()
    await settle()
    paho = h.paho
    paho.drop()
    await settle()
    assert not h.link.is_connected
    assert h.link.is_running
    assert h.events == [(True, None), (False, None)]
    assert len(paho.credentials) == 2  # worked out again before the reconnect
    assert paho.credentials[0][1] != paho.credentials[1][1]
    paho.fire_connack()  # paho reconnected on its own
    await settle()
    assert h.link.is_connected
    assert h.events[-1] == (True, None)
    assert len(paho.subscribed) == 2  # everything resubscribed


async def test_identity_failure_keeps_previous_login(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    await h.link.connect()
    h.link._cloud._user_token = None
    h.paho.drop()
    await settle()
    assert len(h.paho.credentials) == 1


async def test_repeated_refusal_after_reconnect_gives_up(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets, max_auth_failures=2)
    await h.link.connect()
    await settle()
    paho = h.paho
    paho.drop()
    await settle()
    paho.connack = [136]  # a non-auth refusal keeps retrying
    paho.fire_connack()
    await settle()
    assert h.link.is_running
    paho.connack = [135]
    paho.fire_connack()
    await settle()
    assert h.link.is_running
    paho.fire_connack()
    for _ in range(200):
        if not h.link.is_running:
            break
        await asyncio.sleep(0.01)
    assert not h.link.is_running
    assert paho.loop_stopped
    connected, error = h.events[-1]
    assert connected is False
    assert isinstance(error, MqttAuthError)
    assert isinstance(h.link.last_error, MqttAuthError)


async def test_successful_connack_resets_the_refusal_count(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets, max_auth_failures=2)
    await h.link.connect()
    paho = h.paho
    paho.drop()
    await settle()
    paho.connack = [135]
    paho.fire_connack()
    paho.connack = [0]
    paho.fire_connack()
    paho.drop()
    paho.connack = [135]
    paho.fire_connack()
    await settle()
    assert h.link.is_running


async def test_graceful_disconnect(http: FakeSession, secrets: CloudSecrets) -> None:
    h = make_link(http, secrets)
    h.link.subscribe_printer(PRINTER_KEY, MACHINE_TYPE)
    await h.link.connect()
    await settle()
    await h.link.disconnect()
    paho = h.paho
    assert paho.unsubscribed == [list(printer_topics(MACHINE_TYPE, PRINTER_KEY))]
    assert paho.disconnects == 1
    assert paho.loop_stopped
    assert not h.link.is_running
    assert not h.link.is_connected
    assert h.events == [(True, None)]  # a deliberate stop is not reported
    await h.link.disconnect()  # nothing to do
    assert h.link.printers == frozenset({PRINTER_KEY})


async def test_disconnect_waits_at_most_for_the_broker(
    http: FakeSession, secrets: CloudSecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("anycubic_cloud_client.mqtt.DISCONNECT_TIMEOUT", 0.01)
    h = make_link(http, secrets)
    await h.link.connect()
    h.paho.disconnect = lambda: None  # type: ignore[method-assign]
    await h.link.disconnect()
    assert not h.link.is_running


async def test_wait_until_connected(http: FakeSession, secrets: CloudSecrets) -> None:
    h = make_link(http, secrets)
    with pytest.raises(MqttNotConnectedError, match="not running"):
        await h.link.wait_until_connected()
    await h.link.connect()
    await h.link.wait_until_connected()  # connected: returns at once
    h.paho.drop()
    await settle()
    with pytest.raises(MqttNotConnectedError, match="Timed out"):
        await h.link.wait_until_connected(timeout=0.01)
    waiter = asyncio.create_task(h.link.wait_until_connected(timeout=1, settle=0.01))
    await settle()
    h.paho.fire_connack()
    await waiter
    h.paho.drop()
    await settle()
    waiter = asyncio.create_task(h.link.wait_until_connected(timeout=1))
    await settle()
    await h.link.disconnect()
    with pytest.raises(MqttNotConnectedError, match="stopped"):
        await waiter


async def test_connect_while_reconnecting_waits(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    await h.link.connect()
    h.paho.drop()
    await settle()
    task = asyncio.create_task(h.link.connect(timeout=1))
    await settle()
    h.paho.fire_connack()
    await task
    assert h.link.is_connected
    assert len(h.clients) == 1


async def test_late_callbacks_after_stop_are_ignored(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    h = make_link(http, secrets)
    await h.link.connect()
    paho = h.paho
    await h.link.disconnect()
    paho.fire_connack()
    paho.drop()
    await settle()
    assert not h.link.is_connected
    link = CloudMqttClient(make_client(http, secrets))
    link._dispatch(print)  # no loop yet: dropped quietly


def test_dispatch_after_loop_closed(http: FakeSession, secrets: CloudSecrets) -> None:
    link = CloudMqttClient(make_client(http, secrets))
    loop = asyncio.new_event_loop()
    link._loop = loop
    loop.close()
    link._dispatch(print)


async def test_light_reports_set_the_light_type(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    """L1: ``light`` reports feed the type set_light sends (B §5.4.7, C §4.7)."""
    h = make_link(http, secrets)
    cloud = h.link._cloud
    cloud.remember_printer_key(12345, PRINTER_KEY)
    h.link.subscribe_printer(PRINTER_KEY, MACHINE_TYPE)
    await h.link.connect()
    topic = f"anycubic/anycubicCloud/v1/printer/public/{MACHINE_TYPE}/{PRINTER_KEY}"
    for body in (
        message("light", "control", "failed", {"type": 1, "status": 1}),
        message("light", "query", "done", None),
        message("fan", "auto", "done", {"type": 1}),
    ):
        h.paho.deliver(f"{topic}/light/report", json.dumps(body).encode())
    await settle()
    assert cloud.light_type(12345) is None
    h.paho.deliver(
        f"{topic}/light/report",
        json.dumps(
            message(
                "light",
                "query",
                "done",
                {"lights": [{"type": 3, "status": 0}, {"type": 2, "status": 1}, "x"]},
            )
        ).encode(),
    )
    await settle()
    assert cloud.light_type(12345) == 2
    h.paho.deliver(
        f"{topic}/light/report",
        json.dumps(
            message("light", "control", "done", {"type": 1, "status": 1})
        ).encode(),
    )
    await settle()
    assert cloud.light_type(12345) == 1
    assert h.messages[-1].light_types == (1,)
