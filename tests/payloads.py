"""Payloads from docs/PROTOCOL.md, with its placeholders filled by fake values."""

from __future__ import annotations

import copy
from typing import Any

PRINTER_ID = 12345
PRINTER_KEY = "fakeprinterkey0001"
MACHINE_TYPE = 20025

# PROTOCOL B §2.3.12 (Kobra S1 with one ACE Pro, idle).
PRINTER_DETAIL: dict[str, Any] = {
    "is_queue_task": 0,
    "base": {
        "print_count": 174,
        "print_totaltime": "798hour29min",
        "material_type": "Filament",
        "material_used": "18.17kg",
        "description": "<printer_serial>",
        "create_time": 1751145530,
        "firmware_version": "2.7.2.7",
        "machine_mac": "<mac>",
    },
    "name": "Anycubic Kobra S1",
    "id": PRINTER_ID,
    "key": PRINTER_KEY,
    "img": "https://cdn.example.invalid/device/kobra_s1_1.png",
    "machine_type": MACHINE_TYPE,
    "device_status": 1,
    "is_printing": 1,
    "model": "Anycubic Kobra S1",
    "free_temp_limit": {"hotbed_temp_limit": [0, 110], "nozzle_temp_limit": [0, 320]},
    "temp_limit": {"hotbed_temp_limit": [35, 120], "nozzle_temp_limit": [185, 320]},
    "machine_data": {
        "name": "Anycubic Kobra S1",
        "pixel": 34.4,
        "res_x": 11520,
        "res_y": 5120,
        "format": "pw0Img",
        "size_x": 250,
        "size_y": 250,
        "size_z": 260,
        "suffix": "gcode",
        "anti_max": 8,
    },
    "rotate_deg": 0,
    "type_function_ids": [2, 13, 22, 39, 41, 43, 44, 45, 47, 48, 2006],
    "parameter": {"curr_hotbed_temp": 28, "curr_nozzle_temp": 31},
    "nozzle_diameter": 0.4,
    "tools": [
        {
            "id": 1,
            "typd_id": 2,
            "model_id": 20025,
            "type_function_id": 2,
            "parent_id": 0,
            "function_name": "Document Management",
            "function_des": "View or delete files stored locally on the printer",
            "control": 0,
            "param": [],
            "icon_url": "https://cdn.example.invalid/php/img/4/2.png",
            "function_type": 1,
            "status": 1,
            "show_place": 1,
        }
    ],
    "advance": [],
    "help_url": "https://wiki.example.invalid/en/fdm-3d-printer/kobra-s1-Combo",
    "version": {
        "need_update": 0,
        "firmware_version": "2.7.2.7",
        "update_progress": 0,
        "update_date": 0,
        "update_status": "",
        "update_desc": "1.Fixed system errors ...\n2.Optimized key processes ...",
        "force_update": 0,
        "target_version": "2.7.2.7",
        "time_cost": 10,
        "img": "https://cdn.example.invalid/device/kobra_s1_1.png",
    },
    "quick_start_url": "https://wiki.example.invalid/...",
    "is_read_quick_start_url": 0,
    "maintenance_manual_url": "https://wiki.example.invalid/...",
    "multi_color_box_version": [
        {
            "box_name": "ACE Pro",
            "need_update": 0,
            "firmware_version": "1.3.863",
            "update_desc": "",
            "force_update": 0,
            "target_version": "1.3.863",
            "time_cost": 0,
            "update_progress": 0,
            "update_date": 0,
            "update_status": "",
            "img": "https://cdn.example.invalid/device/new_multi_color_box.png",
            "box_id": 0,
        }
    ],
    "head_tools_model": 0,
    "external_shelves": {
        "type": "PLA",
        "color": [233, 157, 67],
        "loaded": 1,
        "id": 7,
        "status_type": -1,
        "current_status": -1,
    },
    "need_update": 0,
    "releasefilm_url": "https://wiki.example.invalid/...",
    "multi_color_box": {
        "id": 1,
        "status": 1,
        "temp": 33,
        "humidity": 0,
        "model_id": 40001,
        "auto_feed": 1,
        "loaded_slot": -1,
        "feed_status": {
            "code": 200,
            "type": -1,
            "current_status": -1,
            "slot_index": -1,
        },
        "drying_status": {
            "status": 0,
            "duration": 0,
            "target_temp": 0,
            "remain_time": 0,
        },
        "curr_nozzle_temp": 31,
        "target_nozzle_temp": 0,
        "slots": [
            {
                "index": 0,
                "sku": "",
                "type": "PETG",
                "color": [175, 175, 175],
                "status": 5,
                "edit_status": 1,
                "color_group": [[175, 175, 175, 255]],
                "icon_type": 0,
                "consumables_percent": 0,
            },
            {
                "index": 3,
                "sku": "<sku>",
                "type": "PETG",
                "color": [239, 240, 241],
                "status": 5,
                "edit_status": 0,
                "color_group": [[239, 240, 241, 255]],
                "icon_type": 0,
                "consumables_percent": 0,
            },
        ],
    },
    "features": [
        {"name": "auto_leveling_support", "value": True},
        {"name": "camera_timelapse_support", "value": True},
        {"name": "shengwang_rtc_support", "value": True},
    ],
    "max_box_num": 4,
}

# PROTOCOL B §2.3.8: firmware 2.0.1.9 sends this when there is no holder.
EXTERNAL_SHELVES_ABSENT: dict[str, Any] = {
    "brand_name": None,
    "color": [255, 255, 255],
    "current_status": 11,
    "id": None,
    "loaded": None,
    "material_name": None,
    "status_type": 2,
    "type": "",
}

# PROTOCOL B §3.1.5 (shape only).
JOB_RECORD: dict[str, Any] = {
    "id": 900001,
    "taskid": 900002,
    "user_id": 424242,
    "printer_id": PRINTER_ID,
    "gcode_id": 900003,
    "model": 0,
    "img": "",
    "estimate": 12345,
    "remain_time": 42,
    "print_time": 95,
    "progress": 69,
    "pause": 0,
    "print_status": 1,
    "reason": 0,
    "status": 1,
    "create_time": 1790000000,
    "start_time": 1790000100,
    "end_time": 0,
    "total_time": "2hour17min",
    "gcode_name": "benchy_PLA_0.2.gcode",
    "settings": (
        '{"curr_layer":120,"total_layers":240,"supplies_usage":31783,'
        '"state":"printing","slicer":"<slicer>"}'
    ),
    "slice_param": (
        '{"image_id":"relative/image/path.png","layer_height":0.2,'
        '"paint_infos":[{"paint_index":0,"material_type":"PLA","filament_used":94.5}]}'
    ),
    "slice_result": '{"size_x":60.0,"size_y":31.0,"size_z":48.0,"used_filament":94.5}',
    "source": "<source>",
    "key": PRINTER_KEY,
    "type": "<printer_type>",
    "machine_type": MACHINE_TYPE,
    "printer_name": "<printer_name>",
    "machine_name": "Anycubic Kobra S1",
    "device_status": 1,
    "material": "",
    "material_type": 0,
    "connect_status": 1,
    "slice_data": None,
    "slice_status": 0,
    "ischeck": 0,
    "project_type": 1,
    "printed": 0,
    "slice_start_time": 0,
    "slice_end_time": 0,
    "delete": 0,
    "auto_operation": None,
    "monitor": None,
    "last_update_time": 1790005700,
    "localtask": None,
    "device_message": None,
    "signal_strength": 0,
    "post_title": None,
}

# PROTOCOL D §3.2.
CLOUD_FILE: dict[str, Any] = {
    "id": 700001,
    "gcode_id": 700002,
    "old_filename": "benchy.gcode",
    "filename": "<storage name>",
    "size": 1234567,
    "thumbnail": "https://cdn.example.invalid/thumb.png",
    "estimate": 3600,
    "material_name": "PLA",
    "layer_height": 0.2,
    "supplies_usage": 4567,
    "size_x": 60.0,
    "size_y": 31.0,
    "size_z": 48.0,
    "is_temp_file": 0,
    "url": "https://storage.example.invalid/signed",
}

# PROTOCOL D §1.4.
CAMERA_REPLY: dict[str, Any] = {
    "code": 1,
    "msg": "Operation successful",
    "data": {
        "msgid": "fake-msgid-0001",
        "token": "<opaque, unused>",
        "shengwang": {
            "appid": "FAKEAGORAAPPID",
            "channel": PRINTER_KEY,
            "rtc_token": "007fakertctoken",
            "uid": 5001,
            "client_uid": 6001,
            "event_id": "1790000000-fakeprinterkey0001-abcdef",
            "encryption_key": "0123456789abcdef0123456789abcdef",
            "encryption_kdf_salt": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
            "encryption_mode": "AES_256_GCM2",
        },
        "shengwang_device": {"uid": 5002},
    },
}

GCODE_INFO: dict[str, Any] = {
    "file_id": 700001,
    "gcode_id": 700002,
    "name": "benchy.gcode",
    "size": 1234567,
    "create_time": 1790000000,
    "estimate": 3600,
    "status": 1,
    "progress": 100,
    "machine_class": 0,
    "image_id": "relative/image.png",
    "slice_result": "{}",
    "slice_param": {
        "paint_infos": [
            {"paint_index": 1, "material_type": "PLA", "filament_used": 4.1},
            {"paint_index": 0, "material_type": "PETG", "filament_used": 12.3},
        ]
    },
}

JOB_DETAIL: dict[str, Any] = {
    "z_thick": 0.2,
    "print_speed_mode": 2,
    "print_speed_pct": 100,
    "fan_speed_pct": 60,
    "task_mode": 1,
    "reason_id": 0,
    "type_function_ids": [1, 2],
    "temp": {
        "target_nozzle_temp": 220,
        "target_hotbed_temp": 60,
        "limit": {"hotbed_temp_limit": [35, 120], "nozzle_temp_limit": [185, 320]},
    },
    "print_speed_model_des": [
        {"title": "Silent", "print_speed_mode": 1},
        {"title": "Standard", "print_speed_mode": 2},
        {"title": "Sport", "print_speed_mode": 3},
    ],
}


def detail(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(PRINTER_DETAIL)
    data.update(changes)
    return data


# PROTOCOL C §4 messages (envelopes constructed as the protocol shows them).
def mqtt(
    kind: str,
    action: str,
    state: str,
    data: Any = None,
    code: int = 200,
    msg: str = "done",
) -> dict[str, Any]:
    return {
        "type": kind,
        "action": action,
        "state": state,
        "code": code,
        "msg": msg,
        "msgid": "fake-msgid",
        "timestamp": 0,
        "data": data,
    }


ACE_GET_INFO_DATA: dict[str, Any] = {
    "multi_color_box": [
        {
            "id": 0,
            "status": 1,
            "temp": 33,
            "humidity": 0.0,
            "model_id": 40001,
            "auto_feed": 1,
            "loaded_slot": -1,
            "feed_status": {
                "code": 200,
                "type": -1,
                "current_status": -1,
                "slot_index": -1,
            },
            "drying_status": {
                "status": 0,
                "duration": 0,
                "target_temp": 0,
                "remain_time": 0,
            },
            "slots": [
                {
                    "index": 0,
                    "sku": "",
                    "type": "PLA",
                    "color": [255, 255, 255],
                    "status": 5,
                    "edit_status": 0,
                }
            ],
        }
    ]
}
