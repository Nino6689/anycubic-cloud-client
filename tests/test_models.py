"""Typed models from the captured and constructed payloads (PROTOCOL Part B)."""

from __future__ import annotations

import copy

import pytest

from anycubic_cloud_client import (
    Account,
    CameraCredentials,
    CloudFile,
    GcodeInfo,
    Job,
    JobDetail,
    PrinterDetail,
    PrinterSummary,
    Region,
    select_latest_job,
)
from anycubic_cloud_client.models import (
    AceUnit,
    ExternalHolder,
    FirmwareInfo,
    PrinterFile,
    PrinterModel,
    as_bool,
    as_int,
    as_json_map,
    as_number,
    as_rgb,
    function_names,
    paint_color_of,
    parse_ace_units,
    parse_duration_minutes,
    parse_kilograms,
)

from .payloads import (
    CAMERA_REPLY,
    CLOUD_FILE,
    EXTERNAL_SHELVES_ABSENT,
    GCODE_INFO,
    JOB_DETAIL,
    JOB_RECORD,
    PRINTER_ID,
    PRINTER_KEY,
    detail,
)


def test_field_helpers() -> None:
    assert as_int("12345") == 12345
    assert as_int("-3") == -3
    assert as_int(2.0) == 2
    assert as_int(2.5) is None
    assert as_int(True) is None
    assert as_int("x") is None
    assert as_number("1.5") == 1.5
    assert as_number("7") == 7
    assert as_number("7.0") == 7.0
    assert as_number(float("nan")) is None
    assert as_number("inf") is None
    assert as_number(False) is None
    assert as_number("x") is None
    assert as_number([1]) is None
    assert as_bool(1) is True
    assert as_bool("0") is False
    assert as_bool(2) is None
    assert as_bool(True) is True
    assert as_rgb([1, None, 2, 3]) == (1, 2, 3)
    assert as_rgb([1, 2]) is None
    assert as_rgb([1, "x", 3]) is None
    assert as_rgb("red") is None
    assert as_json_map('{"a": 1}') == {"a": 1}
    assert as_json_map("{broken") is None
    assert as_json_map("[1]") is None
    assert as_json_map("") is None
    assert as_json_map({"b": 2}) == {"b": 2}


@pytest.mark.parametrize(
    ("value", "minutes"),
    [
        ("798hour29min", 798 * 60 + 29),
        ("95", 95),
        ("12.5", 12.5),
        (30, 30),
        ("", None),
        ("soon", None),
        (None, None),
        ("1h 2m", None),
    ],
)
def test_duration(value: object, minutes: object) -> None:
    assert parse_duration_minutes(value) == minutes


def test_kilograms() -> None:
    assert parse_kilograms("18.17kg") == 18.17
    assert parse_kilograms("3KG") == 3.0
    assert parse_kilograms("18.17") is None
    assert parse_kilograms(18.17) is None


def test_printer_detail_from_capture() -> None:
    printer = PrinterDetail.from_data(detail())
    assert printer.id == PRINTER_ID
    assert printer.key == PRINTER_KEY
    assert PRINTER_KEY not in repr(printer)
    assert printer.machine_type == 20025
    assert printer.is_online
    assert not printer.is_busy
    assert printer.base.print_count == 174
    assert printer.base.print_totaltime_minutes == 798 * 60 + 29
    assert printer.base.material_used_kg == 18.17
    assert printer.base.material_type == "Filament"
    assert printer.nozzle_temp == 31
    assert printer.hotbed_temp == 28
    assert printer.machine_data["suffix"] == "gcode"
    assert printer.firmware is not None
    assert printer.firmware.firmware_version == "2.7.2.7"
    assert printer.firmware.latest_version == "2.7.2.7"
    assert not printer.firmware.need_update
    assert printer.ace_firmware[0].box_id == 0
    assert printer.ace_firmware[0].box_name == "ACE Pro"
    assert printer.supported_functions == (
        "FILE_MANAGER",
        "FDM_AXIS_MOVE",
        "FDM_PEER_VIDEO",
        "TIME_LAPSE",
        "BOX_LIGHT",
        "MULTI_COLOR_BOX",
    )
    # a single ACE arrives as an object (quirk Q4)
    assert printer.has_ace
    (unit,) = printer.ace_units
    assert unit.id == 1
    assert unit.position == 0
    assert unit.model_id == 40001
    assert unit.auto_feed is True
    assert unit.loaded_slot_raw == -1
    assert unit.loaded_slot == 0  # falls back to the first slot in status 5
    assert unit.feed_status.code == 200
    assert not unit.drying.is_drying
    assert unit.slot(3) is not None
    assert unit.slot(3).color == (239, 240, 241)
    assert unit.slot(2) is None
    assert unit.slots[0].color_group == ((175, 175, 175, 255),)
    assert unit.slots[0].is_loaded
    assert not unit.slots[0].is_empty
    holder = printer.external_holder
    assert holder is not None
    assert holder.material == "PLA"
    assert holder.loaded
    assert printer.features["shengwang_rtc_support"] is True
    assert printer.temp_limit.nozzle == (185, 320)
    assert printer.free_temp_limit.hotbed == (0, 110)
    assert printer.max_box_num == 4
    assert printer.nozzle_diameter == 0.4
    assert printer.tools[0]["function_name"] == "Document Management"
    assert printer.raw["is_queue_task"] == 0


def test_all_null_holder_is_absent() -> None:
    printer = PrinterDetail.from_data(detail(external_shelves=EXTERNAL_SHELVES_ABSENT))
    assert printer.external_holder is None
    assert ExternalHolder.from_data({"id": None, "type": "", "loaded": 0}) is not None


def test_nulls_never_discard_the_record() -> None:
    data = detail(
        machine_data=None,
        parameter={"curr_hotbed_temp": None, "curr_nozzle_temp": "x"},
        version={"need_update": None, "firmware_version": None},
        multi_color_box=[
            {
                "id": 0,
                "status": None,
                "temp": None,
                "loaded_slot": None,
                "feed_status": None,
                "drying_status": None,
                "slots": [None, {"index": None}],
            },
            {"status": 1},  # no id: rejected
        ],
        external_shelves={
            "id": None,
            "type": None,
            "loaded": 1,
            "color": [1, None, 2, 3],
        },
        features=[{"name": "x"}, "junk", {"name": "y", "value": False}],
        temp_limit={"nozzle_temp_limit": [1, 2, 3]},
        is_printing=None,
        tools=[None, {"id": None}],
    )
    printer = PrinterDetail.from_data(data)
    assert printer.machine_data == {}
    assert printer.hotbed_temp is None
    assert printer.nozzle_temp is None
    assert printer.firmware is not None
    assert printer.firmware.firmware_version is None
    assert printer.firmware.latest_version is None
    (unit,) = printer.ace_units
    assert unit.status == 0
    assert unit.temp == 0
    assert unit.loaded_slot_raw == -1
    assert unit.loaded_slot is None
    assert unit.feed_status.slot_index == -1
    assert len(unit.slots) == 1
    assert unit.slots[0].index is None
    assert printer.external_holder is not None
    assert printer.external_holder.color == (1, 2, 3)
    assert dict(printer.features) == {"y": False}
    assert printer.temp_limit.nozzle is None
    assert printer.is_printing == 1
    assert len(printer.tools) == 1


def test_ace_units_list_and_positions() -> None:
    units = parse_ace_units([{"id": 0}, "junk", {"id": 1, "loaded_slot": 2}])
    assert [(u.id, u.position) for u in units] == [(0, 0), (1, 1)]
    assert units[1].loaded_slot == 2
    assert parse_ace_units(None) == ()
    assert AceUnit.from_data({"id": "x"}, 0) is None


def test_printer_summary() -> None:
    record = {
        "id": "12345",
        "user_id": 424242,
        "name": "S1",
        "key": PRINTER_KEY,
        "machine_type": 20025,
        "model": "Anycubic Kobra S1",
        "device_status": 2,
        "material_used": "1.5kg",
        "print_totaltime": "10hour0min",
        "material_type": "resin",
        "machine_mac": "<mac>",
        "type_function_ids": [2006, "7", None],
        "version": {"need_update": 1, "firmware_version": "1", "target_version": "2"},
    }
    summary = PrinterSummary.from_data(record)
    assert summary.id == 12345
    assert not summary.is_online
    assert not summary.is_busy
    assert summary.is_printing == 1  # absent reads as free
    assert summary.material_used_kg == 1.5
    assert summary.print_totaltime_minutes == 600
    assert summary.material_type == "Resin"
    assert summary.supports_ace
    assert summary.type_function_ids == (2006, 7)
    assert summary.firmware is not None
    assert summary.firmware.need_update
    assert summary.firmware.latest_version == "2"
    assert "<mac>" not in repr(summary)
    assert not PrinterSummary.from_data({"type_function_ids": [2]}).supports_ace


def test_function_names_skip_unknown() -> None:
    assert function_names([1, 43, 2006]) == ("AXLE_MOVEMENT", "MULTI_COLOR_BOX")


def test_job_record() -> None:
    job = Job.from_data(JOB_RECORD)
    assert job.id == 900001
    assert job.taskid == 900002
    assert job.printer_id == PRINTER_ID
    assert job.name == "benchy_PLA_0.2"
    assert job.reason is None  # the server's 0 means none
    assert job.total_time_minutes == 137
    assert job.settings is not None
    assert job.settings.curr_layer == 120
    assert job.settings.supplies_usage == 31783
    assert job.settings.state == "printing"
    assert job.slice_param is not None
    assert job.slice_param.paint_infos[0].filament_used == 94.5
    assert job.slice_param.layer_height == 0.2
    assert job.slice_result == {
        "size_x": 60.0,
        "size_y": 31.0,
        "size_z": 48.0,
        "used_filament": 94.5,
    }
    # no absolute img: image base + slice_param.image_id
    assert (
        job.image_url
        == "https://workbentch.s3.us-east-2.amazonaws.com/relative/image/path.png"
    )
    assert not job.is_paused
    china = Job.from_data(
        {**JOB_RECORD, "img": "https://cdn.example.invalid/a.png"}, Region.CHINA
    )
    assert china.image_url == "https://cdn.example.invalid/a.png"


def test_job_tolerates_bad_json_and_null_status() -> None:
    job = Job.from_data(
        {
            "id": 1,
            "settings": "{broken",
            "slice_param": None,
            "status": None,
            "reason": "Nozzle clogged",
            "gcode_name": "",
        }
    )
    assert job.settings is None
    assert job.slice_param is None
    assert job.status is None
    assert job.reason == "Nozzle clogged"
    assert job.name is None
    assert job.image_url is None
    assert Job.from_data({"status": 0}).status == 0


def _job(job_id: int, printer: int, name: str | None, img: str = "") -> Job:
    return Job.from_data(
        {"id": job_id, "printer_id": printer, "gcode_name": name, "img": img}
    )


def test_latest_job_rules() -> None:
    jobs = [
        _job(1, 7, "other", "https://i/1.png"),
        _job(2, 5, "benchy.gcode"),
        _job(3, 7, "benchy.gcode", "https://i/3.png"),
        _job(4, 5, "benchy.gcode", "https://i/4.png"),
    ]
    latest = select_latest_job(jobs, 5)
    assert latest is not None
    assert latest.id == 2
    assert latest.image_url == "https://i/3.png"  # borrowed from any printer
    assert select_latest_job(jobs, 7).id == 1  # type: ignore[union-attr]
    assert select_latest_job(jobs, 99) is None
    lonely = select_latest_job([_job(2, 5, "x.gcode"), _job(3, 5, "y.gcode", "u")], 5)
    assert lonely is not None
    assert lonely.image_url is None
    nameless = select_latest_job([_job(2, 5, None)], 5)
    assert nameless is not None
    assert nameless.id == 2


def test_image_search_limit() -> None:
    jobs = [_job(0, 5, "a.gcode")] + [_job(i, 9, "z") for i in range(1, 201)]
    jobs.append(_job(999, 9, "a.gcode", "https://i/far.png"))
    latest = select_latest_job(jobs, 5)
    assert latest is not None
    assert latest.image_url is None


def test_job_detail() -> None:
    job = JobDetail.from_data(JOB_DETAIL)
    assert job.print_speed_mode == 2
    assert job.speed_mode_title() == "Standard"
    assert job.speed_mode_title(3) == "Sport"
    assert job.speed_mode_title(9) is None
    assert job.limits.nozzle == (185, 320)
    assert job.limits.hotbed == (35, 120)
    assert job.target_nozzle_temp == 220
    assert job.type_function_ids == (1, 2)
    bare = JobDetail.from_data(
        {"print_speed_model_des": [{"title": "x"}], "temp": None}
    )
    assert bare.speed_modes == ()
    assert bare.limits.nozzle is None


def test_gcode_info() -> None:
    info = GcodeInfo.from_data(GCODE_INFO)
    assert info.file_id == 700001
    assert [p.paint_index for p in info.paint_infos] == [1, 0]
    assert GcodeInfo.from_data({"file_id": 1}).paint_infos == ()


def test_cloud_file() -> None:
    file = CloudFile.from_data(CLOUD_FILE)
    assert file.id == 700001
    assert file.gcode_id == 700002
    assert file.name == "benchy.gcode"
    assert file.size_mb == 1.234567
    assert file.material == "PLA"
    assert file.dimensions is not None
    assert file.dimensions.z == 48.0
    assert file.is_temp_file is False
    empty = CloudFile.from_data({"thumbnail": "", "material_name": "", "size_x": None})
    assert empty.id is None
    assert empty.thumbnail is None
    assert empty.material is None
    assert empty.dimensions is None
    assert empty.size_mb is None
    assert "signed" not in repr(file)


def test_printer_file_and_model() -> None:
    assert PrinterFile("a.gcode", False, size=2_000_000).size_mb == 2.0
    model = PrinterModel.from_data({"machine_type": 20025, "name": "S1", "img": ""})
    assert model.machine_type == 20025
    assert model.img is None


def test_account_identifier() -> None:
    assert (
        Account.from_data({"id": 5, "user_email": "", "mobile": ""}).identifier == "5"
    )
    assert Account(user_id=None).identifier == ""
    assert "@" not in repr(Account.from_data({"id": 5, "user_email": "a@b"}))


def test_camera_credentials() -> None:
    credentials = CameraCredentials.from_data(CAMERA_REPLY["data"])
    assert credentials is not None
    assert credentials.app_id == "FAKEAGORAAPPID"
    assert credentials.channel == PRINTER_KEY
    assert credentials.client_uid == 6001
    assert credentials.publisher_uid == 5002  # shengwang_device wins
    assert credentials.wire_encryption_mode == "aes-256-gcm2"
    assert credentials.is_encrypted
    assert credentials.msgid == "fake-msgid-0001"
    text = repr(credentials)
    for secret in ("007fakertctoken", "0123456789abcdef", "FAKEAGORAAPPID"):
        assert secret not in text
    data = copy.deepcopy(CAMERA_REPLY["data"])
    del data["shengwang_device"]
    data["shengwang"]["encryption_mode"] = "none"
    fallback = CameraCredentials.from_data(data)
    assert fallback is not None
    assert fallback.publisher_uid == 5001
    assert not fallback.is_encrypted
    assert fallback.wire_encryption_mode is None
    assert CameraCredentials.from_data({"msgid": "x"}) is None
    data["shengwang"]["rtc_token"] = ""
    assert CameraCredentials.from_data(data) is None


def test_paint_colour_keys() -> None:
    assert paint_color_of({"paint_color": [1, 2, 3, 255]}) == (1, 2, 3)
    assert paint_color_of({"color": "#0A0B0C"}) == (10, 11, 12)
    assert paint_color_of({"color": "0a0b0cff"}) == (10, 11, 12)
    assert paint_color_of({"color": "red"}) is None
    assert paint_color_of({"paint_color": [1, 2]}) is None


def test_firmware_info_defaults() -> None:
    info = FirmwareInfo.from_data({})
    assert not info.need_update
    assert info.latest_version is None
