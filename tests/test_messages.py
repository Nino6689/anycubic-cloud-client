"""Every cloud MQTT message kind of PROTOCOL C §4, including the null rules."""

from __future__ import annotations

from typing import Any

import pytest
from anycubic_lan.reports import (
    AiSettingsReport,
    AxisReport,
    ExternalFilamentBoxReport,
    FanReport,
    InfoReport,
    LightReport,
    MultiColorBoxReport,
    PeripheralsReport,
    TemperatureReport,
)

from anycubic_cloud_client import (
    AceLoadedSlotUpdate,
    AxisMoveUpdate,
    BindingUpdate,
    CloudMessage,
    ExternalHolderUpdate,
    FileDeletedUpdate,
    FileListUpdate,
    FileSource,
    FirmwareReport,
    FirmwareStep,
    JobStatus,
    OnlineUpdate,
    PrinterEvent,
    PrintUpdate,
    WorkStatus,
    WorkStatusUpdate,
    parse_cloud_message,
)
from anycubic_cloud_client.messages import decode_payload

from .payloads import ACE_GET_INFO_DATA, MACHINE_TYPE, PRINTER_KEY, mqtt

PUBLIC = f"anycubic/anycubicCloud/v1/printer/public/{MACHINE_TYPE}/{PRINTER_KEY}"
RESPONSE = (
    f"anycubic/anycubicCloud/v1/printer/app/{MACHINE_TYPE}/{PRINTER_KEY}/response"
)
USER = "anycubic/anycubicCloud/v1/server/app/424242/abc/slice/report"


def parse(message: dict[str, Any], topic: str | None = None) -> CloudMessage:
    kind = message.get("type") or "x"
    result = parse_cloud_message(topic or f"{PUBLIC}/{kind}/report", message)
    assert result is not None
    return result


# -- routing and envelope ------------------------------------------------------------------


def test_dropped_messages() -> None:
    assert parse_cloud_message(USER, mqtt("print", "start", "printing")) is None
    assert parse_cloud_message(RESPONSE, {"msgid": "abc"}) is None
    assert parse_cloud_message(f"{PUBLIC}/x", {"action": "a", "data": {}}) is None
    assert parse_cloud_message("short/topic", mqtt("fan", "auto", "done")) is None
    # a response with more than one key is processed
    reply = parse_cloud_message(RESPONSE, mqtt("light", "control", "done", {"type": 2}))
    assert reply is not None
    assert reply.printer_key == PRINTER_KEY


def test_decode_payload() -> None:
    assert decode_payload(b'{"a": 1}') == {"a": 1}
    assert decode_payload(b"[1]") is None
    assert decode_payload(b"\xff") is None
    assert decode_payload("{") is None
    assert decode_payload({"b": 2}) == {"b": 2}


def test_envelope_and_fault() -> None:
    message = parse(mqtt("fan", "auto", "done", {"fan_speed_pct": 10}))
    assert message.code == 200
    assert message.fault is None
    assert message.msgid == "fake-msgid"
    assert message.msg == "done"
    assert message.data == {"fan_speed_pct": 10}
    assert PRINTER_KEY not in repr(message)
    zero = parse(mqtt("fan", "auto", "done", {}, code=0))
    assert zero.fault is None
    boolean = parse({**mqtt("fan", "auto", "done", {}), "code": True})
    assert boolean.fault is None
    assert boolean.code is None


def test_fault_kept_when_not_understood() -> None:
    message = parse(
        mqtt("video", "startCapture", "done", None, code=11858, msg="slot empty")
    )
    assert not message.understood
    assert message.fault is not None
    assert (message.fault.code, message.fault.message) == (11858, "slot empty")


# -- lastWill, status, user --------------------------------------------------------------


def test_last_will() -> None:
    online = parse(mqtt("lastWill", "onlineReport", "online"))
    assert online.update == OnlineUpdate(True)
    assert parse(mqtt("lastWill", "onlineReport", "offline")).update == OnlineUpdate(
        False
    )
    assert not parse(mqtt("lastWill", "onlineReport", "sleeping")).understood


def test_status() -> None:
    busy = parse(mqtt("status", "workReport", "busy"))
    assert busy.update == WorkStatusUpdate(WorkStatus.BUSY)
    free = parse(mqtt("status", "workReport", "free", {"ignored": 1}))
    assert free.update == WorkStatusUpdate(WorkStatus.FREE)
    assert not parse(mqtt("status", "workReport", "other")).understood


def test_user_binding() -> None:
    assert parse(mqtt("user", "bindQuery", "done")).update == BindingUpdate(True)
    assert parse(mqtt("user", "unbind", "done")).update == BindingUpdate(False)
    assert not parse(mqtt("user", "unbind", "failed")).understood


# -- print (PROTOCOL C §4.4) -----------------------------------------------------------------

PRINTING_DATA = {
    "taskid": "900001",
    "localtask": "uuid",
    "filename": "benchy.gcode",
    "progress": 42,
    "curr_layer": 57,
    "total_layers": 136,
    "print_time": 31,
    "remain_time": 44,
    "supplies_usage": 5210,
}


def _print(action: str, state: str, data: Any = None, **kw: Any) -> PrintUpdate:
    update = parse(mqtt("print", action, state, data, **kw)).update
    assert isinstance(update, PrintUpdate)
    return update


def test_print_printing() -> None:
    update = _print("start", "printing", PRINTING_DATA)
    assert update.work_status is WorkStatus.BUSY
    assert update.job_status is JobStatus.PRINTING
    assert update.download_progress == 0
    assert update.task_id == 900001
    assert update.task_id_present
    assert update.job is not None
    assert update.job.progress == 42
    assert update.job.current_layer == 57
    assert update.job.supplies_usage == 5210
    assert update.job.filename == "benchy.gcode"
    assert update.applies_to(900001)
    assert not update.applies_to(1)
    assert not update.applies_to(None)


@pytest.mark.parametrize(
    ("action", "state", "work", "status", "pause"),
    [
        ("start", "downloading", WorkStatus.BUSY, JobStatus.DOWNLOADING, None),
        ("start", "checking", WorkStatus.BUSY, JobStatus.CHECKING, None),
        ("start", "preheating", WorkStatus.BUSY, JobStatus.PREHEATING, None),
        ("start", "finished", WorkStatus.FREE, JobStatus.COMPLETE, None),
        ("pause", "pausing", WorkStatus.BUSY, JobStatus.PRINTING, 1),
        ("pause", "paused", WorkStatus.BUSY, JobStatus.PRINTING, 1),
        ("resume", "resuming", WorkStatus.BUSY, JobStatus.PRINTING, 1),
        ("resume", "resumed", WorkStatus.BUSY, JobStatus.PRINTING, 0),
        ("start", "stopping", WorkStatus.FREE, JobStatus.CANCELLED, None),
        ("stop", "stoped", WorkStatus.FREE, JobStatus.CANCELLED, None),
        ("stop", "stopping", WorkStatus.FREE, JobStatus.CANCELLED, None),
        ("start", "stoped", WorkStatus.FREE, JobStatus.CANCELLED, None),
    ],
)
def test_print_status_rows(
    action: str, state: str, work: WorkStatus, status: JobStatus, pause: int | None
) -> None:
    update = _print(action, state, {"taskid": 5, "progress": 10})
    assert (update.work_status, update.job_status, update.pause) == (
        work,
        status,
        pause,
    )


def test_print_downloading() -> None:
    update = _print("start", "downloading", {"taskid": 77, "progress": 33})
    assert update.download_progress == 33
    assert update.job is None  # no job fields while downloading
    checking = _print("start", "checking", {"taskid": 77})
    assert checking.download_progress == 0
    assert checking.job is None


def test_print_failed() -> None:
    update = _print(
        "stop", "failed", {"taskid": "5"}, code=10111, msg="Task abnormally ended"
    )
    assert update.job_status is JobStatus.CANCELLED
    assert update.work_status is WorkStatus.FREE
    assert update.failure_reason == "Task abnormally ended"
    assert _print("start", "failed").failure_reason == "done"


def test_print_updated() -> None:
    data = {
        "taskid": "900001",
        "curr_hotbed_temp": 60,
        "curr_nozzle_temp": 219,
        "settings": {
            "fan_speed_pct": 100,
            "print_speed_pct": 100,
            "print_speed_mode": 2,
            "target_hotbed_temp": 60,
            "target_nozzle_temp": 220,
        },
    }
    update = _print("update", "updated", data)
    assert update.work_status is None
    assert update.job_status is None
    assert (update.nozzle_temp, update.hotbed_temp) == (219, 60)
    assert update.fan_speed_pct == 100
    assert update.print_speed_pct == 100
    assert update.print_speed_mode == 2
    assert (update.target_nozzle_temp, update.target_hotbed_temp) == (220, 60)
    partial = _print("start", "updated", {"curr_nozzle_temp": 200, "settings": None})
    assert partial.nozzle_temp is None
    assert partial.hotbed_temp is None
    one_target = _print("update", "updated", {"settings": {"target_nozzle_temp": 1}})
    assert one_target.target_nozzle_temp is None


def test_print_slice_param() -> None:
    update = _print("getSliceParam", "done", {"slice_param": '{"image_id": "x.png"}'})
    assert update.slice_param is not None
    assert update.slice_param.image_id == "x.png"


@pytest.mark.parametrize(
    ("action", "state"), [("stop", "stopped"), ("pause", "failed"), ("x", "y")]
)
def test_print_not_understood(action: str, state: str) -> None:
    assert not parse(mqtt("print", action, state, {"taskid": 1})).understood


def test_print_without_action_is_not_understood() -> None:
    message = parse({"type": "print", "state": "printing"})
    assert not message.understood


def test_print_task_rule() -> None:
    no_task = _print("start", "printing", {"progress": 1})
    assert no_task.task_id is None
    assert not no_task.task_id_present
    assert no_task.applies_to(5)
    text_task = _print("start", "printing", {"taskid": "abc"})
    assert text_task.task_id_present
    assert text_task.applies_to(5)
    negative = _print("start", "printing", {"taskid": -1})
    assert negative.applies_to(5)


def test_print_null_data() -> None:
    update = _print("start", "printing", None)
    assert update.job_status is JobStatus.PRINTING
    assert update.job is None
    downloading = _print("start", "downloading", None)
    assert downloading.job_status is JobStatus.DOWNLOADING
    assert downloading.download_progress is None


# -- kinds shared with LAN (parsed by anycubic_lan) ------------------------------------------


def test_temperature() -> None:
    message = parse(
        mqtt(
            "tempature",
            "auto",
            "done",
            {
                "curr_hotbed_temp": 60,
                "curr_nozzle_temp": 218,
                "target_hotbed_temp": 60,
                "target_nozzle_temp": 220,
            },
        )
    )
    assert isinstance(message.report, TemperatureReport)
    assert message.report.temperatures.nozzle == 218
    assert message.report.temperatures.bed_target == 60


def test_fan_light_peripherals() -> None:
    fan = parse(mqtt("fan", "auto", "done", {"fan_speed_pct": 50, "box_fan_level": 2}))
    assert isinstance(fan.report, FanReport)
    assert fan.report.fans.box_fan_level == 2
    lights = parse(
        mqtt(
            "light",
            "query",
            "done",
            {"lights": [{"type": 2, "status": 1, "brightness": 100}]},
        )
    )
    assert isinstance(lights.report, LightReport)
    assert lights.report.full_list
    single = parse(
        mqtt("light", "control", "done", {"type": 2, "status": 0, "brightness": 0})
    )
    assert isinstance(single.report, LightReport)
    assert not single.report.full_list
    assert not parse(mqtt("light", "control", "failed", {"type": 2})).understood
    peripherals = parse(
        mqtt(
            "peripherie", "query", "done", {"camera": 1, "multiColorBox": 1, "udisk": 0}
        )
    )
    assert isinstance(peripherals.report, PeripheralsReport)
    assert peripherals.report.peripherals.camera is True
    assert peripherals.report.peripherals.usb_disk is False
    assert not parse(mqtt("peripherie", "set", "done", {})).understood


def test_axis() -> None:
    query = parse(
        mqtt(
            "axis",
            "query",
            "done",
            {"coordinates": {"x": 47, "y": 276, "z": 3.8152532726237904}},
        )
    )
    assert isinstance(query.report, AxisReport)
    assert query.report.position is not None
    assert query.report.position.z == 3.8152532726237904
    move = parse(mqtt("axis", "move", "doing"))
    assert move.update == AxisMoveUpdate("doing")
    assert isinstance(move.update, AxisMoveUpdate)
    assert move.update.moving
    refused = AxisMoveUpdate("failed")
    assert refused.refused
    assert not refused.moving
    assert not AxisMoveUpdate("done").moving
    assert not parse(mqtt("axis", "turnOff", "done")).understood


def test_ace_get_info_list_and_single_object() -> None:
    message = parse(mqtt("multiColorBox", "getInfo", "success", ACE_GET_INFO_DATA))
    assert isinstance(message.report, MultiColorBoxReport)
    assert message.report.is_full_list
    assert message.report.boxes is not None
    assert message.report.boxes[0].model_id == 40001
    single = {
        "multi_color_box": ACE_GET_INFO_DATA["multi_color_box"][0],
        "head_tools_model": 0,
    }
    one = parse(mqtt("multiColorBox", "getInfo", "success", single))
    assert isinstance(one.report, MultiColorBoxReport)
    assert one.report.boxes is not None
    assert len(one.report.boxes) == 1


def test_ace_auto_update_info_flat() -> None:
    message = parse(
        mqtt("multiColorBox", "autoUpdateInfo", "done", {"id": 1, "loaded_slot": 2})
    )
    assert message.update == AceLoadedSlotUpdate(1, 2)
    assert isinstance(message.report, MultiColorBoxReport)
    assert message.report.boxes is not None
    assert message.report.boxes[0].loaded_slot_raw == 2
    listed = parse(
        mqtt(
            "multiColorBox",
            "autoUpdateInfo",
            "done",
            {"multi_color_box": [{"id": 0, "loaded_slot": 1}]},
        )
    )
    assert listed.update is None


@pytest.mark.parametrize(
    "action",
    [
        "setInfo",
        "refresh",
        "autoUpdateDryStatus",
        "setDry",
        "feedFilament",
        "setAutoFeed",
    ],
)
def test_ace_partial_actions(action: str) -> None:
    data = {
        "multi_color_box": [
            {
                "id": 0,
                "temp": 44,
                "drying_status": {
                    "status": 1,
                    "target_temp": 45,
                    "duration": 240,
                    "remain_time": 212,
                },
            }
        ]
    }
    message = parse(mqtt("multiColorBox", action, "success", data))
    assert message.understood
    assert isinstance(message.report, MultiColorBoxReport)
    assert not message.report.is_full_list


def test_ace_unknown_action() -> None:
    assert not parse(mqtt("multiColorBox", "mystery", "success", {})).understood


def test_extfilbox() -> None:
    message = parse(
        mqtt(
            "extfilbox",
            "reportInfo",
            "success",
            {
                "type": "PLA",
                "color": [1, 2, 3],
                "loaded": 1,
                "status_type": 2,
                "current_status": 11,
            },
        )
    )
    assert message.update == ExternalHolderUpdate("PLA", (1, 2, 3), True, 2, 11)
    assert isinstance(message.report, ExternalFilamentBoxReport)
    empty = parse(mqtt("extfilbox", "reportInfo", "success", None))
    assert empty.update == ExternalHolderUpdate()


def test_ai_settings_and_info() -> None:
    ai = parse(
        mqtt(
            "aiSettings",
            "anything",
            "whatever",
            {
                "ai_settings": {
                    "status": 3,
                    "type": 2,
                    "count": 60,
                    "notice_type": [0, 1],
                    "sensitivity_level": [1, 1],
                }
            },
        )
    )
    assert isinstance(ai.report, AiSettingsReport)
    assert ai.report.settings.status == 3
    info = parse(mqtt("info", "query", "done", {"state": "free"}))
    assert isinstance(info.report, InfoReport)
    assert info.report.printer_state == "free"


# -- file (PROTOCOL C §4.12) --------------------------------------------------------------------


def test_file_lists() -> None:
    records = [
        {
            "filename": "part.gcode",
            "timestamp": 1760000000,
            "size": 2345678,
            "is_dir": False,
        },
        {"filename": "folder", "is_dir": True},
        {"filename": "bad", "is_dir": "no"},
        None,
    ]
    local = parse(mqtt("file", "listLocal", "done", {"records": records}))
    assert isinstance(local.update, FileListUpdate)
    assert local.update.source is FileSource.LOCAL
    assert local.update.records is not None
    assert [r.filename for r in local.update.records] == ["part.gcode", "folder"]
    assert local.update.records[1].size == 0
    usb = parse(mqtt("file", "listUdisk", "done", {"records": []}))
    assert usb.update == FileListUpdate(FileSource.UDISK, ())
    null_list = parse(mqtt("file", "listLocal", "done", {"records": None}))
    assert null_list.update == FileListUpdate(FileSource.LOCAL, None)
    null_data = parse(mqtt("file", "listUdisk", "done", None))
    assert null_data.update == FileListUpdate(FileSource.UDISK, None)


def test_file_other_actions() -> None:
    assert parse(mqtt("file", "deleteLocal", "success")).update == FileDeletedUpdate(
        FileSource.LOCAL
    )
    assert parse(mqtt("file", "deleteUdisk", "success")).update == FileDeletedUpdate(
        FileSource.UDISK
    )
    recommend = parse(mqtt("file", "cloudRecommendList", "done", {"anything": 1}))
    assert recommend.understood
    assert recommend.update is None
    assert not parse(mqtt("file", "fileDetails", "done")).understood


# -- ota (PROTOCOL C §4.13) -------------------------------------------------------------------


def test_printer_ota() -> None:
    version = parse(
        mqtt(
            "ota", "reportVersion", "done", {"firmware_version": "2.8.0", "model_id": 1}
        )
    )
    assert version.update == FirmwareReport(FirmwareStep.VERSION, version="2.8.0")
    assert parse(mqtt("ota", "update", "start")).update == FirmwareReport(
        FirmwareStep.START
    )
    downloading = parse(mqtt("ota", "update", "downloading", {"progress": 42}))
    assert downloading.update == FirmwareReport(
        FirmwareStep.DOWNLOADING, download_progress=42
    )
    updating = parse(mqtt("ota", "update", "updating", {"current_progress": 10}))
    assert updating.update == FirmwareReport(FirmwareStep.UPDATING, install_progress=10)
    assert not parse(mqtt("ota", "update", "update-success")).understood
    assert not parse(mqtt("ota", "other", "done")).understood


def test_ace_ota_topic() -> None:
    topic = f"{PUBLIC}/multiColorBox/report/1"  # box index = segment 9
    message = parse(mqtt("ota", "update", "downloading", {"progress": 5}), topic)
    assert message.update == FirmwareReport(
        FirmwareStep.DOWNLOADING, is_ace=True, box_index=1, download_progress=5
    )
    success = parse(
        mqtt("ota", "update", "updateSuccessProcessed"), f"{PUBLIC}/multiColorBox/ota"
    )
    assert success.update == FirmwareReport(
        FirmwareStep.SUCCESS, is_ace=True, box_index=0
    )
    odd = parse(
        mqtt("ota", "update", "update-success"), f"{PUBLIC}/multiColorBox/report/x"
    )
    assert isinstance(odd.update, FirmwareReport)
    assert odd.update.box_index == 0


# -- events ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["event", "printerevent", "printer_event"])
def test_events(kind: str) -> None:
    data = {"code": 10107, "msg": "filament", "msgid": "m", "state": "s", "action": "a"}
    message = parse(mqtt(kind, "report", "done", data, code=10107, msg="filament"))
    assert isinstance(message.update, PrinterEvent)
    assert message.update.data == data
    assert message.fault is not None
    assert message.fault.code == 10107
    assert parse(mqtt(kind, "x", "y", None)).update == PrinterEvent({})


# -- null payloads (PROTOCOL C §4.20) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "action", "state"),
    [
        ("tempature", "auto", "done"),
        ("fan", "auto", "done"),
        ("light", "query", "done"),
        ("peripherie", "query", "done"),
        ("axis", "query", "done"),
        ("axis", "move", "done"),
        ("multiColorBox", "getInfo", "success"),
        ("multiColorBox", "autoUpdateInfo", "done"),
        ("extfilbox", "reportInfo", "success"),
        ("aiSettings", "query", "done"),
        ("info", "query", "done"),
        ("file", "listLocal", "done"),
        ("ota", "reportVersion", "done"),
        ("print", "start", "printing"),
        ("print", "update", "updated"),
        ("print", "getSliceParam", "done"),
        ("lastWill", "onlineReport", "online"),
        ("status", "workReport", "busy"),
        ("event", "report", "done"),
    ],
)
def test_null_data_is_tolerated(kind: str, action: str, state: str) -> None:
    message = parse(mqtt(kind, action, state, None))
    assert message.understood


def test_null_fields_never_clear_known_state() -> None:
    temps = parse(
        mqtt(
            "tempature",
            "auto",
            "done",
            {"curr_nozzle_temp": None, "curr_hotbed_temp": 60},
        )
    )
    assert isinstance(temps.report, TemperatureReport)
    assert temps.report.temperatures.nozzle is None  # None = "keep", never 0
    boxes = parse(
        mqtt(
            "multiColorBox",
            "getInfo",
            "success",
            {"multi_color_box": [{"id": 0, "temp": None, "slots": None}]},
        )
    )
    assert isinstance(boxes.report, MultiColorBoxReport)
    assert boxes.report.boxes is not None
    assert boxes.report.boxes[0].temp is None


def test_parser_errors_do_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("anycubic_cloud_client.messages.parse_message", broken)
    message = parse(mqtt("fan", "auto", "done", {}))
    assert not message.understood
