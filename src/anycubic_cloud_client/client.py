"""The Anycubic cloud HTTP client (PROTOCOL Parts A, B and D).

One :class:`AnycubicCloudClient` holds one account's tokens for one region.
It signs every request (A §3), classifies failures (A §4.4), runs the sign-in
sequence with its retries and fallbacks (A §2.6), and offers one method per
endpoint and order the integration uses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import aiohttp

from .credentials import CloudSecrets
from .errors import (
    AnycubicCloudError,
    CloudFileNotFoundError,
    CredentialsRejectedError,
    NoCameraCredentialsError,
    OrderRefusedError,
    PrinterRemovedError,
    RejectReason,
    RenameFailedError,
    ServiceUnavailableError,
    StorageFullError,
    UnexpectedResponseError,
    UploadError,
)
from .models import (
    Account,
    AceUnit,
    CameraCredentials,
    CloudFile,
    GcodeInfo,
    Job,
    JobDetail,
    PaintInfo,
    PrinterDetail,
    PrinterModel,
    PrinterSummary,
    StorageQuota,
    as_int,
    as_str,
    select_latest_job,
)
from .orders import (
    ABSENT,
    Axis,
    FeedType,
    FileSource,
    MoveType,
    Order,
    ai_settings_data,
    auto_feed_data,
    build_order_body,
    delete_file_data,
    drying_data,
    fan_data,
    feed_data,
    light_data,
    move_data,
    set_slot_data,
    temperature_data,
)
from .printing import (
    SlotAssignment,
    TaskSettings,
    ams_info,
    build_slot_mapping,
    cloud_file_print_data,
    gcode_colors,
    parse_gcode_header,
    printer_file_print_data,
    validate_slots,
)
from .regions import BROWSER_USER_AGENT, AuthMode, Region
from .signing import build_headers, make_android_device_id
from .tokens import TokenState, decode_claims, store_overlay

_LOGGER = logging.getLogger(__name__)

# -- constants (PROTOCOL A §3.7, §3.8; B §8.4, §9) -------------------------

#: Light type sent when the printer has reported none (B §5.4.7).
DEFAULT_LIGHT_TYPE = 1
#: Token exchange: 2 attempts, 2 s apart.
EXCHANGE_ATTEMPTS = 2
EXCHANGE_RETRY_DELAY = 2.0
#: Start print: 3 attempts, 3 s after each ``No file found``.
START_PRINT_ATTEMPTS = 3
START_PRINT_RETRY_DELAY = 3.0
#: Slow-call warning: over 20 s, at most once per 10 minutes.
SLOW_CALL_SECONDS = 20
SLOW_CALL_WARNING_INTERVAL = 600.0
#: The job list is always page 1 with 2000 records.
JOB_LIST_LIMIT = 2000
#: The cloud file list default page size.
CLOUD_FILE_LIST_LIMIT = 10

MSG_REQUEST_ERROR = "request error"
MSG_NO_FILE_FOUND = "No file found"
MSG_SESSION_EXPIRED = "Login information has expired. Please login again."
CODE_PRINTER_REMOVED = 1007

PARSE_ERROR = "Unexpected error parsing Anycubic response, server maintenance?"
NO_CAMERA_MESSAGE = (
    "No camera credentials: the printer has no camera, or another Anycubic "
    "session (slicer or phone app) holds the account"
)

type Params = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """One workbench API answer (PROTOCOL A §4.1)."""

    code: int | None
    msg: str | None
    data: Any = field(repr=False)
    raw: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PrintStartResult:
    """What a print start returns (PROTOCOL D §2.5.2 step 7)."""

    msgid: str | None
    printer_id: int
    saved_in_cloud: bool
    file_name: str | None
    cloud_file_id: int | None
    gcode_id: int | None = None
    colors: tuple[PaintInfo, ...] = ()
    mapping: tuple[SlotAssignment, ...] = ()


def _reason(message: str | None) -> RejectReason:
    if message == MSG_SESSION_EXPIRED:
        return RejectReason.EXPIRED
    return RejectReason.INVALID


class AnycubicCloudClient:
    """Client for one Anycubic cloud account in one region.

    Build it from a config entry with :meth:`from_entry`, lay the token store
    over it with :meth:`apply_token_store`, then call :meth:`check`. After a
    call that changed the tokens (a new exchange, the web fallback)
    :attr:`tokens_changed` is ``True`` until :meth:`mark_tokens_saved`.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        secrets: CloudSecrets,
        *,
        region: Region = Region.INTERNATIONAL,
        auth_mode: AuthMode = AuthMode.WEB,
        user_token: str | None = None,
        access_token: str | None = None,
        device_id: str | None = None,
        debug_api_calls: bool = False,
        request_timeout: float | None = None,
    ) -> None:
        if not isinstance(secrets, CloudSecrets):
            raise TypeError("secrets must be a CloudSecrets")
        self._session = session
        self._secrets = secrets
        self._region = region
        # Per-mode header values stay those of the mode the client was built
        # with, also after the web fallback (Q1 in docs/QUESTIONS.md).
        self._profile_mode = auth_mode
        self._auth_mode = auth_mode
        self._user_token = user_token or None
        self._access_token = access_token or None
        self._device_id = device_id or None
        self._web_fallback_done = False
        self._tokens_changed = False
        self._account: Account | None = None
        self.debug_api_calls = debug_api_calls
        self._timeout = (
            aiohttp.ClientTimeout(total=request_timeout) if request_timeout else None
        )
        self._last_slow_warning: float | None = None
        self._clock: Callable[[], float] = time.monotonic
        # Light types reported over MQTT, by printer key (B §5.4.7, C §4.7),
        # and the printer keys learned from the printer lists, by id.
        self._light_types: dict[str, set[int]] = {}
        self._printer_keys: dict[int, str] = {}

    @classmethod
    def from_entry(
        cls,
        session: aiohttp.ClientSession,
        secrets: CloudSecrets,
        *,
        token: str,
        auth_mode: AuthMode | int | None,
        region: Region | str | None = None,
        device_id: str | None = None,
        store: Mapping[str, Any] | None = None,
        debug_api_calls: bool = False,
    ) -> AnycubicCloudClient:
        """Build a client from a config entry (PROTOCOL A §2.6.5 steps 1-3).

        For SLICER outside China the pasted token is the access token and the
        user token starts empty; otherwise the pasted token is the user token.
        ``store``, when given, is overlaid with :meth:`apply_token_store`. To
        retry "from the entry alone" (step 6) build again without ``store``.
        """
        mode = AuthMode.resolve(auth_mode)
        resolved_region = Region.resolve(region)
        exchanges = mode is AuthMode.SLICER and resolved_region is not Region.CHINA
        client = cls(
            session,
            secrets,
            region=resolved_region,
            auth_mode=mode,
            user_token=None if exchanges else token,
            access_token=token if exchanges else None,
            device_id=device_id,
            debug_api_calls=debug_api_calls,
        )
        if store:
            client.apply_token_store(store)
        return client

    def __repr__(self) -> str:
        return (
            f"AnycubicCloudClient(region={self._region.value}, "
            f"auth_mode={self._auth_mode.name}, tokens={self.token_state!r})"
        )

    # -- properties -----------------------------------------------------------

    @property
    def region(self) -> Region:
        return self._region

    @property
    def auth_mode(self) -> AuthMode:
        """The current mode; WEB after the web fallback."""
        return self._auth_mode

    @property
    def secrets(self) -> CloudSecrets:
        return self._secrets

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    @property
    def account(self) -> Account | None:
        """The account from the last successful :meth:`check`."""
        return self._account

    @property
    def user_token(self) -> str | None:
        """The user token in use (the MQTT login is derived from it)."""
        return self._user_token

    @property
    def token_state(self) -> TokenState:
        return TokenState(
            auth_token=self._user_token,
            auth_access_token=self._access_token,
            device_id=self._device_id,
            auth_mode=self._auth_mode,
        )

    @property
    def tokens_changed(self) -> bool:
        """``True`` when the tokens changed since :meth:`mark_tokens_saved`."""
        return self._tokens_changed

    @property
    def supports_mqtt_login(self) -> bool:
        """SLICER or ANDROID with a user token; never WEB (PROTOCOL A §2.12)."""
        return self._auth_mode.supports_mqtt and bool(self._user_token)

    # -- token store ----------------------------------------------------------

    def apply_token_store(self, store: Mapping[str, Any]) -> bool:
        """Lay a 2.x token store over the entry's tokens (PROTOCOL A §2.10).

        Only ``auth_token``, ``auth_access_token`` and ``device_id`` are read,
        and only when present; a stored ``null`` does overwrite. Returns
        whether any key was applied.
        """
        overlay = store_overlay(store)
        if "auth_token" in overlay:
            self._user_token = overlay["auth_token"]
        if "auth_access_token" in overlay:
            self._access_token = overlay["auth_access_token"]
        if "device_id" in overlay:
            self._device_id = overlay["device_id"]
        return bool(overlay)

    def export_token_store(self) -> dict[str, Any]:
        """The token-store dict to save: never any ``app_*`` key."""
        return self.token_state.to_store()

    def mark_tokens_saved(self) -> None:
        self._tokens_changed = False

    def _set_user_token(self, token: str | None) -> None:
        if token != self._user_token:
            self._user_token = token
            self._tokens_changed = True

    # -- transport --------------------------------------------------------------

    def _headers(self, *, with_token: bool) -> dict[str, str]:
        if self._profile_mode is AuthMode.ANDROID and not self._device_id:
            # Should never be needed: Android is only chosen with a device id.
            self._device_id = make_android_device_id()
            self._tokens_changed = True
        return build_headers(
            app_id=self._secrets.app_id,
            app_secret=self._secrets.app_secret,
            profile_mode=self._profile_mode,
            current_mode=self._auth_mode,
            region=self._region,
            token=(self._user_token or "") if with_token else None,
            device_id=self._device_id,
        )

    def _log_timing(self, url: str, started: float) -> None:
        elapsed = self._clock() - started
        if self.debug_api_calls:
            _LOGGER.debug("Anycubic API %s took %.2fs", url, elapsed)
        if int(elapsed) > SLOW_CALL_SECONDS:
            now = self._clock()
            last = self._last_slow_warning
            if last is None or now - last >= SLOW_CALL_WARNING_INTERVAL:
                self._last_slow_warning = now
                _LOGGER.warning(
                    "Responses from server are taking over %ss (Took %ss)",
                    SLOW_CALL_SECONDS,
                    int(elapsed),
                )

    async def request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        params: Params | None = None,
        body: Mapping[str, Any] | None = None,
        with_token: bool = True,
    ) -> ApiResponse:
        """Send one signed request and return its envelope (PROTOCOL A §3-§4).

        GET parameters go in the query string (as text); a POST sends its
        parameters as JSON (``{}`` when there are none). HTTP status codes are
        not inspected. Raises :class:`ServiceUnavailableError` for transport
        errors, non-JSON answers and ``msg`` = ``request error``,
        :class:`PrinterRemovedError` for ``code`` 1007 and
        :class:`UnexpectedResponseError` for a JSON answer that is not an
        object.
        """
        url = self._region.endpoints.url(path)
        kwargs: dict[str, Any] = {"headers": self._headers(with_token=with_token)}
        if method == "GET":
            if params:
                kwargs["params"] = {key: str(value) for key, value in params.items()}
        else:
            kwargs["data"] = json.dumps(dict(body or {}))
        if self._timeout is not None:
            kwargs["timeout"] = self._timeout
        started = self._clock()
        try:
            async with self._session.request(method, url, **kwargs) as response:
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Anycubic API %s failed: %s", url, type(err).__name__)
            raise ServiceUnavailableError(PARSE_ERROR) from err
        finally:
            self._log_timing(url, started)
        if not isinstance(payload, Mapping):
            raise UnexpectedResponseError(f"{path}: the answer is not a JSON object")
        msg = as_str(payload.get("msg"))
        code = as_int(payload.get("code"))
        if msg == MSG_REQUEST_ERROR:
            raise ServiceUnavailableError(
                "The Anycubic cloud answered 'request error' "
                "(maintenance or rate limit)"
            )
        # 1007 is only recorded on printer calls; it is checked on every call,
        # the conservative side: "not ready", never re-authentication (Q9).
        # Success is never judged by ``code``, only by ``data`` (Q4).
        if code == CODE_PRINTER_REMOVED:
            raise PrinterRemovedError(
                msg or "The printer has been removed from the cloud"
            )
        return ApiResponse(code=code, msg=msg, data=payload.get("data"), raw=payload)

    @staticmethod
    def _data_map(response: ApiResponse, what: str) -> Mapping[str, Any]:
        if not isinstance(response.data, Mapping):
            raise UnexpectedResponseError(f"{what}: no data object in the answer")
        return response.data

    @staticmethod
    def _data_list(
        response: ApiResponse, what: str, *, null_is_empty: bool
    ) -> list[Any]:
        if response.data is None and null_is_empty:
            return []
        if not isinstance(response.data, list):
            raise UnexpectedResponseError(f"{what}: no data list in the answer")
        return response.data

    # -- sign-in (PROTOCOL A §2.6) --------------------------------------------

    def _can_web_fallback(self) -> bool:
        return (
            self._auth_mode is AuthMode.SLICER
            and bool(self._access_token)
            and not self._user_token
            and not self._web_fallback_done
        )

    async def _exchange(self) -> str | None:
        """Exchange the access token (A §2.6.3). Returns the failure ``msg``."""
        message: str | None = None
        for attempt in range(EXCHANGE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(EXCHANGE_RETRY_DELAY)
            response = await self.request(
                "POST",
                "/v3/public/loginWithAccessToken",
                body={
                    "device_type": self._profile_mode.profile.device_type,
                    "access_token": self._access_token,
                },
                with_token=False,
            )
            data = response.data
            token = data.get("token") if isinstance(data, Mapping) else None
            if isinstance(token, str) and token:
                self._set_user_token(token)
                return None
            message = response.msg
            _LOGGER.debug("Token exchange refused (attempt %s)", attempt + 1)
        if self._can_web_fallback():
            # PROTOCOL A §2.8: a web token taken for a slicer token.
            _LOGGER.debug("Retrying the pasted token as a web token")
            self._web_fallback_done = True
            self._user_token = self._access_token
            self._access_token = None
            self._auth_mode = AuthMode.WEB
            self._tokens_changed = True
            return message
        raise CredentialsRejectedError(
            "The Anycubic cloud refused the token",
            reason=_reason(message),
            server_message=message,
        )

    async def get_user_info(self) -> Account:
        """userInfo (E3): the credentials check (PROTOCOL A §2.6.1)."""
        response = await self.request("GET", "/user/profile/userInfo")
        data = response.data
        if not isinstance(data, Mapping) or data.get("id") is None:
            raise CredentialsRejectedError(
                "The Anycubic cloud did not accept the token",
                server_message=response.msg,
            )
        account = Account.from_data(data)
        self._account = account
        return account

    async def _check_once(self) -> Account:
        exchange_message: str | None = None
        if (
            self._auth_mode is AuthMode.SLICER
            and self._access_token
            and not self._user_token
        ):
            exchange_message = await self._exchange()
        try:
            return await self.get_user_info()
        except CredentialsRejectedError as err:
            if exchange_message is not None:
                err.reason = _reason(exchange_message)
                err.server_message = exchange_message
            raise

    async def check(self) -> Account:
        """Sign in if needed and check the credentials (PROTOCOL A §2.6.5 4-5).

        Exchanges an access token when there is no user token (2 attempts,
        2 s apart, then the web fallback once), runs userInfo, and when that
        fails for a SLICER client holding both tokens drops the (displaced)
        user token and runs once more.

        Raises :class:`CredentialsRejectedError` only for a credentials
        verdict; transport and parse problems raise other errors.
        """
        try:
            return await self._check_once()
        except CredentialsRejectedError:
            if not (
                self._auth_mode is AuthMode.SLICER
                and self._access_token
                and self._user_token
            ):
                raise
            _LOGGER.debug(
                "Stored user token refused; exchanging the access token again"
            )
            self._set_user_token(None)
            return await self._check_once()

    # -- account and printers (PROTOCOL B §1-§2) ------------------------------

    async def get_printers(self) -> list[PrinterSummary]:
        """The account's printers (E4); empty while the only printer is in LAN Mode."""
        response = await self.request("GET", "/work/printer/getPrinters")
        items = self._data_list(response, "printer list", null_is_empty=False)
        return self._learn_keys(
            [PrinterSummary.from_data(i) for i in items if isinstance(i, Mapping)]
        )

    async def get_printers_status(self) -> list[PrinterSummary]:
        """E5: printer records of the same shape as E4 (not used by 2.x)."""
        response = await self.request("GET", "/work/printer/printersStatus")
        items = self._data_list(response, "printers status", null_is_empty=True)
        return self._learn_keys(
            [PrinterSummary.from_data(i) for i in items if isinstance(i, Mapping)]
        )

    async def get_printer(self, printer_id: int) -> PrinterDetail:
        """One printer's detail (E6). Code 1007 raises :class:`PrinterRemovedError`."""
        response = await self.request(
            "GET", "/v2/printer/info", params={"id": printer_id}
        )
        detail = PrinterDetail.from_data(self._data_map(response, "printer detail"))
        if detail.id is not None and detail.id != printer_id:
            raise UnexpectedResponseError(
                "printer detail: the answer is for another printer"
            )
        self._learn_keys([detail])
        return detail

    def _learn_keys[P: PrinterSummary | PrinterDetail](
        self, printers: list[P]
    ) -> list[P]:
        for printer in printers:
            if printer.id is not None and printer.key:
                self._printer_keys[printer.id] = printer.key
        return printers

    # -- light types (PROTOCOL B §5.4.7, C §4.7) ------------------------------

    def remember_printer_key(self, printer_id: int, printer_key: str) -> None:
        """Tie a printer id to its key (learned anyway from the printer lists)."""
        self._printer_keys[int(printer_id)] = printer_key

    def note_light_types(self, printer_key: str, types: Iterable[int | None]) -> None:
        """Record light types the printer reported (its ``light`` messages).

        :class:`~anycubic_cloud_client.mqtt.CloudMqttClient` calls this for
        every ``light`` report; an integration may also call it to restore
        types it remembered across a restart.
        """
        known = self._light_types.setdefault(printer_key, set())
        known.update(int(t) for t in types if t is not None)

    def light_type(self, printer_id: int) -> int | None:
        """The printer's light type: the lowest reported, ``None`` if none yet."""
        key = self._printer_keys.get(int(printer_id))
        known = self._light_types.get(key) if key is not None else None
        return min(known) if known else None

    async def get_printer_status(self, printer_id: int) -> Any:
        """E7 ``GET /v2/Printer/status`` (capital P); raw ``data`` (shape unknown)."""
        response = await self.request(
            "GET", "/v2/Printer/status", params={"id": printer_id}
        )
        return response.data

    async def get_printer_models(self) -> list[PrinterModel]:
        """E8: the printer model catalogue."""
        response = await self.request("GET", "/v2/printer/all")
        data = self._data_map(response, "printer models")
        return [
            PrinterModel.from_data(entry)
            for entry in data.get("printer_type") or []
            if isinstance(entry, Mapping)
        ]

    async def rename_printer(
        self, printer_id: int, name: str, *, old_name: str | None = None
    ) -> str:
        """Rename a printer (E11, ``POST /work/printer/Info``; D §5.4)."""
        if not name:
            raise ValueError("the new name must not be empty")
        response = await self.request(
            "POST", "/work/printer/Info", body={"id": str(printer_id), "name": name}
        )
        stored = as_str(self._data_map(response, "rename").get("name"))
        if stored == name:
            return name
        if old_name is not None and stored == old_name:
            raise RenameFailedError("The cloud reverted the printer name")
        raise RenameFailedError("The cloud did not store the printer name")

    # -- jobs (PROTOCOL B §3) ---------------------------------------------------

    async def get_projects(
        self,
        *,
        page: int = 1,
        limit: int = JOB_LIST_LIMIT,
        print_status: int | None = None,
    ) -> list[Job]:
        """The account-wide job list, newest first (E12); null reads as empty."""
        params: dict[str, object] = {"page": page, "limit": limit}
        if print_status is not None:
            params["print_status"] = print_status
        response = await self.request("GET", "/work/project/getProjects", params=params)
        items = self._data_list(response, "job list", null_is_empty=True)
        return [Job.from_data(i, self._region) for i in items if isinstance(i, Mapping)]

    async def get_latest_jobs(
        self, printer_ids: Sequence[int]
    ) -> dict[int, Job | None]:
        """Every printer's latest job from **one** job-list call (B §3.1.4, §8.2)."""
        jobs = await self.get_projects()
        return {
            printer_id: select_latest_job(jobs, printer_id)
            for printer_id in printer_ids
        }

    async def get_job_detail(self, job_id: int) -> JobDetail:
        """Job detail (E13): speed-mode names and temperature limits."""
        response = await self.request("GET", "/v2/project/info", params={"id": job_id})
        return JobDetail.from_data(self._data_map(response, "job detail"))

    async def get_print_history(self) -> Any:
        """E14 (not used by 2.x): raw ``data``."""
        return (await self.request("GET", "/v2/project/printHistory")).data

    async def get_job_monitor(self, job_id: int) -> Any:
        """E15 (not used by 2.x): raw ``data``."""
        response = await self.request(
            "GET", "/v2/project/monitor", params={"id": job_id}
        )
        return response.data

    async def get_gcode_info(self, gcode_id: int) -> GcodeInfo:
        """Sliced-file detail (E16): the cloud file id and the colour list."""
        response = await self.request(
            "GET", "/work/gcode/infoFdm", params={"id": gcode_id}
        )
        return GcodeInfo.from_data(self._data_map(response, "gcode info"))

    async def fetch_image(self, url: str) -> bytes:
        """A plain unsigned GET of a job or model picture (X2)."""
        try:
            async with self._session.get(url) as response:
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ServiceUnavailableError("Could not fetch the image") from err

    # -- firmware (PROTOCOL B §6, D §4.2) ------------------------------------

    async def update_printer_firmware(self, printer: PrinterDetail) -> str | None:
        """Start a printer firmware update (E24) when the cloud offers one.

        Nothing is sent unless ``need_update`` is 1. Returns the version being
        installed when the cloud answers ``update_status`` 1, else ``None``.
        2.x sends the **installed** version as ``target_version`` and that
        works (Q14 in docs/QUESTIONS.md).
        """
        firmware = printer.firmware
        if printer.id is None or firmware is None or not firmware.need_update:
            return None
        response = await self.request(
            "GET",
            "/work/printer/update_version",
            params={
                "id": printer.id,
                "target_version": firmware.firmware_version or "",
            },
        )
        data = response.data if isinstance(response.data, Mapping) else {}
        if as_int(data.get("update_status")) == 1:
            return firmware.target_version
        return None

    async def update_ace_firmware(
        self, printer: PrinterDetail, box_index: int
    ) -> str | None:
        """Start an ACE firmware update (E25) for box 0 or 1, when one is offered."""
        if printer.id is None or not printer.has_ace:
            return None
        if not 0 <= box_index < len(printer.ace_firmware):
            return None
        entry = printer.ace_firmware[box_index]
        if not entry.need_update:
            return None
        response = await self.request(
            "POST",
            "/v2/printer/update_multi_color_box_version",
            body={"id": int(printer.id), "box_id": int(box_index)},
        )
        data = response.data if isinstance(response.data, Mapping) else {}
        target = as_str(data.get("target_version"))
        if target is not None and target == entry.target_version:
            return target
        return None

    async def update_all_ace_firmware(self, printer: PrinterDetail) -> list[str | None]:
        """Run :meth:`update_ace_firmware` for every ACE entry in turn."""
        return [
            await self.update_ace_firmware(printer, index)
            for index in range(len(printer.ace_firmware))
        ]

    # -- orders (PROTOCOL B §5) -----------------------------------------------

    async def send_order(
        self,
        printer_id: int,
        order_id: int,
        data: Any = ABSENT,
        project_id: int | None = None,
        *,
        ams: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Send one order in its required body shape; returns the ``msgid``.

        The order's effect, and any data it asks for, arrives over MQTT.
        ``Operation successful`` never proves anything happened. Raises
        :class:`CloudFileNotFoundError` for ``No file found`` and
        :class:`OrderRefusedError` for any other null ``data``.
        """
        body = build_order_body(
            int(printer_id),
            int(order_id),
            data=data,
            project_id=project_id,
            ams_info=ams,
        )
        response = await self.request("POST", "/work/operation/sendOrder", body=body)
        if response.data is None:
            if response.msg == MSG_NO_FILE_FOUND:
                raise CloudFileNotFoundError("The file does not exist in the cloud")
            raise OrderRefusedError(
                f"Order {order_id} refused: {response.msg}", server_message=response.msg
            )
        msgid = (
            response.data.get("msgid") if isinstance(response.data, Mapping) else None
        )
        if isinstance(msgid, str) and msgid:
            return msgid
        _LOGGER.debug("Order %s answered without a message id", order_id)
        return None

    async def pause_print(self, printer_id: int, job_id: int) -> str | None:
        """Order 2 for the latest job."""
        return await self.send_order(printer_id, Order.PAUSE_PRINT, None, job_id)

    async def resume_print(self, printer_id: int, job_id: int) -> str | None:
        """Order 3 for the latest job."""
        return await self.send_order(printer_id, Order.RESUME_PRINT, None, job_id)

    async def cancel_print(self, printer_id: int, job_id: int) -> str | None:
        """Order 4 for the latest job."""
        return await self.send_order(printer_id, Order.STOP_PRINT, None, job_id)

    async def set_light(
        self,
        printer_id: int,
        on: bool,
        brightness: int | None = None,
        *,
        light_type: int | None = None,
        job_id: int | None = None,
    ) -> str | None:
        """Order 1233: shape J with the latest job's id, shape P without a job.

        ``light_type`` defaults to the type the printer last reported over
        MQTT (:meth:`light_type`), and to 1 when none has been reported
        (PROTOCOL B §5.4.7). An explicit value wins.
        """
        if light_type is None:
            reported = self.light_type(printer_id)
            light_type = DEFAULT_LIGHT_TYPE if reported is None else reported
        return await self.send_order(
            printer_id,
            Order.SET_LIGHT_STATUS,
            light_data(on, brightness, light_type),
            job_id,
        )

    async def set_temperature(
        self, printer_id: int, *, nozzle: int | None = None, bed: int | None = None
    ) -> str | None:
        """Order 1216 (works idle). Nothing is sent when neither target is given."""
        data = temperature_data(nozzle, bed)
        if data is None:
            return None
        return await self.send_order(printer_id, Order.SET_TEMPERATURE, data)

    async def set_fan_speed(
        self,
        printer_id: int,
        *,
        fan_speed_pct: int | None = None,
        aux_fan_speed_pct: int | None = None,
        box_fan_level: int | None = None,
    ) -> str | None:
        """Order 1221 (works idle): exactly one fan per order."""
        data = fan_data(fan_speed_pct, aux_fan_speed_pct, box_fan_level)
        return await self.send_order(printer_id, Order.SET_FAN_SPEED, data)

    async def set_print_settings(
        self,
        printer_id: int,
        job_id: int,
        settings: Mapping[str, int | float],
        *,
        job: JobDetail | None = None,
    ) -> str | None:
        """Order 6 with only the keys being changed (PROTOCOL B §5.4.3).

        With ``job`` (the job detail, E13) the values are validated first: a
        speed mode must be one the job offers, temperatures must lie within
        the job's limits, fans within 0-100. The server refuses order 6 when
        no job is running.
        """
        if not settings:
            raise ValueError("give at least one setting")
        if job is not None:
            validate_print_settings(settings, job)
        return await self.send_order(
            printer_id, Order.PRINT_SETTINGS, {"settings": dict(settings)}, job_id
        )

    async def set_speed_mode(
        self, printer_id: int, job_id: int, mode: int, job: JobDetail
    ) -> str | None:
        """Order 6 ``print_speed_mode``, validated against the job's modes."""
        return await self.set_print_settings(
            printer_id, job_id, {"print_speed_mode": int(mode)}, job=job
        )

    async def move_axis(
        self, printer_id: int, axis: Axis, move_type: MoveType, distance: int = 0
    ) -> str | None:
        """Order 201 (shape P: string id, no ``project_id``)."""
        return await self.send_order(
            printer_id, Order.MOVE_AXLE, move_data(axis, move_type, distance)
        )

    async def home_axis(self, printer_id: int, axis: Axis) -> str | None:
        """Order 201 with ``move_type`` 2."""
        return await self.move_axis(printer_id, axis, MoveType.HOME)

    async def motors_off(self, printer_id: int) -> str | None:
        """Order 1213, ``data`` null: every axis must be homed again afterwards."""
        return await self.send_order(printer_id, Order.MOVE_AXLE_TURN_OFF, None)

    async def query_axis_position(self, printer_id: int) -> str | None:
        """Order 1214 (shape B); the position arrives as ``axis``/``query``."""
        return await self.send_order(printer_id, Order.QUERY_AXIS_POSITION)

    async def query_peripherals(self, printer_id: int) -> str | None:
        """Order 1231 (bare query): camera, ACE and USB presence."""
        return await self.send_order(printer_id, Order.QUERY_PERIPHERALS)

    async def query_light_status(self, printer_id: int) -> str | None:
        """Order 1232 (bare query)."""
        return await self.send_order(printer_id, Order.GET_LIGHT_STATUS)

    async def ace_get_info(self, printer_id: int) -> str | None:
        """Order 1206 (shape B): the full ACE list arrives over MQTT."""
        return await self.send_order(printer_id, Order.MULTI_COLOR_BOX_GET_INFO)

    async def ace_start_drying(
        self,
        printer_id: int,
        box_id: int,
        *,
        target_temp: int = 40,
        duration: int = 0,
    ) -> str | None:
        """Order 1207 for one box: ``status`` 1."""
        data = drying_data(
            [
                {
                    "id": box_id,
                    "status": 1,
                    "target_temp": target_temp,
                    "duration": duration,
                }
            ]
        )
        return await self.send_order(printer_id, Order.MULTI_COLOR_BOX_DRY, data)

    async def ace_stop_drying(
        self, printer_id: int, box_ids: Sequence[int]
    ) -> str | None:
        """Order 1207 with one ``status`` 0 entry per named box."""
        if not box_ids:
            raise ValueError("name at least one ACE unit")
        data = drying_data([{"id": box_id, "status": 0} for box_id in box_ids])
        return await self.send_order(printer_id, Order.MULTI_COLOR_BOX_DRY, data)

    async def ace_feed(
        self, printer_id: int, slot_index: int, *, box_id: int = 0
    ) -> str | None:
        """Order 1208 type 1: feed a slot (0-3) through to the hotend."""
        return await self.send_order(
            printer_id,
            Order.FEED_FILAMENT,
            feed_data(box_id, FeedType.FEED, slot_index),
        )

    async def ace_retract(self, printer_id: int, *, box_id: int = 0) -> str | None:
        """Order 1208 type 2: retract whatever is loaded (slot -1)."""
        return await self.send_order(
            printer_id, Order.FEED_FILAMENT, feed_data(box_id, FeedType.RETRACT)
        )

    async def ace_finish_feed(
        self, printer_id: int, slot_index: int, *, box_id: int = 0
    ) -> str | None:
        """Order 1208 type 3: finish a feed."""
        return await self.send_order(
            printer_id,
            Order.FEED_FILAMENT,
            feed_data(box_id, FeedType.FINISH, slot_index),
        )

    async def ace_set_slot(
        self,
        printer_id: int,
        box_id: int,
        index: int,
        color: Sequence[int],
        material: str,
    ) -> str | None:
        """Order 1211: define one slot's colour and material."""
        return await self.send_order(
            printer_id,
            Order.MULTI_COLOR_BOX_SET_SLOT,
            set_slot_data(box_id, index, color, material),
        )

    async def ace_set_auto_refill(
        self, printer_id: int, box_id: int, enabled: bool
    ) -> str | None:
        """Order 1212: run-out refill on or off."""
        return await self.send_order(
            printer_id, Order.MULTI_COLOR_BOX_AUTO_FEED, auto_feed_data(box_id, enabled)
        )

    async def request_file_list(
        self, printer_id: int, source: FileSource
    ) -> str | None:
        """Order 103 (local) or 101 (USB); the list arrives over MQTT."""
        return await self.send_order(printer_id, source.list_order, {})

    async def delete_printer_file(
        self, printer_id: int, source: FileSource, filename: str
    ) -> str | None:
        """Order 104 (local) or 102 (USB)."""
        return await self.send_order(
            printer_id, source.delete_order, delete_file_data(filename)
        )

    async def set_ai_detection(
        self,
        printer_id: int,
        enabled: bool,
        current: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Order 1243 (cloud only): ``status`` 3 on / 0 off (PROTOCOL D §5.1)."""
        return await self.send_order(
            printer_id, Order.SET_AI_SETTINGS, ai_settings_data(enabled, current)
        )

    # -- camera (PROTOCOL D §1) -------------------------------------------------

    async def _camera_order(self, printer_id: int) -> CameraCredentials | None:
        body = build_order_body(int(printer_id), Order.CAMERA_OPEN)
        response = await self.request("POST", "/work/operation/sendOrder", body=body)
        if not isinstance(response.data, Mapping):
            return None
        return CameraCredentials.from_data(response.data)

    async def open_camera(self, printer_id: int) -> CameraCredentials:
        """Order 1001 → single-use Agora credentials (PROTOCOL D §1.3-§1.5).

        When the credentials block is missing another session probably holds
        the camera: with a slicer access token the user token is dropped, a
        fresh exchange is forced and the order is sent once more. Never cache
        the result.
        """
        credentials = await self._camera_order(printer_id)
        if credentials is not None:
            return credentials
        if self._auth_mode is AuthMode.SLICER and self._access_token:
            _LOGGER.debug("No camera credentials; forcing a fresh login and retrying")
            self._set_user_token(None)
            try:
                await self.check()
            except AnycubicCloudError as err:
                raise NoCameraCredentialsError(NO_CAMERA_MESSAGE) from err
            credentials = await self._camera_order(printer_id)
            if credentials is not None:
                return credentials
        raise NoCameraCredentialsError(NO_CAMERA_MESSAGE)

    # -- cloud files (PROTOCOL B §4, D §3) ------------------------------------

    async def list_cloud_files(
        self,
        *,
        page: int = 1,
        limit: int = CLOUD_FILE_LIST_LIMIT,
        printable: bool | None = None,
        machine_type: int | None = None,
    ) -> list[CloudFile]:
        """One page of cloud files, newest first (E17)."""
        body: dict[str, Any] = {"page": int(page), "limit": int(limit)}
        if printable is not None:
            body["printable"] = 1 if printable else 0
        if machine_type is not None:
            body["machine_type"] = int(machine_type)
        response = await self.request("POST", "/work/index/files", body=body)
        items = self._data_list(response, "cloud file list", null_is_empty=True)
        return [CloudFile.from_data(i) for i in items if isinstance(i, Mapping)]

    async def delete_cloud_files(self, file_ids: Sequence[int]) -> None:
        """Delete cloud files (E18); success only when ``data`` is ``""``."""
        if not file_ids:
            raise ValueError("give at least one file id")
        response = await self.request(
            "POST", "/work/index/delFiles", body={"idArr": [int(i) for i in file_ids]}
        )
        if response.data != "":
            raise OrderRefusedError(
                "Failed to delete cloud file.", server_message=response.msg
            )

    async def get_storage_quota(self) -> StorageQuota:
        """Cloud storage quota (E19)."""
        response = await self.request("POST", "/work/index/getUserStore", body={})
        data = self._data_map(response, "storage quota")
        used, total = as_int(data.get("used_bytes")), as_int(data.get("total_bytes"))
        if used is None or total is None:
            raise UnexpectedResponseError("storage quota: no byte counts")
        flag = data.get("user_file_exists")
        return StorageQuota(
            used_bytes=used,
            total_bytes=total,
            used=as_str(data.get("used")),
            total=as_str(data.get("total")),
            user_file_exists=flag if isinstance(flag, bool) else None,
            raw=data,
        )

    async def _put_file(self, url: str, content: bytes) -> None:
        headers: dict[str, str] = {}
        if self._auth_mode is AuthMode.WEB:
            headers = {
                "User-Agent": BROWSER_USER_AGENT,
                "Origin": self._region.endpoints.origin,
            }
        # No Content-Type of our own: the HTTP client's default for raw bytes
        # is what the pre-signed URL has accepted (Q11 in docs/QUESTIONS.md).
        try:
            async with self._session.put(
                url, data=content, headers=headers
            ) as response:
                text = await response.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UploadError("Uploading to cloud storage failed") from err
        if text:
            raise UploadError("Cloud storage refused the upload")

    async def upload_file(
        self, filename: str, content: bytes, *, temporary: bool
    ) -> int:
        """Upload a file to cloud storage; returns the new cloud file id.

        E19 quota → E20 lock → PUT → E21 register → E22 unlock → E19 again
        (PROTOCOL B §4.4). The quota checks are skipped for a ``temporary``
        upload (print without saving). The unlock is sent **whether or not**
        the upload succeeded, with ``is_delete_cos`` 1 after a failure.
        """
        if not content:
            raise UploadError("The file is empty")
        size = len(content)
        name = posixpath.basename(filename.replace("\\", "/"))
        before: StorageQuota | None = None
        if not temporary:
            before = await self.get_storage_quota()
            if before.available_bytes < size:
                raise StorageFullError("Not enough cloud storage for this file")
        response = await self.request(
            "POST",
            "/v2/cloud_storage/lockStorageSpace",
            body={"size": size, "name": name, "is_temp_file": 1 if temporary else 0},
        )
        lock = self._data_map(response, "storage lock")
        lock_id = as_int(lock.get("id"))
        url = as_str(lock.get("preSignUrl"))
        if lock_id is None or not url:
            raise UploadError("The cloud did not grant an upload slot")
        succeeded = False
        try:
            await self._put_file(url, content)
            claim = await self.request(
                "POST",
                "/v2/profile/newUploadFile",
                body={"user_lock_space_id": lock_id},
            )
            file_id = (
                as_int(claim.data.get("id"))
                if isinstance(claim.data, Mapping)
                else None
            )
            if file_id is None:
                raise UploadError("The cloud did not register the uploaded file")
            succeeded = True
        finally:
            await self._unlock(lock_id, discard=not succeeded)
        if before is not None:
            after = await self.get_storage_quota()
            if before.available_bytes - after.available_bytes < size:
                raise UploadError("The uploaded file was not found in the cloud")
        return file_id

    async def _unlock(self, lock_id: int, *, discard: bool) -> None:
        try:
            await self.request(
                "POST",
                "/v2/cloud_storage/unlockStorageSpace",
                body={"id": lock_id, "is_delete_cos": 1 if discard else 0},
            )
        except AnycubicCloudError as err:
            _LOGGER.warning("Could not release the cloud upload slot: %s", err)

    # -- printing (PROTOCOL D §2) -----------------------------------------------

    async def _send_start(
        self,
        printer_id: int,
        data: Mapping[str, Any],
        mapping: Sequence[SlotAssignment],
    ) -> str | None:
        for attempt in range(START_PRINT_ATTEMPTS):
            try:
                return await self.send_order(
                    printer_id,
                    Order.START_PRINT,
                    dict(data),
                    0,
                    ams=ams_info(mapping),
                )
            except CloudFileNotFoundError:
                if attempt == START_PRINT_ATTEMPTS - 1:
                    raise
                _LOGGER.debug("Start print: file not known yet, retrying")
                await asyncio.sleep(START_PRINT_RETRY_DELAY)
        raise AssertionError("unreachable")  # pragma: no cover

    async def start_print_cloud_file(
        self,
        printer_id: int,
        file_id: int,
        *,
        delete_after: bool = False,
        mapping: Sequence[SlotAssignment] = (),
        task_settings: TaskSettings | None = None,
    ) -> str | None:
        """Order 1 for a cloud file (``filetype`` 0).

        Retried up to 3 times, 3 s apart, while the cloud answers ``No file
        found``; after that :class:`CloudFileNotFoundError` is raised.
        """
        data = cloud_file_print_data(
            file_id, delete_after=delete_after, task_settings=task_settings
        )
        return await self._send_start(printer_id, data, mapping)

    async def start_print_printer_file(
        self,
        printer_id: int,
        filename: str,
        source: FileSource = FileSource.LOCAL,
        *,
        folder: str = "",
        task_settings: TaskSettings | None = None,
    ) -> str | None:
        """Order 1 for a file on the printer (1) or its USB stick (2).

        No ``ams_info`` is sent, as in 2.x (Q12 in docs/QUESTIONS.md).
        """
        data = printer_file_print_data(
            filename, source, folder=folder, task_settings=task_settings
        )
        return await self._send_start(printer_id, data, ())

    async def print_by_gcode_id(
        self,
        printer_id: int,
        gcode_id: int,
        *,
        slots: Sequence[int] | None = None,
        ace_units: Sequence[AceUnit] = (),
        task_settings: TaskSettings | None = None,
    ) -> PrintStartResult:
        """Print a cloud file known by its gcode id (PROTOCOL D §2.5.2 steps 4-6)."""
        validate_slots(slots, ace_units)
        info = await self.get_gcode_info(gcode_id)
        if info.file_id is None:
            raise UnexpectedResponseError("gcode info: no cloud file id")
        mapping: list[SlotAssignment] = []
        if slots:
            if not info.paint_infos:
                raise UnexpectedResponseError("gcode info: the file has no colour list")
            mapping = build_slot_mapping(info.paint_infos, slots, ace_units)
        msgid = await self.start_print_cloud_file(
            printer_id, info.file_id, mapping=mapping, task_settings=task_settings
        )
        return PrintStartResult(
            msgid=msgid,
            printer_id=printer_id,
            saved_in_cloud=True,
            file_name=info.name,
            cloud_file_id=info.file_id,
            gcode_id=gcode_id,
            colors=info.paint_infos,
            mapping=tuple(mapping),
        )

    async def upload_and_print(
        self,
        printer_id: int,
        filename: str,
        content: bytes,
        *,
        save_in_cloud: bool,
        slots: Sequence[int] | None = None,
        ace_units: Sequence[AceUnit] = (),
        task_settings: TaskSettings | None = None,
    ) -> PrintStartResult:
        """Upload a file and print it (PROTOCOL D §2.5.2 and §2.5.3).

        ``slots`` are 0-based global ACE slots, one per colour; required on a
        printer with an ACE (``ace_units`` non-empty), forbidden without one.
        With ``save_in_cloud`` the file stays in the account and its colour
        list comes from the cloud's parse (E17 then E16); without it the file
        is a temporary upload deleted after printing and its colour list is
        read from the G-code header.
        """
        validate_slots(slots, ace_units)
        if not save_in_cloud:
            colors: tuple[PaintInfo, ...] = ()
            mapping: list[SlotAssignment] = []
            if slots:
                if not filename.lower().endswith(".gcode"):
                    raise UploadError("Only .gcode files can be mapped to ACE slots")
                colors = tuple(
                    c.paint for c in gcode_colors(parse_gcode_header(content))
                )
                mapping = build_slot_mapping(colors, slots, ace_units)
            file_id = await self.upload_file(filename, content, temporary=True)
            msgid = await self.start_print_cloud_file(
                printer_id,
                file_id,
                delete_after=True,
                mapping=mapping,
                task_settings=task_settings,
            )
            return PrintStartResult(
                msgid=msgid,
                printer_id=printer_id,
                saved_in_cloud=False,
                file_name=posixpath.basename(filename),
                cloud_file_id=file_id,
                colors=colors,
                mapping=tuple(mapping),
            )
        file_id = await self.upload_file(filename, content, temporary=False)
        newest = await self.list_cloud_files(
            page=1, limit=CLOUD_FILE_LIST_LIMIT, printable=True, machine_type=0
        )
        if not newest or newest[0].id != file_id:
            raise UploadError("Upload mismatch: the newest cloud file is another file")
        gcode_id = newest[0].gcode_id
        if gcode_id is None:
            raise UploadError("The cloud has not parsed the uploaded file yet")
        return await self.print_by_gcode_id(
            printer_id,
            gcode_id,
            slots=slots,
            ace_units=ace_units,
            task_settings=task_settings,
        )


def validate_print_settings(
    settings: Mapping[str, int | float], job: JobDetail
) -> None:
    """Client-side checks of order 6 values (PROTOCOL B §5.4.3)."""
    for key, value in settings.items():
        if key == "print_speed_mode":
            if value not in {option.mode for option in job.speed_modes}:
                raise ValueError("this job does not offer that speed mode")
        elif key in ("target_nozzle_temp", "target_hotbed_temp"):
            limits = (
                job.limits.nozzle if key == "target_nozzle_temp" else job.limits.hotbed
            )
            if limits is None or not limits[0] <= value <= limits[1]:
                raise ValueError(f"{key} is outside this job's limits")
        elif key in ("fan_speed_pct", "aux_fan_speed_pct", "box_fan_level") and not (
            0 <= value <= 100
        ):
            raise ValueError(f"{key} must be between 0 and 100")


# --------------------------------------------------------------------------
# Picking the mode (PROTOCOL A §2.9)
# --------------------------------------------------------------------------


def sign_in_order(token: str, device_id: str | None) -> list[AuthMode]:
    """Modes to try: Android only with a device id; else the guess, then the other."""
    if device_id:
        return [AuthMode.ANDROID]
    if token.startswith("eyJ"):
        return [AuthMode.SLICER, AuthMode.WEB]
    return [AuthMode.WEB, AuthMode.SLICER]


@dataclass(frozen=True, slots=True)
class SignInResult:
    """The winning attempt of :func:`sign_in_any`."""

    auth_mode: AuthMode
    """The mode tried (what the entry saves), even if it fell back to web."""
    account: Account
    tokens: TokenState
    client: AnycubicCloudClient = field(repr=False)


async def sign_in_any(
    session: aiohttp.ClientSession,
    secrets: CloudSecrets,
    token: str,
    *,
    device_id: str | None = None,
    region: Region | str | None = None,
) -> SignInResult:
    """Try the modes in the order of PROTOCOL A §2.9, each with a new client.

    Raises :class:`CredentialsRejectedError` when every mode is refused, with
    reason ``WRONG_TOKEN_TYPE`` when the JWT's ``tokenType`` exists and is not
    ``access-token`` (not checked for China). Any other error (transport,
    maintenance, unreadable answer) ends the attempt at once.
    """
    resolved = Region.resolve(region)
    last_error: CredentialsRejectedError | None = None
    for mode in sign_in_order(token, device_id):
        client = AnycubicCloudClient.from_entry(
            session,
            secrets,
            token=token,
            auth_mode=mode,
            region=resolved,
            device_id=device_id,
        )
        try:
            account = await client.check()
        except CredentialsRejectedError as err:
            last_error = err
            continue
        return SignInResult(
            auth_mode=mode, account=account, tokens=client.token_state, client=client
        )
    claims = decode_claims(token)
    if resolved is not Region.CHINA and claims is not None and claims.is_wrong_type:
        raise CredentialsRejectedError(
            "The token is not a slicer access token",
            reason=RejectReason.WRONG_TOKEN_TYPE,
            server_message=last_error.server_message if last_error else None,
        )
    if last_error is None:  # pragma: no cover - sign_in_order is never empty
        raise CredentialsRejectedError("No sign-in mode to try")
    raise last_error
