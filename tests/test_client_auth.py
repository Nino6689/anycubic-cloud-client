"""HTTP transport, the envelope and sign-in (PROTOCOL A §2-§4)."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
import pytest

from anycubic_cloud_client import (
    AnycubicCloudClient,
    AuthMode,
    CloudSecrets,
    CredentialsRejectedError,
    PrinterRemovedError,
    Region,
    RejectReason,
    ServiceUnavailableError,
    UnexpectedResponseError,
    sign_in_any,
    sign_in_order,
)
from anycubic_cloud_client.signing import signature

from .conftest import (
    FAKE_APP_ID,
    FAKE_APP_SECRET,
    USER_INFO,
    Call,
    FakeResponse,
    FakeSession,
    aiohttp_session,
    envelope,
    make_client,
)
from .test_tokens import make_jwt

EXCHANGE = "/v3/public/loginWithAccessToken"
USER = "/user/profile/userInfo"
EXPIRED_MSG = "Login information has expired. Please login again."


def exchange_ok(token: str = "EXCHANGED") -> dict[str, Any]:
    return envelope({"token": token, "other": 1})


def refused(msg: str = "User does not exist") -> dict[str, Any]:
    return envelope(None, msg=msg, code=0)


# -- transport ----------------------------------------------------------------------


async def test_get_request_is_signed(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("GET", "/v2/printer/info", envelope({"id": 5}))
    client = make_client(http, secrets)
    response = await client.request("GET", "/v2/printer/info", params={"id": 5})
    assert response.data == {"id": 5}
    assert response.code == 1
    call = http.calls[0]
    assert (
        call.url
        == "https://cloud-universe.anycubic.com/p/p/workbench/api/v2/printer/info"
    )
    assert call.params == {"id": "5"}
    assert "data" not in call.kwargs
    headers = call.headers
    assert headers["XX-Token"] == "USER-TOKEN"
    assert headers["Content-Type"] == "application/json"
    assert headers["Xx-Signature"] == signature(
        FAKE_APP_ID,
        headers["Xx-Timestamp"],
        "V3.0.0",
        FAKE_APP_SECRET,
        headers["Xx-Nonce"],
    )


async def test_post_sends_json_even_when_empty(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", "/work/index/getUserStore", envelope({}))
    client = make_client(http, secrets)
    await client.request("POST", "/work/index/getUserStore")
    call = http.calls[0]
    assert call.raw_body == "{}"
    assert "params" not in call.kwargs


async def test_request_timeout_is_passed(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, envelope(USER_INFO))
    client = make_client(http, secrets, request_timeout=12)
    await client.request("GET", USER)
    assert http.calls[0].kwargs["timeout"].total == 12


@pytest.mark.parametrize(
    "failure",
    [
        aiohttp.ClientConnectionError("reset"),
        TimeoutError(),
        FakeResponse(json_error=aiohttp.ContentTypeError(None, ())),  # type: ignore[arg-type]
        FakeResponse(json_error=json.JSONDecodeError("x", "<html>", 0)),
    ],
)
async def test_transport_failures_are_service_unavailable(
    http: FakeSession, secrets: CloudSecrets, failure: Any
) -> None:
    http.add("GET", USER, failure)
    with pytest.raises(ServiceUnavailableError, match="server maintenance"):
        await make_client(http, secrets).request("GET", USER)


async def test_request_error_is_service_unavailable(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, envelope(None, msg="request error"))
    with pytest.raises(ServiceUnavailableError, match="request error"):
        await make_client(http, secrets).check()


async def test_code_1007_is_printer_removed(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add(
        "GET", "/v2/printer/info", envelope(None, msg="printer not exist", code=1007)
    )
    with pytest.raises(PrinterRemovedError, match="printer not exist"):
        await make_client(http, secrets).get_printer(5)


async def test_non_object_answer_is_unexpected(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, [1, 2])
    with pytest.raises(UnexpectedResponseError):
        await make_client(http, secrets).request("GET", USER)


async def test_status_code_is_not_inspected(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, FakeResponse(envelope(USER_INFO), status=500))
    account = await make_client(http, secrets).check()
    assert account.user_id == 424242


# -- slow calls and debug logging ------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.step = 0.0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


async def test_slow_call_warning_at_most_every_10_minutes(
    http: FakeSession, secrets: CloudSecrets, caplog: pytest.LogCaptureFixture
) -> None:
    http.add("GET", USER, envelope(USER_INFO))
    client = make_client(http, secrets)
    clock = Clock()
    client._clock = clock
    caplog.set_level(logging.WARNING)

    clock.step = 21  # each request takes 21 s
    await client.request("GET", USER)
    await client.request("GET", USER)
    warnings = [r for r in caplog.records if "taking over 20s" in r.getMessage()]
    assert len(warnings) == 1
    assert "(Took 21s)" in warnings[0].getMessage()

    clock.now += 600
    await client.request("GET", USER)
    warnings = [r for r in caplog.records if "taking over 20s" in r.getMessage()]
    assert len(warnings) == 2


async def test_exactly_20s_is_not_slow(
    http: FakeSession, secrets: CloudSecrets, caplog: pytest.LogCaptureFixture
) -> None:
    http.add("GET", USER, envelope(USER_INFO))
    client = make_client(http, secrets)
    clock = Clock()
    clock.step = 20.9
    client._clock = clock
    await client.request("GET", USER)
    assert not [r for r in caplog.records if "taking over" in r.getMessage()]


async def test_debug_api_calls_logs_url_not_secrets(
    http: FakeSession, secrets: CloudSecrets, caplog: pytest.LogCaptureFixture
) -> None:
    http.add("GET", "/v2/printer/info", envelope({"id": 5}))
    client = make_client(http, secrets, debug_api_calls=True)
    caplog.set_level(logging.DEBUG, logger="anycubic_cloud_client")
    await client.get_printer(5)
    text = caplog.text
    assert "/v2/printer/info took" in text
    assert "id=5" not in text
    assert "USER-TOKEN" not in text
    assert FAKE_APP_SECRET not in text


# -- sign-in -------------------------------------------------------------------------


async def test_web_mode_check(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("GET", USER, envelope(USER_INFO))
    client = AnycubicCloudClient.from_entry(
        aiohttp_session(http),
        secrets,
        token="web-token",
        auth_mode=1,
        region="international",
    )
    account = await client.check()
    assert account.user_id == 424242
    assert account.email == "someone@example.invalid"
    assert account.mobile is None
    assert account.identifier == "someone@example.invalid"
    assert client.account is account
    assert not client.tokens_changed
    headers = http.calls[0].headers
    assert headers["Xx-Device-Type"] == "web"
    assert headers["XX-Token"] == "web-token"
    assert headers["Origin"] == "https://uc.makeronline.com"
    assert not client.supports_mqtt_login


async def test_android_mode_check(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("GET", USER, envelope({**USER_INFO, "user_email": "", "mobile": "+000"}))
    client = AnycubicCloudClient.from_entry(
        aiohttp_session(http),
        secrets,
        token="android-token",
        auth_mode=2,
        device_id="devid",
    )
    account = await client.check()
    assert account.identifier == "+000"
    headers = http.calls[0].headers
    assert headers["XX-Device-Id"] == "devid"
    assert len(headers["Xx-Nonce"]) == 22
    assert client.supports_mqtt_login


async def test_android_without_device_id_makes_one(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, envelope(USER_INFO))
    client = make_client(http, secrets, mode=AuthMode.ANDROID)
    await client.check()
    device = client.token_state.device_id
    assert device is not None
    assert len(device) == 33
    assert http.calls[0].headers["XX-Device-Id"] == device
    assert client.tokens_changed


async def test_slicer_exchange(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("POST", EXCHANGE, exchange_ok())
    http.add("GET", USER, envelope(USER_INFO))
    client = AnycubicCloudClient.from_entry(
        aiohttp_session(http), secrets, token="access", auth_mode=AuthMode.SLICER
    )
    assert client.token_state.auth_access_token == "access"
    assert client.token_state.auth_token is None
    await client.check()
    exchange = http.calls[0]
    assert exchange.path == EXCHANGE
    assert "XX-Token" not in exchange.headers
    assert exchange.raw_body == '{"device_type": "pcf", "access_token": "access"}'
    assert http.calls[1].headers["XX-Token"] == "EXCHANGED"
    assert client.tokens_changed
    assert client.user_token == "EXCHANGED"
    client.mark_tokens_saved()
    assert not client.tokens_changed
    assert client.export_token_store() == {
        "auth_token": "EXCHANGED",
        "auth_access_token": "access",
        "device_id": None,
        "auth_mode": 3,
    }


async def test_slicer_china_uses_token_directly(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, envelope(USER_INFO))
    client = AnycubicCloudClient.from_entry(
        aiohttp_session(http),
        secrets,
        token="cn-token",
        auth_mode=3,
        region=Region.CHINA,
    )
    await client.check()
    assert http.calls[0].url.startswith("https://cloud-platform.anycubicloud.com/")
    assert http.calls[0].headers["XX-Token"] == "cn-token"
    assert http.calls[0].headers["Xx-Device-Type"] == "pcf"
    assert client.token_state.auth_access_token is None


async def test_exchange_retried_once_after_2s(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", EXCHANGE, refused(), exchange_ok())
    http.add("GET", USER, envelope(USER_INFO))
    client = make_client(http, secrets, user_token=None, access_token="access")
    await client.check()
    assert no_sleep == [2.0]
    assert len(http.calls_to(EXCHANGE)) == 2
    assert client.auth_mode is AuthMode.SLICER


async def test_exchange_transport_error_propagates(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", EXCHANGE, aiohttp.ClientConnectionError())
    client = make_client(http, secrets, user_token=None, access_token="access")
    with pytest.raises(ServiceUnavailableError):
        await client.check()
    assert len(http.calls_to(EXCHANGE)) == 1
    assert no_sleep == []


async def test_web_fallback(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", EXCHANGE, refused())
    http.add("GET", USER, envelope(USER_INFO))
    client = AnycubicCloudClient.from_entry(
        aiohttp_session(http), secrets, token="web-token-as-slicer", auth_mode=3
    )
    await client.check()
    assert len(http.calls_to(EXCHANGE)) == 2
    assert client.auth_mode is AuthMode.WEB
    assert client.tokens_changed
    user_call = http.calls_to(USER)[0]
    # the mixed header set of 2.x (PROTOCOL A §2.8, Q1)
    assert user_call.headers["XX-Token"] == "web-token-as-slicer"
    assert user_call.headers["Xx-Device-Type"] == "pcf"
    assert user_call.headers["Xx-Version"] == "V3.0.0"
    assert user_call.headers["User-Agent"].startswith("Mozilla")
    assert user_call.headers["Origin"] == "https://uc.makeronline.com"
    assert client.export_token_store() == {
        "auth_token": "web-token-as-slicer",
        "auth_access_token": None,
        "device_id": None,
        "auth_mode": 1,
    }
    assert not client.supports_mqtt_login


async def test_web_fallback_then_rejected_keeps_exchange_reason(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", EXCHANGE, refused(EXPIRED_MSG))
    http.add("GET", USER, refused())
    client = make_client(http, secrets, user_token=None, access_token="access")
    with pytest.raises(CredentialsRejectedError) as info:
        await client.check()
    assert info.value.reason is RejectReason.EXPIRED
    assert info.value.server_message == EXPIRED_MSG
    # the fallback fires at most once
    http.calls.clear()
    with pytest.raises(CredentialsRejectedError):
        await client.check()
    assert http.calls_to(EXCHANGE) == []


async def test_exchange_refused_without_fallback(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", EXCHANGE, refused(EXPIRED_MSG))
    client = make_client(http, secrets, user_token=None, access_token="access")
    client._web_fallback_done = True
    with pytest.raises(CredentialsRejectedError) as info:
        await client.check()
    assert info.value.reason is RejectReason.EXPIRED


@pytest.mark.parametrize("data", [None, {"id": None}, {"user_email": "x"}, "text"])
async def test_user_info_rejections(
    http: FakeSession, secrets: CloudSecrets, data: Any
) -> None:
    http.add("GET", USER, envelope(data, msg="User does not exist"))
    client = make_client(http, secrets, mode=AuthMode.WEB)
    with pytest.raises(CredentialsRejectedError) as info:
        await client.check()
    assert info.value.reason is RejectReason.INVALID


async def test_user_info_non_integer_id_is_accepted(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, envelope({"id": "abc", "user_email": "", "mobile": None}))
    account = await make_client(http, secrets, mode=AuthMode.WEB).check()
    assert account.user_id is None
    assert account.identifier == "abc"
    assert account.mqtt_identity is None


async def test_displaced_user_token_is_dropped_and_exchanged_again(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, refused(), envelope(USER_INFO))
    http.add("POST", EXCHANGE, exchange_ok("NEW"))
    client = make_client(http, secrets, user_token="STALE", access_token="access")
    await client.check()
    assert [c.path for c in http.calls] == [USER, EXCHANGE, USER]
    assert http.calls[0].headers["XX-Token"] == "STALE"
    assert http.calls[2].headers["XX-Token"] == "NEW"
    assert client.tokens_changed


async def test_displaced_retry_runs_once(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("GET", USER, refused())
    http.add("POST", EXCHANGE, exchange_ok("NEW"))
    client = make_client(http, secrets, user_token="STALE", access_token="access")
    with pytest.raises(CredentialsRejectedError):
        await client.check()
    assert [c.path for c in http.calls] == [USER, EXCHANGE, USER]


async def test_token_store_overlay(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("GET", USER, envelope(USER_INFO))
    store = {
        "auth_token": "STORED",
        "auth_access_token": "access",
        "device_id": None,
        "auth_mode": 1,
        "app_id": "ignored",
        "app_secret": "ignored",
        "app_version": "ignored",
        "app_client_id": "ignored",
    }
    client = AnycubicCloudClient.from_entry(
        aiohttp_session(http), secrets, token="access", auth_mode=3, store=store
    )
    assert client.auth_mode is AuthMode.SLICER  # the stored auth_mode is ignored
    await client.check()
    assert http.calls_to(EXCHANGE) == []
    assert http.calls[0].headers["XX-Token"] == "STORED"
    exported = client.export_token_store()
    assert set(exported) == {
        "auth_token",
        "auth_access_token",
        "device_id",
        "auth_mode",
    }
    assert not client.apply_token_store({"unrelated": 1})


async def test_stored_null_overwrites(http: FakeSession, secrets: CloudSecrets) -> None:
    client = AnycubicCloudClient.from_entry(
        aiohttp_session(http),
        secrets,
        token="web",
        auth_mode=1,
        device_id="d",
        store={"auth_token": None, "device_id": None},
    )
    assert client.token_state.auth_token is None
    assert client.token_state.device_id is None


def test_repr_hides_tokens(http: FakeSession, secrets: CloudSecrets) -> None:
    client = make_client(
        http, secrets, user_token="SECRET-USER", access_token="SECRET-ACC"
    )
    text = repr(client)
    assert "SECRET" not in text
    assert "SLICER" in text
    assert client.region is Region.INTERNATIONAL
    assert client.secrets is secrets
    assert client.session is http


def test_client_needs_cloud_secrets(http: FakeSession) -> None:
    with pytest.raises(TypeError):
        AnycubicCloudClient(aiohttp_session(http), object())  # type: ignore[arg-type]


# -- sign_in_any (PROTOCOL A §2.9) -----------------------------------------------------


def test_sign_in_order() -> None:
    assert sign_in_order("eyJabc", "dev") == [AuthMode.ANDROID]
    assert sign_in_order("eyJabc", None) == [AuthMode.SLICER, AuthMode.WEB]
    assert sign_in_order("opaque", None) == [AuthMode.WEB, AuthMode.SLICER]


async def test_sign_in_any_first_success_wins(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", EXCHANGE, exchange_ok())
    http.add("GET", USER, envelope(USER_INFO))
    result = await sign_in_any(aiohttp_session(http), secrets, "eyJtoken.x.y")
    assert result.auth_mode is AuthMode.SLICER
    assert result.account.user_id == 424242
    assert result.tokens.auth_token == "EXCHANGED"
    assert result.client.account is result.account


async def test_sign_in_any_reports_the_mode_tried_after_fallback(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", EXCHANGE, refused())
    http.add("GET", USER, envelope(USER_INFO))
    result = await sign_in_any(aiohttp_session(http), secrets, "eyJweb.token.z")
    assert result.auth_mode is AuthMode.SLICER
    assert result.tokens.auth_mode is AuthMode.WEB


async def test_sign_in_any_tries_the_other_mode(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    answers = [refused(), refused(), envelope(USER_INFO)]

    def user_info(call: Call) -> Any:
        return answers.pop(0)

    http.add("GET", USER, user_info)
    http.add("POST", EXCHANGE, exchange_ok())
    result = await sign_in_any(aiohttp_session(http), secrets, "opaque-token")
    # WEB first (refused), then SLICER: exchange, userInfo refused, displaced
    # retry: exchange again, userInfo accepted.
    assert result.auth_mode is AuthMode.SLICER


async def test_sign_in_any_android_only_with_device(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, refused())
    with pytest.raises(CredentialsRejectedError) as info:
        await sign_in_any(aiohttp_session(http), secrets, "tok", device_id="dev")
    assert info.value.reason is RejectReason.INVALID
    assert len(http.calls) == 1
    assert http.calls[0].headers["XX-Device-Id"] == "dev"


async def test_sign_in_any_wrong_token_type(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", EXCHANGE, refused())
    http.add("GET", USER, refused())
    token = make_jwt({"tokenType": "id-token", "iss": "https://uc.makeronline.com"})
    with pytest.raises(CredentialsRejectedError) as info:
        await sign_in_any(aiohttp_session(http), secrets, token)
    assert info.value.reason is RejectReason.WRONG_TOKEN_TYPE


async def test_sign_in_any_china_skips_token_type(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("GET", USER, refused())
    token = make_jwt({"tokenType": "id-token"})
    with pytest.raises(CredentialsRejectedError) as info:
        await sign_in_any(aiohttp_session(http), secrets, token, region="china")
    assert info.value.reason is RejectReason.INVALID
    assert http.calls_to(EXCHANGE) == []


async def test_sign_in_any_stops_on_transport_error(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", USER, envelope(None, msg="request error"))
    with pytest.raises(ServiceUnavailableError):
        await sign_in_any(aiohttp_session(http), secrets, "opaque")
    assert len(http.calls) == 1
