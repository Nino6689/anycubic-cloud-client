"""The cloud MQTT link (PROTOCOL C §1, §2, §6).

One :class:`CloudMqttClient` per account. The integration decides **when** it
runs (its ``mqtt_connect_mode`` option); this class connects, subscribes,
routes and parses messages, reconnects with back-off, and reports a refused
login as a clear error instead of looping silently as 2.x did.

paho-mqtt runs its network loop in its own thread. Every callback from that
thread is handed to the asyncio loop with ``call_soon_threadsafe``; state is
only read and written on the asyncio loop, except the subscription table,
which paho's thread reads under a lock when it resubscribes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .errors import (
    AnycubicCloudError,
    MqttAuthError,
    MqttConnectionError,
    MqttError,
    MqttNotAllowedError,
    MqttNotConnectedError,
)
from .messages import CloudMessage, decode_payload, parse_cloud_message
from .mqtt_identity import (
    KEEPALIVE,
    MqttIdentity,
    build_identity,
    build_tls_context,
    parse_topic,
    printer_topics,
    redact_topic,
    user_topics,
)

if TYPE_CHECKING:
    from paho.mqtt.properties import Properties
    from paho.mqtt.reasoncodes import ReasonCode

    from .client import AnycubicCloudClient

_LOGGER = logging.getLogger(__name__)

#: Reconnect back-off: 5 s doubling to 120 s (PROTOCOL C §1.6).
RECONNECT_MIN_DELAY = 5
RECONNECT_MAX_DELAY = 120
#: Wait for the broker to confirm a deliberate disconnect.
DISCONNECT_TIMEOUT = 10.0
#: Default time allowed for connect + CONNACK + first SUBACK.
CONNECT_TIMEOUT = 30.0
#: Consecutive refused logins after which the client gives up (Q8).
MAX_AUTH_FAILURES = 3

#: CONNACK reason codes that mean "bad credentials" (MQTT 3.1.1 rc 4 and 5,
#: as paho 2 maps them).
_AUTH_REFUSALS = frozenset({4, 5, 134, 135})

type MessageListener = Callable[[CloudMessage], None]
type RawListener = Callable[[str, bytes], None]
type ConnectionListener = Callable[[bool, MqttError | None], None]
type ClientFactory = Callable[[str], mqtt.Client]


def _default_factory(client_id: str) -> mqtt.Client:
    return mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )


class CloudMqttClient:
    """The account's cloud MQTT link.

    Usage::

        link = CloudMqttClient(cloud_client)
        link.add_message_listener(on_message)
        link.subscribe_printer(printer.key, printer.machine_type)
        await link.connect()
        ...
        await link.disconnect()

    :meth:`connect` needs a client that has signed in (:meth:`check`) in
    SLICER or ANDROID mode. The login is worked out again for every
    reconnect, because the tokens may have changed and the password is
    randomised (PROTOCOL A §2.12).
    """

    def __init__(
        self,
        cloud: AnycubicCloudClient,
        *,
        keepalive: int = KEEPALIVE,
        connect_timeout: float = CONNECT_TIMEOUT,
        max_auth_failures: int = MAX_AUTH_FAILURES,
        debug_messages: bool = False,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._cloud = cloud
        self._keepalive = keepalive
        self._connect_timeout = connect_timeout
        self._max_auth_failures = max_auth_failures
        self.debug_messages = debug_messages
        self._factory = client_factory or _default_factory
        self._printers: dict[str, int] = {}
        self._printers_lock = threading.Lock()
        self._message_listeners: list[MessageListener] = []
        self._raw_listeners: list[RawListener] = []
        self._connection_listeners: list[ConnectionListener] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mqtt: mqtt.Client | None = None
        self._connected = False
        self._closing = False
        self._pending: asyncio.Future[None] | None = None
        self._up = asyncio.Event()
        self._stopped = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._auth_failures = 0
        self._last_error: MqttError | None = None
        self._user_id: int | None = None
        self._give_up_task: asyncio.Task[None] | None = None

    # -- properties -----------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """``True`` once the broker acknowledged a subscription, until a drop."""
        return self._connected

    @property
    def is_running(self) -> bool:
        """A client exists: connected, connecting or reconnecting."""
        return self._mqtt is not None

    @property
    def last_error(self) -> MqttError | None:
        """Why the last connection attempt failed, or why the client gave up."""
        return self._last_error

    @property
    def printers(self) -> frozenset[str]:
        """The printer keys in the subscription set."""
        with self._printers_lock:
            return frozenset(self._printers)

    # -- listeners ------------------------------------------------------------

    def add_message_listener(self, callback: MessageListener) -> Callable[[], None]:
        """``callback(message)`` for every parsed printer message; returns a remover."""
        self._message_listeners.append(callback)
        return lambda: _discard(self._message_listeners, callback)

    def add_raw_listener(self, callback: RawListener) -> Callable[[], None]:
        """``callback(topic, payload)`` for every message received, before parsing."""
        self._raw_listeners.append(callback)
        return lambda: _discard(self._raw_listeners, callback)

    def add_connection_listener(
        self, callback: ConnectionListener
    ) -> Callable[[], None]:
        """``callback(connected, error)`` when the link changes.

        ``(True, None)`` after every successful (re)subscription, including
        the first; ``(False, None)`` when an established link drops (paho
        reconnects on its own); ``(False, error)`` when the client gives up,
        e.g. after repeated refused logins.
        """
        self._connection_listeners.append(callback)
        return lambda: _discard(self._connection_listeners, callback)

    # -- subscription set -------------------------------------------------------

    def subscribe_printer(self, printer_key: str, machine_type: int) -> None:
        """Add a printer; subscribed at once when the link is up (not only at
        the next reconnect, as in 2.x)."""
        with self._printers_lock:
            known = self._printers.get(printer_key)
            self._printers[printer_key] = machine_type
        if known == machine_type:
            return
        client = self._mqtt
        if client is not None and self._connected:
            for topic in printer_topics(machine_type, printer_key):
                client.subscribe(topic, qos=0)

    def unsubscribe_printer(self, printer_key: str) -> None:
        """Remove a printer and unsubscribe its topics when the link is up."""
        with self._printers_lock:
            machine_type = self._printers.pop(printer_key, None)
        client = self._mqtt
        if machine_type is not None and client is not None and self._connected:
            client.unsubscribe(list(printer_topics(machine_type, printer_key)))

    def _all_topics(self) -> list[str]:
        topics: list[str] = []
        if self._user_id is not None:
            topics.extend(user_topics(self._user_id))
        with self._printers_lock:
            printers = list(self._printers.items())
        for key, machine_type in printers:
            topics.extend(printer_topics(machine_type, key))
        return topics

    # -- connection -----------------------------------------------------------

    def _identity(self) -> MqttIdentity:
        cloud = self._cloud
        return build_identity(
            cloud.auth_mode, cloud.user_token, cloud.account, cloud.secrets
        )

    def _address(self) -> str:
        endpoints = self._cloud.region.endpoints
        return f"{endpoints.mqtt_host}:{endpoints.mqtt_port}"

    def _fail(self, error: MqttError) -> MqttError:
        self._last_error = error
        _LOGGER.error("Anycubic MQTT connection failed: %s", error)
        return error

    async def connect(self, timeout: float | None = None) -> None:
        """Connect, subscribe (user topics, then every printer) and return once
        the broker acknowledged the first subscription.

        Raises :class:`MqttNotAllowedError` before opening any socket when the
        login cannot be built, :class:`MqttAuthError` when the broker refuses
        it and :class:`MqttConnectionError` for anything else (with host and
        port). A failed attempt leaves nothing behind: the next call starts
        afresh.
        """
        if self._mqtt is not None:
            await self.wait_until_connected(timeout or self._connect_timeout, settle=0)
            return
        loop = self._loop = asyncio.get_running_loop()
        account = self._cloud.account
        self._user_id = account.user_id if account is not None else None
        endpoints = self._cloud.region.endpoints
        try:
            identity = await loop.run_in_executor(None, self._identity)
            context = await loop.run_in_executor(
                None, build_tls_context, self._cloud.secrets, endpoints
            )
        except MqttNotAllowedError as err:
            raise self._fail(err) from None
        except (OSError, ValueError) as err:
            raise self._fail(
                MqttConnectionError(f"TLS setup failed: {err} (host={self._address()})")
            ) from err
        client = self._factory(identity.client_id)
        client.username_pw_set(identity.username, identity.password)
        client.tls_set_context(context)
        client.tls_insecure_set(not endpoints.mqtt_check_hostname)
        client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._mqtt = client
        self._closing = False
        self._auth_failures = 0
        self._up.clear()
        self._stopped.clear()
        self._pending = loop.create_future()
        try:
            async with asyncio.timeout(timeout or self._connect_timeout):
                await loop.run_in_executor(
                    None,
                    client.connect,
                    endpoints.mqtt_host,
                    endpoints.mqtt_port,
                    self._keepalive,
                )
                client.loop_start()
                await self._pending
        except MqttError as err:
            await self._teardown(client)
            raise self._fail(err) from None
        except (TimeoutError, OSError) as err:
            await self._teardown(client)
            reason = "timed out" if isinstance(err, TimeoutError) else str(err)
            raise self._fail(
                MqttConnectionError(
                    f"{type(err).__name__}: {reason} (host={self._address()})"
                )
            ) from err
        except BaseException:
            await self._teardown(client)
            raise
        finally:
            self._pending = None
        self._last_error = None

    async def wait_until_connected(
        self, timeout: float = 10.0, settle: float = 2.0
    ) -> None:
        """Wait on the real link state; then ``settle`` seconds more.

        Returns at once when connected. Raises :class:`MqttNotConnectedError`
        when no connection is running, when the client gives up while
        waiting, or after ``timeout`` seconds. An order must not be sent
        before the link exists (the 2.x wake race, PROTOCOL C §6.9).
        """
        if self._connected:
            return
        if self._mqtt is None:
            raise MqttNotConnectedError("The cloud MQTT link is not running")
        up = asyncio.ensure_future(self._up.wait())
        stopped = asyncio.ensure_future(self._stopped.wait())
        try:
            done, _ = await asyncio.wait(
                {up, stopped}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            up.cancel()
            stopped.cancel()
        if up not in done or not self.is_connected:
            raise MqttNotConnectedError(
                "Timed out waiting for the cloud MQTT link"
                if not done
                else "The cloud MQTT link stopped"
            )
        if settle > 0:
            await asyncio.sleep(settle)

    async def disconnect(self) -> None:
        """Stop the link: unsubscribe every printer, disconnect, wait up to 10 s.

        Listeners are not told about a deliberate disconnect.
        """
        client = self._mqtt
        if client is None:
            return
        if self._connected:
            with self._printers_lock:
                printers = list(self._printers.items())
            for key, machine_type in printers:
                client.unsubscribe(list(printer_topics(machine_type, key)))
        await self._teardown(client, graceful=True)

    async def _teardown(self, client: mqtt.Client, *, graceful: bool = False) -> None:
        self._closing = True
        self._connected = False
        self._up.clear()
        self._disconnected.clear()
        client.disconnect()
        if graceful:
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await self._disconnected.wait()
            except TimeoutError:
                _LOGGER.debug("The broker did not confirm the disconnect")
        await asyncio.get_running_loop().run_in_executor(None, client.loop_stop)
        if self._mqtt is client:
            self._mqtt = None
        self._stopped.set()

    # -- paho callbacks (paho's thread) ---------------------------------------

    def _dispatch(self, callback: Callable[..., None], *args: Any) -> None:
        if (loop := self._loop) is None:
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:  # loop closed while paho was still running
            _LOGGER.debug("Event loop closed; dropping an MQTT callback")

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            self._dispatch(
                self._handle_refused, int(reason_code.value), str(reason_code)
            )
            return
        topics = self._all_topics()
        # Subscribing here makes every reconnect resubscribe everything.
        if topics:
            client.subscribe([(topic, 0) for topic in topics])
        self._dispatch(self._handle_connack, bool(topics))

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: Properties | None,
    ) -> None:
        self._dispatch(self._handle_suback)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if not self._closing:
            # Work the login out again before paho's automatic reconnect.
            try:
                identity = self._identity()
            except AnycubicCloudError as err:
                _LOGGER.debug("Keeping the previous MQTT login: %s", err)
            else:
                client.username_pw_set(identity.username, identity.password)
        self._dispatch(self._handle_disconnect, str(reason_code))

    def _on_message(
        self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage
    ) -> None:
        self._dispatch(self._handle_message, message.topic, bytes(message.payload))

    # -- handlers (event loop) ------------------------------------------------

    def _handle_refused(self, code: int, reason: str) -> None:
        auth = code in _AUTH_REFUSALS
        error: MqttError
        if auth:
            error = MqttAuthError(f"The broker refused the login: {reason}")
        else:
            error = MqttConnectionError(
                f"The broker refused the connection: {reason} (host={self._address()})"
            )
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_exception(error)
            return
        _LOGGER.warning("Failed to connect, return code %s", reason)
        if not auth:
            return
        self._auth_failures += 1
        if self._auth_failures >= self._max_auth_failures and self._mqtt is not None:
            self._last_error = error
            _LOGGER.error(
                "Anycubic MQTT login refused %s times; giving up", self._auth_failures
            )
            client = self._mqtt
            self._give_up_task = asyncio.get_running_loop().create_task(
                self._give_up(client, error)
            )

    async def _give_up(self, client: mqtt.Client, error: MqttError) -> None:
        await self._teardown(client)
        self._notify_connection(False, error)

    def _handle_connack(self, subscribed: bool) -> None:
        if self._closing:
            return
        self._auth_failures = 0
        if not subscribed:
            self._handle_suback()

    def _handle_suback(self) -> None:
        if self._closing or self._connected:
            return
        self._connected = True
        self._up.set()
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_result(None)
        _LOGGER.debug("Anycubic MQTT subscribed")
        self._notify_connection(True, None)

    def _handle_disconnect(self, reason: str) -> None:
        self._disconnected.set()
        if self._closing:
            return
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_exception(
                MqttConnectionError(
                    f"The broker closed the connection: {reason} "
                    f"(host={self._address()})"
                )
            )
            return
        if self._connected:
            self._connected = False
            self._up.clear()
            _LOGGER.debug("Anycubic MQTT disconnected (%s); reconnecting", reason)
            self._notify_connection(False, None)

    def _handle_message(self, topic: str, payload: bytes) -> None:
        for raw_listener in list(self._raw_listeners):
            try:
                raw_listener(topic, payload)
            except Exception:
                _LOGGER.exception("Error in raw MQTT listener")
        decoded = decode_payload(payload)
        if decoded is None:
            _LOGGER.error("Message decode error on %s", redact_topic(topic))
            return
        info = parse_topic(topic)
        if info.is_user_topic:
            # Account slicing reports: only logged, as in 2.x (Q10).
            _LOGGER.debug("User message on %s", redact_topic(topic))
            return
        if info.printer_key not in self.printers:
            return
        message = parse_cloud_message(info, decoded)
        if message is None:
            return
        if self.debug_messages or not message.understood:
            _LOGGER.debug(
                "Message %s/%s/%s on %s%s",
                message.kind,
                message.action,
                message.state,
                redact_topic(topic),
                "" if message.understood else " not understood",
            )
        for listener in list(self._message_listeners):
            try:
                listener(message)
            except Exception:
                _LOGGER.exception("Error in MQTT message listener")

    def _notify_connection(self, connected: bool, error: MqttError | None) -> None:
        for callback in list(self._connection_listeners):
            try:
                callback(connected, error)
            except Exception:
                _LOGGER.exception("Error in MQTT connection listener")


def _discard[T](items: list[T], item: T) -> None:
    if item in items:
        items.remove(item)
