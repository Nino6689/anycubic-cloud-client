"""Endpoints, orders, files, upload, printing and the camera (PROTOCOL B and D)."""

from __future__ import annotations

import copy
from typing import Any

import aiohttp
import pytest

from anycubic_cloud_client import (
    AuthMode,
    Axis,
    CloudFileNotFoundError,
    CloudSecrets,
    FileSource,
    JobDetail,
    MoveType,
    NoCameraCredentialsError,
    OrderRefusedError,
    PrinterDetail,
    RenameFailedError,
    ServiceUnavailableError,
    SlotMappingError,
    StorageFullError,
    TaskSettings,
    UnexpectedResponseError,
    UploadError,
    validate_print_settings,
)

from .conftest import USER_INFO, Call, FakeResponse, FakeSession, envelope, make_client
from .payloads import (
    CAMERA_REPLY,
    CLOUD_FILE,
    GCODE_INFO,
    JOB_DETAIL,
    JOB_RECORD,
    PRINTER_ID,
    detail,
)

ORDER = "/work/operation/sendOrder"
OK = envelope({"msgid": "msg-1"}, msg="Operation successful")


def order_bodies(http: FakeSession) -> list[Any]:
    return [c.body for c in http.calls_to(ORDER)]


# -- printers ------------------------------------------------------------------------


async def test_get_printers(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add(
        "GET", "/work/printer/getPrinters", envelope([{"id": 1, "name": "a"}, None])
    )
    printers = await make_client(http, secrets).get_printers()
    assert [p.id for p in printers] == [1]
    http.routes.clear()
    http.add("GET", "/work/printer/getPrinters", envelope([]))
    assert await make_client(http, secrets).get_printers() == []
    http.add("GET", "/work/printer/getPrinters", envelope(None))
    http.routes[("GET", "/work/printer/getPrinters")] = [envelope(None)]
    with pytest.raises(UnexpectedResponseError):
        await make_client(http, secrets).get_printers()


async def test_get_printers_status(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("GET", "/work/printer/printersStatus", envelope(None))
    assert await make_client(http, secrets).get_printers_status() == []


async def test_get_printer(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("GET", "/v2/printer/info", envelope(detail()))
    printer = await make_client(http, secrets).get_printer(PRINTER_ID)
    assert printer.id == PRINTER_ID
    assert http.calls[0].params == {"id": str(PRINTER_ID)}


async def test_get_printer_mismatch_and_missing(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", "/v2/printer/info", envelope(detail(id=99)), envelope(None))
    client = make_client(http, secrets)
    with pytest.raises(UnexpectedResponseError, match="another printer"):
        await client.get_printer(PRINTER_ID)
    with pytest.raises(UnexpectedResponseError):
        await client.get_printer(PRINTER_ID)


async def test_raw_and_catalogue_endpoints(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", "/v2/Printer/status", envelope({"x": 1}))
    http.add(
        "GET", "/v2/printer/all", envelope({"printer_type": [{"machine_type": 1}, 2]})
    )
    http.add("GET", "/v2/project/printHistory", envelope([1]))
    http.add("GET", "/v2/project/monitor", envelope({"m": 1}))
    client = make_client(http, secrets)
    assert await client.get_printer_status(5) == {"x": 1}
    assert [m.machine_type for m in await client.get_printer_models()] == [1]
    assert await client.get_print_history() == [1]
    assert await client.get_job_monitor(9) == {"m": 1}
    assert http.calls_to("/v2/Printer/status")[0].params == {"id": "5"}
    assert http.calls_to("/v2/project/monitor")[0].params == {"id": "9"}


async def test_rename(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add(
        "POST",
        "/work/printer/Info",
        envelope({"name": "New"}),
        envelope({"name": "Old"}),
        envelope({"name": "???"}),
    )
    client = make_client(http, secrets)
    assert await client.rename_printer(5, "New") == "New"
    assert http.calls[0].body == {"id": "5", "name": "New"}
    with pytest.raises(RenameFailedError, match="reverted"):
        await client.rename_printer(5, "New", old_name="Old")
    with pytest.raises(RenameFailedError, match="did not store"):
        await client.rename_printer(5, "New", old_name="Old")
    with pytest.raises(ValueError, match="empty"):
        await client.rename_printer(5, "")


# -- jobs -------------------------------------------------------------------------------


async def test_job_list_once_for_all_printers(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    other = {**JOB_RECORD, "id": 1, "printer_id": 777}
    http.add("GET", "/work/project/getProjects", envelope([other, JOB_RECORD, "junk"]))
    client = make_client(http, secrets)
    latest = await client.get_latest_jobs([PRINTER_ID, 777, 888])
    assert latest[PRINTER_ID].id == JOB_RECORD["id"]  # type: ignore[union-attr]
    assert latest[777].id == 1  # type: ignore[union-attr]
    assert latest[888] is None
    calls = http.calls_to("/work/project/getProjects")
    assert len(calls) == 1
    assert calls[0].params == {"page": "1", "limit": "2000"}


async def test_job_list_null_and_filter(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", "/work/project/getProjects", envelope(None))
    client = make_client(http, secrets)
    assert await client.get_projects(print_status=2) == []
    assert http.calls[0].params["print_status"] == "2"


async def test_job_detail_and_gcode(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("GET", "/v2/project/info", envelope(JOB_DETAIL))
    http.add("GET", "/work/gcode/infoFdm", envelope(GCODE_INFO))
    client = make_client(http, secrets)
    job = await client.get_job_detail(900001)
    assert job.speed_mode_title() == "Standard"
    info = await client.get_gcode_info(700002)
    assert info.file_id == 700001
    assert http.calls[1].params == {"id": "700002"}


async def test_fetch_image(http: FakeSession, secrets: CloudSecrets) -> None:
    url = "https://workbentch.s3.us-east-2.amazonaws.com/a.png"
    http.add("GET", url, FakeResponse(raw=b"PNG"))
    client = make_client(http, secrets)
    assert await client.fetch_image(url) == b"PNG"
    assert "XX-Token" not in http.calls[0].headers
    http.routes[("GET", url)] = [aiohttp.ClientConnectionError()]
    with pytest.raises(ServiceUnavailableError):
        await client.fetch_image(url)


# -- orders ------------------------------------------------------------------------------


async def test_generic_order_results(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add(
        "POST",
        ORDER,
        OK,
        envelope({"no": "msgid"}),
        envelope(None, msg="Print task does not exist"),
        envelope(None, msg="No file found"),
    )
    client = make_client(http, secrets)
    assert await client.send_order(5, 1216, {"type": 0}) == "msg-1"
    assert await client.send_order(5, 1216, {"type": 0}) is None
    with pytest.raises(OrderRefusedError) as info:
        await client.send_order(5, 6, {"settings": {}}, 7)
    assert info.value.server_message == "Print task does not exist"
    with pytest.raises(CloudFileNotFoundError):
        await client.send_order(5, 1, {}, 0)


async def test_generic_order_unknown_ids(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", ORDER, OK)
    client = make_client(http, secrets)
    await client.send_order(5, 44)
    await client.send_order(5, 44, {"a": 1}, 9)
    assert order_bodies(http) == [
        {"order_id": "44", "printer_id": 5, "data": None},
        {"order_id": 44, "printer_id": 5, "project_id": 9, "data": {"a": 1}},
    ]


async def test_every_order_shape(http: FakeSession, secrets: CloudSecrets) -> None:
    """The body of every order method, exactly (PROTOCOL B §5.1, §5.4)."""
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    p = 5
    await c.pause_print(p, 77)
    await c.resume_print(p, 77)
    await c.cancel_print(p, 77)
    await c.set_light(p, True)
    await c.set_light(p, False, light_type=2, job_id=77)
    await c.set_light(p, True, 40, light_type=2)
    await c.set_temperature(p, nozzle=230)
    await c.set_temperature(p, bed=90)
    await c.set_temperature(p, nozzle=200, bed=60)
    await c.set_fan_speed(p, fan_speed_pct=50, aux_fan_speed_pct=20)
    await c.set_fan_speed(p, aux_fan_speed_pct=20, box_fan_level=1)
    await c.set_fan_speed(p, box_fan_level=1)
    await c.move_axis(p, Axis.X, MoveType.POSITIVE, 15)
    await c.home_axis(p, Axis.XY)
    await c.motors_off(p)
    await c.query_axis_position(p)
    await c.query_peripherals(p)
    await c.query_light_status(p)
    await c.ace_get_info(p)
    await c.ace_start_drying(p, 1, target_temp=55, duration=240)
    await c.ace_stop_drying(p, [0, 1])
    await c.ace_feed(p, 2, box_id=1)
    await c.ace_retract(p)
    await c.ace_finish_feed(p, 3)
    await c.ace_set_slot(p, 0, 1, [255, 0, 0], "PLA SE")
    await c.ace_set_auto_refill(p, 1, True)
    await c.request_file_list(p, FileSource.LOCAL)
    await c.request_file_list(p, FileSource.UDISK)
    await c.delete_printer_file(p, FileSource.LOCAL, "part.gcode")
    await c.delete_printer_file(p, FileSource.UDISK, "usb.gcode")
    await c.set_ai_detection(p, True)
    await c.set_ai_detection(
        p,
        False,
        {"type": 1, "count": 30, "sensitivity_level": (2, 2), "notice_type": None},
    )
    assert order_bodies(http) == [
        {
            "order_id": 2,
            "printer_id": p,
            "project_id": 77,
            "data": None,
            "ams_info": None,
            "settings": None,
        },
        {
            "order_id": 3,
            "printer_id": p,
            "project_id": 77,
            "data": None,
            "ams_info": None,
            "settings": None,
        },
        {
            "order_id": 4,
            "printer_id": p,
            "project_id": 77,
            "data": None,
            "ams_info": None,
            "settings": None,
        },
        {
            "order_id": "1233",
            "printer_id": p,
            "data": {"type": 1, "status": 1, "brightness": 100},
        },
        {
            "order_id": 1233,
            "printer_id": p,
            "project_id": 77,
            "data": {"type": 2, "status": 0, "brightness": 0},
        },
        {
            "order_id": "1233",
            "printer_id": p,
            "data": {"type": 2, "status": 1, "brightness": 40},
        },
        {
            "order_id": "1216",
            "printer_id": p,
            "data": {"type": 0, "target_nozzle_temp": 230, "target_hotbed_temp": 0},
        },
        {
            "order_id": "1216",
            "printer_id": p,
            "data": {"type": 1, "target_nozzle_temp": 0, "target_hotbed_temp": 90},
        },
        {
            "order_id": "1216",
            "printer_id": p,
            "data": {"type": 2, "target_nozzle_temp": 200, "target_hotbed_temp": 60},
        },
        {"order_id": "1221", "printer_id": p, "data": {"fan_speed_pct": 50}},
        {"order_id": "1221", "printer_id": p, "data": {"aux_fan_speed_pct": 20}},
        {"order_id": "1221", "printer_id": p, "data": {"box_fan_level": 1}},
        {
            "order_id": "201",
            "printer_id": p,
            "data": {"axis": 1, "move_type": 1, "distance": 15},
        },
        {
            "order_id": "201",
            "printer_id": p,
            "data": {"axis": 4, "move_type": 2, "distance": 0},
        },
        {"order_id": "1213", "printer_id": p, "data": None},
        {"order_id": 1214, "printer_id": p, "project_id": 0},
        {"order_id": "1231", "printer_id": p},
        {"order_id": "1232", "printer_id": p},
        {"order_id": 1206, "printer_id": p, "project_id": 0},
        {
            "order_id": 1207,
            "printer_id": p,
            "project_id": 0,
            "data": {
                "multi_color_box": [
                    {
                        "id": 1,
                        "drying_status": {
                            "status": 1,
                            "target_temp": 55,
                            "duration": 240,
                            "remain_time": None,
                        },
                    }
                ]
            },
        },
        {
            "order_id": 1207,
            "printer_id": p,
            "project_id": 0,
            "data": {
                "multi_color_box": [
                    {
                        "id": 0,
                        "drying_status": {
                            "status": 0,
                            "target_temp": 40,
                            "duration": 0,
                            "remain_time": None,
                        },
                    },
                    {
                        "id": 1,
                        "drying_status": {
                            "status": 0,
                            "target_temp": 40,
                            "duration": 0,
                            "remain_time": None,
                        },
                    },
                ]
            },
        },
        {
            "order_id": 1208,
            "printer_id": p,
            "project_id": 0,
            "data": {
                "multi_color_box": [
                    {"id": 1, "feed_status": {"slot_index": 2, "type": 1}}
                ]
            },
        },
        {
            "order_id": 1208,
            "printer_id": p,
            "project_id": 0,
            "data": {
                "multi_color_box": [
                    {"id": 0, "feed_status": {"slot_index": -1, "type": 2}}
                ]
            },
        },
        {
            "order_id": 1208,
            "printer_id": p,
            "project_id": 0,
            "data": {
                "multi_color_box": [
                    {"id": 0, "feed_status": {"slot_index": 3, "type": 3}}
                ]
            },
        },
        {
            "order_id": 1211,
            "printer_id": p,
            "project_id": 0,
            "data": {
                "multi_color_box": [
                    {
                        "id": 0,
                        "slots": [{"index": 1, "color": [255, 0, 0], "type": "PLA SE"}],
                    }
                ]
            },
        },
        {
            "order_id": 1212,
            "printer_id": p,
            "project_id": 0,
            "data": {"multi_color_box": [{"id": 1, "auto_feed": 1}]},
        },
        {"order_id": 103, "printer_id": p, "project_id": 0, "data": {}},
        {"order_id": 101, "printer_id": p, "project_id": 0, "data": {}},
        {
            "order_id": 104,
            "printer_id": p,
            "project_id": 0,
            "data": {"filename": "part.gcode", "filetype": -1, "path": "/"},
        },
        {
            "order_id": 102,
            "printer_id": p,
            "project_id": 0,
            "data": {"filename": "usb.gcode", "filetype": -1, "path": "/"},
        },
        {
            "order_id": "1243",
            "printer_id": p,
            "data": {
                "ai_settings": {
                    "status": 3,
                    "type": 2,
                    "count": 60,
                    "sensitivity_level": [1, 1],
                    "notice_type": [0, 1],
                }
            },
        },
        {
            "order_id": "1243",
            "printer_id": p,
            "data": {
                "ai_settings": {
                    "status": 0,
                    "type": 1,
                    "count": 30,
                    "sensitivity_level": [2, 2],
                    "notice_type": [0, 1],
                }
            },
        },
    ]


async def test_order_argument_checks(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    assert await c.set_temperature(5) is None
    with pytest.raises(ValueError, match="fan"):
        await c.set_fan_speed(5)
    with pytest.raises(ValueError, match="brightness"):
        await c.set_light(5, True, 101)
    with pytest.raises(ValueError, match="slot"):
        await c.ace_feed(5, -1)
    with pytest.raises(ValueError, match="slot index"):
        await c.ace_set_slot(5, 0, 4, [1, 2, 3], "PLA")
    with pytest.raises(ValueError, match="colour"):
        await c.ace_set_slot(5, 0, 0, [1, 2], "PLA")
    with pytest.raises(ValueError, match="ACE"):
        await c.ace_stop_drying(5, [])
    with pytest.raises(ValueError, match="setting"):
        await c.set_print_settings(5, 7, {})
    assert http.calls == []


async def test_print_settings(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    job = JobDetail.from_data(JOB_DETAIL)
    await c.set_speed_mode(5, 77, 3, job)
    await c.set_print_settings(
        5, 77, {"target_nozzle_temp": 220, "fan_speed_pct": 50}, job=job
    )
    await c.set_print_settings(5, 77, {"on_time": 2.5})
    assert order_bodies(http) == [
        {
            "order_id": 6,
            "printer_id": 5,
            "project_id": 77,
            "data": {"settings": {"print_speed_mode": 3}},
        },
        {
            "order_id": 6,
            "printer_id": 5,
            "project_id": 77,
            "data": {"settings": {"target_nozzle_temp": 220, "fan_speed_pct": 50}},
        },
        {
            "order_id": 6,
            "printer_id": 5,
            "project_id": 77,
            "data": {"settings": {"on_time": 2.5}},
        },
    ]


@pytest.mark.parametrize(
    "settings",
    [
        {"print_speed_mode": 9},
        {"target_nozzle_temp": 400},
        {"target_hotbed_temp": 10},
        {"fan_speed_pct": 101},
        {"box_fan_level": -1},
    ],
)
def test_print_settings_validation(settings: dict[str, int]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        validate_print_settings(settings, JobDetail.from_data(JOB_DETAIL))


def test_print_settings_without_limits_are_refused() -> None:
    with pytest.raises(ValueError, match="limits"):
        validate_print_settings({"target_nozzle_temp": 200}, JobDetail.from_data({}))
    with pytest.raises(ValueError, match="speed mode"):
        validate_print_settings({"print_speed_mode": 1}, JobDetail.from_data({}))


# -- firmware ---------------------------------------------------------------------------


def _printer_with_updates() -> PrinterDetail:
    data = detail()
    data["version"] = {**data["version"], "need_update": 1, "target_version": "2.8.0.0"}
    second = {
        **data["multi_color_box_version"][0],
        "box_id": 1,
        "need_update": 1,
        "target_version": "1.4.0",
    }
    data["multi_color_box_version"] = [data["multi_color_box_version"][0], second]
    return PrinterDetail.from_data(data)


async def test_printer_firmware_update(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add(
        "GET",
        "/work/printer/update_version",
        envelope({"update_status": 1}),
        envelope({"update_status": 0}),
    )
    c = make_client(http, secrets)
    printer = _printer_with_updates()
    assert await c.update_printer_firmware(printer) == "2.8.0.0"
    # 2.x sends the installed version as target_version (Q14)
    assert http.calls[0].params == {"id": str(PRINTER_ID), "target_version": "2.7.2.7"}
    assert await c.update_printer_firmware(printer) is None
    assert await c.update_printer_firmware(PrinterDetail.from_data(detail())) is None
    assert len(http.calls) == 2


async def test_ace_firmware_update(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add(
        "POST",
        "/v2/printer/update_multi_color_box_version",
        envelope({"target_version": "1.4.0"}),
        envelope({"target_version": "0.0.1"}),
    )
    c = make_client(http, secrets)
    printer = _printer_with_updates()
    assert await c.update_ace_firmware(printer, 0) is None  # no update offered
    assert await c.update_ace_firmware(printer, 5) is None
    assert await c.update_ace_firmware(printer, 1) == "1.4.0"
    assert http.calls[0].body == {"id": PRINTER_ID, "box_id": 1}
    assert await c.update_all_ace_firmware(printer) == [None, None]
    no_ace = PrinterDetail.from_data(detail(multi_color_box=None))
    assert await c.update_ace_firmware(no_ace, 1) is None
    assert len(http.calls) == 2


# -- cloud files ---------------------------------------------------------------------


async def test_cloud_files(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("POST", "/work/index/files", envelope([CLOUD_FILE]), envelope(None))
    http.add("POST", "/work/index/delFiles", envelope(""), envelope(None, msg="nope"))
    c = make_client(http, secrets)
    files = await c.list_cloud_files()
    assert files[0].id == 700001
    assert http.calls[0].body == {"page": 1, "limit": 10}
    assert await c.list_cloud_files(page=2, printable=True, machine_type=0) == []
    assert http.calls[1].body == {
        "page": 2,
        "limit": 10,
        "printable": 1,
        "machine_type": 0,
    }
    await c.delete_cloud_files([700001])
    assert http.calls[2].body == {"idArr": [700001]}
    with pytest.raises(OrderRefusedError, match="delete"):
        await c.delete_cloud_files([700001])
    with pytest.raises(ValueError, match="file id"):
        await c.delete_cloud_files([])


async def test_storage_quota(http: FakeSession, secrets: CloudSecrets) -> None:
    quota = {
        "used_bytes": 10,
        "total_bytes": 100,
        "used": "10 B",
        "total": "100 B",
        "user_file_exists": True,
    }
    http.add(
        "POST", "/work/index/getUserStore", envelope(quota), envelope({"used": "x"})
    )
    c = make_client(http, secrets)
    result = await c.get_storage_quota()
    assert result.available_bytes == 90
    assert result.user_file_exists is True
    assert http.calls[0].raw_body == "{}"
    with pytest.raises(UnexpectedResponseError):
        await c.get_storage_quota()


# -- upload (PROTOCOL B §4.4) -----------------------------------------------------------

LOCK = "/v2/cloud_storage/lockStorageSpace"
PUT_URL = "https://storage.example.invalid/presigned?sig=x"
REGISTER = "/v2/profile/newUploadFile"
UNLOCK = "/v2/cloud_storage/unlockStorageSpace"
QUOTA = "/work/index/getUserStore"


def _quota(used: int) -> dict[str, Any]:
    return envelope(
        {
            "used_bytes": used,
            "total_bytes": 1000,
            "used": "",
            "total": "",
            "user_file_exists": True,
        }
    )


def _upload_routes(http: FakeSession, *, put: Any = None, register: Any = None) -> None:
    http.add("POST", LOCK, envelope({"id": 55, "preSignUrl": PUT_URL}))
    http.add("PUT", PUT_URL, put if put is not None else FakeResponse(text=""))
    http.add(
        "POST", REGISTER, register if register is not None else envelope({"id": 700001})
    )
    http.add("POST", UNLOCK, envelope(None))


async def test_permanent_upload(http: FakeSession, secrets: CloudSecrets) -> None:
    _upload_routes(http)
    http.add("POST", QUOTA, _quota(100), _quota(110))
    c = make_client(http, secrets)
    assert (
        await c.upload_file("dir/benchy.gcode", b"0123456789", temporary=False)
        == 700001
    )
    assert [x.path for x in http.calls] == [
        QUOTA,
        LOCK,
        PUT_URL,
        REGISTER,
        UNLOCK,
        QUOTA,
    ]
    assert http.calls[1].body == {"size": 10, "name": "benchy.gcode", "is_temp_file": 0}
    put = http.calls[2]
    assert put.kwargs["data"] == b"0123456789"
    assert put.headers == {}  # no signed headers, no token, no Content-Type
    assert http.calls[3].body == {"user_lock_space_id": 55}
    assert http.calls[4].body == {"id": 55, "is_delete_cos": 0}


async def test_temporary_upload_skips_quota(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    _upload_routes(http)
    c = make_client(http, secrets, mode=AuthMode.WEB)
    assert await c.upload_file("a.gcode", b"x", temporary=True) == 700001
    assert http.calls_to(QUOTA) == []
    assert http.calls[0].body["is_temp_file"] == 1
    put = http.calls_to(PUT_URL)[0]
    assert put.headers["Origin"] == "https://uc.makeronline.com"
    assert put.headers["User-Agent"].startswith("Mozilla")


@pytest.mark.parametrize(
    ("put", "register", "error"),
    [
        (FakeResponse(text="<Error>AccessDenied</Error>"), None, "refused the upload"),
        (aiohttp.ClientConnectionError(), None, "failed"),
        (None, envelope(None), "did not register"),
        (None, envelope(None, msg="request error"), "request error"),
    ],
)
async def test_failed_upload_still_unlocks(
    http: FakeSession, secrets: CloudSecrets, put: Any, register: Any, error: str
) -> None:
    _upload_routes(http, put=put, register=register)
    c = make_client(http, secrets)
    with pytest.raises((UploadError, ServiceUnavailableError), match=error):
        await c.upload_file("a.gcode", b"abc", temporary=True)
    unlocks = http.calls_to(UNLOCK)
    assert len(unlocks) == 1
    assert unlocks[0].body == {"id": 55, "is_delete_cos": 1}


async def test_unlock_failure_does_not_mask_the_result(
    http: FakeSession, secrets: CloudSecrets, caplog: pytest.LogCaptureFixture
) -> None:
    _upload_routes(http)
    http.routes[("POST", UNLOCK)] = [aiohttp.ClientConnectionError()]
    c = make_client(http, secrets)
    assert await c.upload_file("a.gcode", b"abc", temporary=True) == 700001
    assert "upload slot" in caplog.text


async def test_upload_errors(http: FakeSession, secrets: CloudSecrets) -> None:
    c = make_client(http, secrets)
    with pytest.raises(UploadError, match="empty"):
        await c.upload_file("a.gcode", b"", temporary=True)
    http.add("POST", QUOTA, _quota(999))
    with pytest.raises(StorageFullError):
        await c.upload_file("a.gcode", b"ab", temporary=False)
    http.routes.clear()
    http.add("POST", LOCK, envelope({"id": None}))
    with pytest.raises(UploadError, match="upload slot"):
        await c.upload_file("a.gcode", b"ab", temporary=True)
    http.routes.clear()
    _upload_routes(http)
    http.add("POST", QUOTA, _quota(100), _quota(100))
    with pytest.raises(UploadError, match="not found in the cloud"):
        await c.upload_file("a.gcode", b"ab", temporary=False)


# -- printing (PROTOCOL D §2) ---------------------------------------------------------


async def test_start_print_cloud_file(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    assert await c.start_print_cloud_file(5, 700001) == "msg-1"
    assert order_bodies(http)[0] == {
        "order_id": 1,
        "printer_id": 5,
        "project_id": 0,
        "data": {
            "filetype": 0,
            "file_key": "",
            "file_name": "",
            "task_settings": {"ai_detect": 0, "camera_timelapse": 0},
            "file_id": 700001,
            "hollow_param": None,
            "is_delete_file": 0,
            "matrix": "",
            "project_type": 1,
            "punching_param": None,
            "slice_param": None,
            "slice_size": None,
            "template_id": 0,
        },
        "ams_info": None,
        "settings": None,
    }


async def test_start_print_printer_file(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    await c.start_print_printer_file(5, "part.gcode")
    await c.start_print_printer_file(
        5,
        "usb.gcode",
        FileSource.UDISK,
        folder="/sub/",
        task_settings=TaskSettings(1, 1),
    )
    first, second = order_bodies(http)
    assert first == {
        "order_id": 1,
        "printer_id": 5,
        "project_id": 0,
        "data": {
            "filetype": 1,
            "file_key": "",
            "file_name": "",
            "task_settings": {"ai_detect": 0, "camera_timelapse": 0},
            "filename": "part.gcode",
            "filepath": "/",
        },
        "ams_info": None,
        "settings": None,
    }
    assert second["data"]["filetype"] == 2
    assert second["data"]["filepath"] == "/sub"
    assert second["data"]["task_settings"] == {"ai_detect": 1, "camera_timelapse": 1}


async def test_start_retries_then_fails(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", ORDER, envelope(None, msg="No file found"))
    c = make_client(http, secrets)
    with pytest.raises(CloudFileNotFoundError):
        await c.start_print_cloud_file(5, 1)
    assert len(http.calls_to(ORDER)) == 3
    assert no_sleep == [3.0, 3.0]


async def test_start_retry_succeeds(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", ORDER, envelope(None, msg="No file found"), OK)
    c = make_client(http, secrets)
    assert await c.start_print_cloud_file(5, 1) == "msg-1"
    assert no_sleep == [3.0]


def _ace_units() -> Any:
    data = detail()
    second = copy.deepcopy(data["multi_color_box"])
    second["id"] = 1
    second["slots"] = [
        {
            "index": 0,
            "sku": "",
            "type": "PLA",
            "color": [1, 2, 3],
            "status": 4,
            "edit_status": 0,
        }
    ]
    first = data["multi_color_box"]
    first["id"] = 0
    return PrinterDetail.from_data(
        {**data, "multi_color_box": [first, second]}
    ).ace_units


async def test_print_by_gcode_id_with_mapping(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("GET", "/work/gcode/infoFdm", envelope(GCODE_INFO))
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    # colours in file order: paint 1 (PLA) then paint 0 (PETG); slots 4 and 3
    result = await c.print_by_gcode_id(5, 700002, slots=[4, 3], ace_units=_ace_units())
    assert result.cloud_file_id == 700001
    assert result.saved_in_cloud
    assert result.gcode_id == 700002
    body = order_bodies(http)[0]
    assert body["data"]["file_id"] == 700001
    assert body["ams_info"] == {
        "ams_box_mapping": [
            {
                "ams_color": [239, 240, 241],
                "ams_index": 3,
                "filament_used": 12.3,
                "material_type": "PETG",
                "paint_color": [239, 240, 241],
                "paint_index": 0,
            },
            {
                "ams_color": [1, 2, 3],
                "ams_index": 4,
                "filament_used": 4.1,
                "material_type": "PLA",
                "paint_color": [1, 2, 3],
                "paint_index": 1,
            },
        ],
        "use_ams": True,
    }


async def test_print_by_gcode_id_errors(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add(
        "GET",
        "/work/gcode/infoFdm",
        envelope({"file_id": None}),
        envelope({"file_id": 1}),
    )
    c = make_client(http, secrets)
    with pytest.raises(UnexpectedResponseError, match="file id"):
        await c.print_by_gcode_id(5, 1)
    with pytest.raises(UnexpectedResponseError, match="colour list"):
        await c.print_by_gcode_id(5, 1, slots=[0], ace_units=_ace_units())
    with pytest.raises(SlotMappingError):
        await c.print_by_gcode_id(5, 1, ace_units=_ace_units())


GCODE = b"""; HEADER_BLOCK_START
; generated by a slicer
; filament used [mm] = 100.5, 30
; filament used [g] = 3.2, 1.1
; paint_info = [{"paint_index": 0, "material_type": "PLA"}, {"paint_index": 1, "material_type": "PETG"}]
G28
"""


async def test_upload_and_print_without_cloud_save(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    _upload_routes(http)
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    result = await c.upload_and_print(
        5,
        "part.gcode",
        GCODE,
        save_in_cloud=False,
        slots=[0, 3],
        ace_units=_ace_units(),
    )
    assert not result.saved_in_cloud
    assert result.gcode_id is None
    assert result.cloud_file_id == 700001
    assert [c.paint_index for c in result.colors] == [0, 1]
    body = order_bodies(http)[0]
    assert body["data"]["is_delete_file"] == 1
    assert [m["ams_index"] for m in body["ams_info"]["ams_box_mapping"]] == [0, 3]
    assert http.calls_to(QUOTA) == []


async def test_upload_and_print_without_slots(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    _upload_routes(http)
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    result = await c.upload_and_print(5, "model.zip", b"raw", save_in_cloud=False)
    assert result.mapping == ()
    assert order_bodies(http)[0]["ams_info"] is None


async def test_upload_and_print_rejects_before_uploading(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    c = make_client(http, secrets)
    with pytest.raises(SlotMappingError):
        await c.upload_and_print(5, "a.gcode", GCODE, save_in_cloud=False, slots=[0])
    with pytest.raises(SlotMappingError):
        await c.upload_and_print(
            5, "a.gcode", GCODE, save_in_cloud=True, ace_units=_ace_units()
        )
    with pytest.raises(UploadError, match="gcode"):
        await c.upload_and_print(
            5, "a.zip", GCODE, save_in_cloud=False, slots=[0, 1], ace_units=_ace_units()
        )
    assert http.calls == []


async def test_upload_and_print_saved_in_cloud(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    _upload_routes(http)
    http.add("POST", QUOTA, _quota(100), _quota(200))
    http.add("POST", "/work/index/files", envelope([CLOUD_FILE]))
    http.add("GET", "/work/gcode/infoFdm", envelope(GCODE_INFO))
    http.add("POST", ORDER, OK)
    c = make_client(http, secrets)
    result = await c.upload_and_print(5, "benchy.gcode", b"x" * 50, save_in_cloud=True)
    assert result.saved_in_cloud
    assert result.gcode_id == 700002
    assert http.calls_to("/work/index/files")[0].body == {
        "page": 1,
        "limit": 10,
        "printable": 1,
        "machine_type": 0,
    }
    assert order_bodies(http)[0]["data"]["is_delete_file"] == 0


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ([], "mismatch"),
        ([{**CLOUD_FILE, "id": 1}], "mismatch"),
        ([{**CLOUD_FILE, "gcode_id": None}], "not parsed"),
    ],
)
async def test_upload_and_print_saved_in_cloud_races(
    http: FakeSession, secrets: CloudSecrets, files: list[Any], error: str
) -> None:
    _upload_routes(http)
    http.add("POST", QUOTA, _quota(100), _quota(200))
    http.add("POST", "/work/index/files", envelope(files))
    c = make_client(http, secrets)
    with pytest.raises(UploadError, match=error):
        await c.upload_and_print(5, "benchy.gcode", b"x" * 50, save_in_cloud=True)


# -- camera (PROTOCOL D §1) -----------------------------------------------------------

EXCHANGE = "/v3/public/loginWithAccessToken"
USER = "/user/profile/userInfo"
NO_BLOCK = envelope({"msgid": "m", "token": "t"}, msg="Operation successful")


async def test_open_camera(http: FakeSession, secrets: CloudSecrets) -> None:
    http.add("POST", ORDER, CAMERA_REPLY)
    c = make_client(http, secrets)
    credentials = await c.open_camera(5)
    assert credentials.client_uid == 6001
    assert order_bodies(http) == [
        {"order_id": "1001", "printer_id": 5, "shengwang_rtc_support": True}
    ]


async def test_camera_retry_after_fresh_login(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", ORDER, NO_BLOCK, CAMERA_REPLY)
    http.add("POST", EXCHANGE, envelope({"token": "FRESH"}))
    http.add("GET", USER, envelope(USER_INFO))
    c = make_client(http, secrets, user_token="OLD", access_token="access")
    credentials = await c.open_camera(5)
    assert credentials.client_uid == 6001
    assert [x.path for x in http.calls] == [ORDER, EXCHANGE, USER, ORDER]
    assert http.calls[3].headers["XX-Token"] == "FRESH"


async def test_camera_gives_up_after_one_retry(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", ORDER, NO_BLOCK, envelope(None, msg="x"))
    http.add("POST", EXCHANGE, envelope({"token": "FRESH"}))
    http.add("GET", USER, envelope(USER_INFO))
    c = make_client(http, secrets, user_token="OLD", access_token="access")
    with pytest.raises(NoCameraCredentialsError, match="another Anycubic session"):
        await c.open_camera(5)
    assert len(http.calls_to(ORDER)) == 2


async def test_camera_no_retry_without_access_token(
    http: FakeSession, secrets: CloudSecrets
) -> None:
    http.add("POST", ORDER, NO_BLOCK)
    c = make_client(http, secrets, mode=AuthMode.WEB)
    with pytest.raises(NoCameraCredentialsError):
        await c.open_camera(5)
    assert len(http.calls) == 1


async def test_camera_failed_login_gives_up(
    http: FakeSession, secrets: CloudSecrets, no_sleep: list[float]
) -> None:
    http.add("POST", ORDER, NO_BLOCK)
    http.add("POST", EXCHANGE, envelope(None, msg="User does not exist"))

    def user_info(call: Call) -> Any:
        return envelope(None)

    http.add("GET", USER, user_info)
    c = make_client(http, secrets, user_token="OLD", access_token="access")
    with pytest.raises(NoCameraCredentialsError) as info:
        await c.open_camera(5)
    assert info.value.__cause__ is not None
    assert len(http.calls_to(ORDER)) == 1
