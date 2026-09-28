"""Shared fixtures: fake credentials generated at test time and a fake HTTP layer.

Nothing here touches the network. The CA, client certificate and key are
throwaway material made with ``cryptography`` for each test session.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from anycubic_cloud_client import AnycubicCloudClient, AuthMode, CloudSecrets, Region

if TYPE_CHECKING:
    import aiohttp

API = "https://cloud-universe.anycubic.com/p/p/workbench/api"
API_CN = "https://cloud-platform.anycubicloud.com/p/p/workbench/api"

FAKE_APP_ID = "FAKEAPPID0000000000000000000000A"
FAKE_APP_SECRET = "FAKESECRET000000000000000000000B"


@dataclass(frozen=True)
class Material:
    ca_key: rsa.RSAPrivateKey
    ca_pem: bytes
    cert_pem: bytes
    key_pem: bytes


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_material() -> Material:
    now = dt.datetime.now(dt.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Throwaway Test CA"))
        .issuer_name(_name("Throwaway Test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Throwaway Test Client"))
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(ca_key, hashes.SHA256())
    )
    return Material(
        ca_key=ca_key,
        ca_pem=ca_cert.public_bytes(serialization.Encoding.PEM),
        cert_pem=client_cert.public_bytes(serialization.Encoding.PEM),
        key_pem=client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )


@pytest.fixture(scope="session")
def material() -> Material:
    return make_material()


@pytest.fixture(scope="session")
def secrets(material: Material) -> CloudSecrets:
    return CloudSecrets(
        app_id=FAKE_APP_ID,
        app_secret=FAKE_APP_SECRET,
        client_id_web="FAKEWEBCLIENTID00001",
        client_id_app="FAKEAPPCLIENTID00002",
        mqtt_ca_pem=material.ca_pem,
        mqtt_client_cert_pem=material.cert_pem,
        mqtt_client_key_pem=material.key_pem,
    )


# --------------------------------------------------------------------------
# Fake HTTP
# --------------------------------------------------------------------------


@dataclass
class Call:
    method: str
    url: str
    kwargs: dict[str, Any]

    @property
    def path(self) -> str:
        for root in (API, API_CN):
            if self.url.startswith(root):
                return self.url[len(root) :]
        return self.url

    @property
    def headers(self) -> dict[str, str]:
        return dict(self.kwargs.get("headers") or {})

    @property
    def body(self) -> Any:
        data = self.kwargs.get("data")
        return json.loads(data) if isinstance(data, str) else data

    @property
    def raw_body(self) -> Any:
        return self.kwargs.get("data")

    @property
    def params(self) -> dict[str, str]:
        return dict(self.kwargs.get("params") or {})


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        text: str = "",
        status: int = 200,
        raw: bytes = b"",
        json_error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self._text = text
        self.status = status
        self._raw = raw
        self._json_error = json_error

    async def json(self, content_type: str | None = "application/json") -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._raw


class _Context:
    def __init__(self, response: FakeResponse | BaseException) -> None:
        self._response = response

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    async def __aexit__(self, *args: object) -> None:
        return None


type Responder = (
    FakeResponse | BaseException | dict[str, Any] | list[Any] | Callable[[Call], Any]
)


@dataclass
class FakeSession:
    """Answers requests from per-(method, path) queues; records every call."""

    routes: dict[tuple[str, str], list[Responder]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    ws_factory: Callable[[str], Any] | None = None

    def add(self, method: str, path: str, *responses: Responder) -> None:
        self.routes.setdefault((method, path), []).extend(responses)

    def calls_to(self, path: str, method: str | None = None) -> list[Call]:
        return [
            c
            for c in self.calls
            if c.path == path and (method is None or c.method == method)
        ]

    def _answer(self, call: Call) -> FakeResponse | BaseException:
        queue = self.routes.get((call.method, call.path))
        if not queue:
            raise AssertionError(f"unexpected request {call.method} {call.path}")
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(responder) and not isinstance(
            responder, FakeResponse | BaseException
        ):
            responder = responder(call)
        if isinstance(responder, FakeResponse | BaseException):
            return responder
        return FakeResponse(responder)

    def request(self, method: str, url: str, **kwargs: Any) -> _Context:
        call = Call(method, url, kwargs)
        self.calls.append(call)
        return _Context(self._answer(call))

    def get(self, url: str, **kwargs: Any) -> _Context:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _Context:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> _Context:
        return self.request("PUT", url, **kwargs)

    async def ws_connect(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(Call("WS", url, kwargs))
        assert self.ws_factory is not None
        return self.ws_factory(url)


def envelope(data: Any = None, msg: str = "success", code: int = 1) -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}


@pytest.fixture
def http() -> FakeSession:
    return FakeSession()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make ``asyncio.sleep`` in the client instant, recording the delays."""
    delays: list[float] = []

    async def fake_sleep(delay: float, *args: Any) -> None:
        delays.append(delay)

    monkeypatch.setattr("anycubic_cloud_client.client.asyncio.sleep", fake_sleep)
    return delays


def make_client(
    http: FakeSession,
    secrets: CloudSecrets,
    *,
    mode: AuthMode = AuthMode.SLICER,
    region: Region = Region.INTERNATIONAL,
    user_token: str | None = "USER-TOKEN",
    access_token: str | None = None,
    device_id: str | None = None,
    **kwargs: Any,
) -> AnycubicCloudClient:
    return AnycubicCloudClient(
        aiohttp_session(http),
        secrets,
        region=region,
        auth_mode=mode,
        user_token=user_token,
        access_token=access_token,
        device_id=device_id,
        **kwargs,
    )


def aiohttp_session(http: FakeSession) -> aiohttp.ClientSession:
    return http  # type: ignore[return-value]


USER_INFO = {
    "id": 424242,
    "user_email": "someone@example.invalid",
    "mobile": "",
    "user_nickname": "<nickname>",
    "casdoor_user_id": "<casdoor id>",
}
