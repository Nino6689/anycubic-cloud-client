"""Printer orders over ``POST /work/operation/sendOrder`` (PROTOCOL B §5, D §0.2).

The server acknowledges every body shape with ``Operation successful``, but
the printer silently ignores an order in the wrong shape. The shape of each
order is therefore fixed here: whether ``order_id`` is a string or an integer,
and whether ``project_id`` is present.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Sentinel for "the key is absent" (distinct from ``None`` = JSON null).
ABSENT: Any = object()


class Order(IntEnum):
    """Order ids the library sends (PROTOCOL B §5.3)."""

    START_PRINT = 1
    PAUSE_PRINT = 2
    RESUME_PRINT = 3
    STOP_PRINT = 4
    PRINT_SETTINGS = 6
    LIST_UDISK_FILES = 101
    DELETE_UDISK_FILE = 102
    LIST_LOCAL_FILES = 103
    DELETE_LOCAL_FILE = 104
    MOVE_AXLE = 201
    CAMERA_OPEN = 1001
    MULTI_COLOR_BOX_GET_INFO = 1206
    MULTI_COLOR_BOX_DRY = 1207
    FEED_FILAMENT = 1208
    MULTI_COLOR_BOX_SET_SLOT = 1211
    MULTI_COLOR_BOX_AUTO_FEED = 1212
    MOVE_AXLE_TURN_OFF = 1213
    QUERY_AXIS_POSITION = 1214
    SET_TEMPERATURE = 1216
    SET_FAN_SPEED = 1221
    QUERY_PERIPHERALS = 1231
    GET_LIGHT_STATUS = 1232
    SET_LIGHT_STATUS = 1233
    SET_AI_SETTINGS = 1243


class OrderShape(Enum):
    """Body shapes (PROTOCOL B §5.1)."""

    PRINTER = "P"
    """String ``order_id``, no ``project_id``, ``data`` object or null."""
    QUERY = "Q"
    """String ``order_id``, no ``project_id``, no ``data``."""
    BARE_PROJECT = "B"
    """Integer ``order_id``, ``project_id`` 0, no ``data``."""
    PROJECT = "J"
    """Integer ``order_id``, ``project_id``, ``data`` object."""
    PRINT_CONTROL = "C"
    """Integer ``order_id``, ``project_id``, ``data``, ``ams_info``, null ``settings``.

    Used for orders 1-4."""
    CAMERA = "V"
    """String ``order_id``, top-level ``shengwang_rtc_support: true``."""


#: The shape of every known order. 1233 is P without a job and J with one.
ORDER_SHAPES: Mapping[int, OrderShape] = {
    Order.START_PRINT: OrderShape.PRINT_CONTROL,
    Order.PAUSE_PRINT: OrderShape.PRINT_CONTROL,
    Order.RESUME_PRINT: OrderShape.PRINT_CONTROL,
    Order.STOP_PRINT: OrderShape.PRINT_CONTROL,
    Order.PRINT_SETTINGS: OrderShape.PROJECT,
    Order.LIST_UDISK_FILES: OrderShape.PROJECT,
    Order.DELETE_UDISK_FILE: OrderShape.PROJECT,
    Order.LIST_LOCAL_FILES: OrderShape.PROJECT,
    Order.DELETE_LOCAL_FILE: OrderShape.PROJECT,
    Order.MOVE_AXLE: OrderShape.PRINTER,
    Order.CAMERA_OPEN: OrderShape.CAMERA,
    Order.MULTI_COLOR_BOX_GET_INFO: OrderShape.BARE_PROJECT,
    Order.MULTI_COLOR_BOX_DRY: OrderShape.PROJECT,
    Order.FEED_FILAMENT: OrderShape.PROJECT,
    Order.MULTI_COLOR_BOX_SET_SLOT: OrderShape.PROJECT,
    Order.MULTI_COLOR_BOX_AUTO_FEED: OrderShape.PROJECT,
    Order.MOVE_AXLE_TURN_OFF: OrderShape.PRINTER,
    Order.QUERY_AXIS_POSITION: OrderShape.BARE_PROJECT,
    Order.SET_TEMPERATURE: OrderShape.PRINTER,
    Order.SET_FAN_SPEED: OrderShape.PRINTER,
    Order.QUERY_PERIPHERALS: OrderShape.QUERY,
    Order.GET_LIGHT_STATUS: OrderShape.QUERY,
    Order.SET_AI_SETTINGS: OrderShape.PRINTER,
}


def shape_for(order_id: int, project_id: int | None) -> OrderShape:
    """The body shape for ``order_id``.

    1233 (light) is shape J with a job id and shape P without. An unknown
    order is shape J when a ``project_id`` is given and P otherwise.
    """
    if order_id == Order.SET_LIGHT_STATUS:
        return OrderShape.PROJECT if project_id is not None else OrderShape.PRINTER
    if (shape := ORDER_SHAPES.get(order_id)) is not None:
        return shape
    return OrderShape.PROJECT if project_id is not None else OrderShape.PRINTER


def build_order_body(
    printer_id: int,
    order_id: int,
    *,
    data: Any = ABSENT,
    project_id: int | None = None,
    ams_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON body of one order, in the shape PROTOCOL B §5.1 requires.

    ``data`` left :data:`ABSENT` is sent as ``null`` in shapes P and C and as
    ``{}`` in shape J; shapes Q, B and V never carry ``data``.
    """
    shape = shape_for(order_id, project_id)
    body: dict[str, Any]
    match shape:
        case OrderShape.PRINTER:
            body = {
                "order_id": str(order_id),
                "printer_id": printer_id,
                "data": None if data is ABSENT else data,
            }
        case OrderShape.QUERY:
            body = {"order_id": str(order_id), "printer_id": printer_id}
        case OrderShape.CAMERA:
            body = {
                "order_id": str(order_id),
                "printer_id": printer_id,
                "shengwang_rtc_support": True,
            }
        case OrderShape.BARE_PROJECT:
            body = {
                "order_id": int(order_id),
                "printer_id": printer_id,
                "project_id": project_id or 0,
            }
        case OrderShape.PROJECT:
            body = {
                "order_id": int(order_id),
                "printer_id": printer_id,
                "project_id": project_id or 0,
                "data": {} if data is ABSENT else data,
            }
        case OrderShape.PRINT_CONTROL:
            body = {
                "order_id": int(order_id),
                "printer_id": printer_id,
                "project_id": project_id or 0,
                "data": None if data is ABSENT else data,
                "ams_info": ams_info,
                "settings": None,
            }
    return body


# --------------------------------------------------------------------------
# ``data`` builders (PROTOCOL B §5.4)
# --------------------------------------------------------------------------


class Axis(IntEnum):
    """``axis`` of order 201."""

    X = 1
    Y = 2
    Z = 3
    XY = 4
    """X and Y (not Z)."""


class MoveType(IntEnum):
    """``move_type`` of order 201."""

    NEGATIVE = 0
    POSITIVE = 1
    HOME = 2


class FeedType(IntEnum):
    """``feed_status.type`` of order 1208."""

    FEED = 1
    RETRACT = 2
    FINISH = 3


class FileSource(Enum):
    """Where a printer-held file lives."""

    LOCAL = "local"
    """Internal storage: list 103, delete 104, ``filetype`` 1."""
    UDISK = "udisk"
    """USB stick: list 101, delete 102, ``filetype`` 2."""

    @property
    def list_order(self) -> Order:
        return (
            Order.LIST_LOCAL_FILES
            if self is FileSource.LOCAL
            else Order.LIST_UDISK_FILES
        )

    @property
    def delete_order(self) -> Order:
        return (
            Order.DELETE_LOCAL_FILE
            if self is FileSource.LOCAL
            else Order.DELETE_UDISK_FILE
        )

    @property
    def filetype(self) -> int:
        return 1 if self is FileSource.LOCAL else 2


#: Defaults of order 1243 when the printer never reported its settings.
AI_SETTINGS_DEFAULTS: Mapping[str, Any] = {
    "type": 2,
    "count": 60,
    "sensitivity_level": [1, 1],
    "notice_type": [0, 1],
}

#: ``status`` of order 1243: 3 = on, 0 = off.
AI_STATUS_ON = 3
AI_STATUS_OFF = 0


def temperature_data(nozzle: int | None, bed: int | None) -> dict[str, int] | None:
    """Order 1216: type 0 nozzle, 1 bed, 2 both; the unused target is 0.

    ``None`` when neither target is given (nothing is sent).
    """
    if nozzle is None and bed is None:
        return None
    kind = (
        2
        if nozzle is not None and bed is not None
        else (0 if nozzle is not None else 1)
    )
    return {
        "type": kind,
        "target_nozzle_temp": int(nozzle or 0),
        "target_hotbed_temp": int(bed or 0),
    }


def fan_data(
    fan_speed_pct: int | None = None,
    aux_fan_speed_pct: int | None = None,
    box_fan_level: int | None = None,
) -> dict[str, int]:
    """Order 1221: exactly one key, in the priority part fan, aux fan, box fan."""
    if fan_speed_pct is not None:
        return {"fan_speed_pct": int(fan_speed_pct)}
    if aux_fan_speed_pct is not None:
        return {"aux_fan_speed_pct": int(aux_fan_speed_pct)}
    if box_fan_level is not None:
        return {"box_fan_level": int(box_fan_level)}
    raise ValueError("give one fan value")


def move_data(axis: Axis, move_type: MoveType, distance: int = 0) -> dict[str, int]:
    """Order 201; homing sends distance 0."""
    return {
        "axis": int(axis),
        "move_type": int(move_type),
        "distance": 0 if move_type is MoveType.HOME else int(distance),
    }


def light_data(
    on: bool, brightness: int | None = None, light_type: int = 1
) -> dict[str, int]:
    """Order 1233: brightness defaults to 100 when on and is 0 when off.

    ``light_type`` is the type the printer last reported; 1 when never
    reported (PROTOCOL B §5.4.7).
    """
    if on:
        level = 100 if brightness is None else int(brightness)
        if not 0 <= level <= 100:
            raise ValueError("brightness must be between 0 and 100")
    else:
        level = 0
    return {"type": int(light_type), "status": 1 if on else 0, "brightness": level}


def drying_data(
    boxes: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Order 1207: ``{"multi_color_box": [...]}``.

    Each entry: ``id`` (the box's position in the list when not given),
    ``status`` 1 start / 0 stop, ``target_temp`` (default 40), ``duration``
    minutes (default 0); ``remain_time`` is always null.
    """
    entries = []
    for position, box in enumerate(boxes):
        box_id = box.get("id")
        entries.append(
            {
                "id": position if box_id is None else int(box_id),
                "drying_status": {
                    "status": int(box.get("status", 0)),
                    "target_temp": int(box.get("target_temp", 40)),
                    "duration": int(box.get("duration", 0)),
                    "remain_time": None,
                },
            }
        )
    return {"multi_color_box": entries}


def feed_data(box_id: int, feed_type: FeedType, slot_index: int = -1) -> dict[str, Any]:
    """Order 1208; a retract always sends slot -1, feeds need a slot ≥ 0."""
    if feed_type is FeedType.RETRACT:
        slot_index = -1
    elif slot_index < 0:
        raise ValueError("feeding needs a slot index >= 0")
    return {
        "multi_color_box": [
            {
                "id": int(box_id),
                "feed_status": {"slot_index": int(slot_index), "type": int(feed_type)},
            }
        ]
    }


def set_slot_data(
    box_id: int, index: int, color: Sequence[int], material: str
) -> dict[str, Any]:
    """Order 1211: define one slot's colour and material."""
    if not 0 <= index <= 3:
        raise ValueError("slot index must be 0-3")
    if len(color) != 3:
        raise ValueError("colour must be [r, g, b]")
    return {
        "multi_color_box": [
            {
                "id": int(box_id),
                "slots": [
                    {
                        "index": int(index),
                        "color": [int(c) for c in color],
                        "type": material,
                    }
                ],
            }
        ]
    }


def auto_feed_data(box_id: int, enabled: bool) -> dict[str, Any]:
    """Order 1212: run-out refill on or off."""
    return {"multi_color_box": [{"id": int(box_id), "auto_feed": 1 if enabled else 0}]}


def delete_file_data(filename: str) -> dict[str, Any]:
    """Orders 102 / 104: ``filetype`` -1 and ``path`` ``/`` always."""
    return {"filename": filename, "filetype": -1, "path": "/"}


def ai_settings_data(
    enabled: bool, current: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Order 1243: ``status`` 3 on / 0 off; the rest copied from the last report.

    ``current`` is the printer's last ``ai_settings`` (any of ``type``,
    ``count``, ``sensitivity_level``, ``notice_type``); missing or null
    values take the defaults 2, 60, ``[1, 1]``, ``[0, 1]``.
    """
    settings: dict[str, Any] = {"status": AI_STATUS_ON if enabled else AI_STATUS_OFF}
    current = current or {}
    for key, default in AI_SETTINGS_DEFAULTS.items():
        value = current.get(key)
        if value is None:
            value = default
        settings[key] = list(value) if isinstance(value, list | tuple) else value
    return {"ai_settings": settings}
