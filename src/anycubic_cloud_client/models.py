"""Typed results of the HTTP API (PROTOCOL Part B).

Every model is a frozen dataclass that keeps the raw ``dict`` it came from
(``raw``, excluded from ``repr``), so new fields reach diagnostics without a
release. Every field is parsed on its own: a missing, null or wrong-typed
value becomes ``None`` (or the documented default) and never discards the rest
of the record (BEHAVIOUR B3).
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum
from types import MappingProxyType
from typing import Any, cast

from .regions import Region

_LOGGER = logging.getLogger(__name__)

_EMPTY: Mapping[str, Any] = MappingProxyType({})

type Number = int | float
type RGB = tuple[int, int, int]

# --------------------------------------------------------------------------
# Field helpers: each returns None for anything it does not recognise.
# --------------------------------------------------------------------------


def as_int(value: object) -> int | None:
    """An int from an int, an integral float or a string of digits."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    return None


def as_number(value: object) -> Number | None:
    """A finite number from a number or a numeric string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() and "." not in value else number
    return None


def as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def as_text(value: object) -> str | None:
    """A string, with ``""`` read as "no value"."""
    return value if isinstance(value, str) and value != "" else None


def as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    number = as_int(value)
    if number in (0, 1):
        return bool(number)
    return None


def as_map(value: object) -> Mapping[str, Any]:
    """A mapping, or an empty one."""
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else _EMPTY


def as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def as_rgb(value: object) -> RGB | None:
    """``[r, g, b]`` with null components dropped; ``None`` unless 3 remain."""
    if not isinstance(value, list):
        return None
    parts = [as_int(v) for v in value if v is not None][:3]
    if len(parts) != 3 or any(p is None for p in parts):
        return None
    red, green, blue = cast("list[int]", parts)
    return (red, green, blue)


def as_json_map(value: object) -> Mapping[str, Any] | None:
    """An object that may arrive JSON-encoded in a string (PROTOCOL B §0.4).

    An unparseable string gives ``None`` instead of failing the record.
    """
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except ValueError:
            _LOGGER.debug("Ignoring a JSON-in-string field that does not parse")
            return None
        if isinstance(decoded, Mapping):
            return cast("Mapping[str, Any]", decoded)
    return None


_HOUR_MIN_RE = re.compile(r"(\d+)hour(\d+)min")
_KG_RE = re.compile(r"(\d+(?:\.\d+)?)kg", re.IGNORECASE)


def parse_duration_minutes(value: object) -> Number | None:
    """Lifetime/total print time in minutes (PROTOCOL B §2.3.2, quirk B §10 Q8).

    A plain number in text is minutes (may be fractional); ``<h>hour<m>min``
    is converted. A JSON number is accepted as minutes too. Anything else is
    no value (not zero, BEHAVIOUR G11).
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return as_number(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (match := _HOUR_MIN_RE.fullmatch(text)) is not None:
        return int(match.group(1)) * 60 + int(match.group(2))
    return as_number(text) if text else None


def parse_kilograms(value: object) -> float | None:
    """``"18.17kg"`` → 18.17; only a number followed by ``kg`` is accepted."""
    if not isinstance(value, str):
        return None
    match = _KG_RE.fullmatch(value.strip())
    return float(match.group(1)) if match is not None else None


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Account:
    """The signed-in account from userInfo (PROTOCOL A §2.6.1)."""

    user_id: int | None
    email: str | None = field(default=None, repr=False)
    mobile: str | None = field(default=None, repr=False)
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @property
    def identifier(self) -> str:
        """E-mail, else mobile, else the decimal user id."""
        if self.email:
            return self.email
        if self.mobile:
            return self.mobile
        if self.user_id is not None:
            return str(self.user_id)
        raw_id = self.raw.get("id")
        return "" if raw_id is None else str(raw_id)

    @property
    def mqtt_identity(self) -> str | None:
        """E-mail, else mobile; ``None`` when neither exists (no MQTT)."""
        return self.email or self.mobile or None

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Account:
        return cls(
            user_id=as_int(data.get("id")),
            email=as_text(data.get("user_email")),
            mobile=as_text(data.get("mobile")),
            raw=data,
        )


# --------------------------------------------------------------------------
# Printers
# --------------------------------------------------------------------------


class Function(IntEnum):
    """Capability ids in ``type_function_ids`` (PROTOCOL B §2.3.9)."""

    AXLE_MOVEMENT = 1
    FILE_MANAGER = 2
    EXPOSURE_TEST = 3
    LCD_PEER_VIDEO = 7
    FDM_AXIS_MOVE = 13
    FDM_PEER_VIDEO = 22
    DEVICE_STARTUP_SELF_TEST = 26
    PRINT_STARTUP_SELF_TEST = 27
    AUTOMATIC_OPERATION = 28
    RESIDUE_CLEAN = 29
    NOVICE_GUIDE = 30
    RELEASE_FILM = 31
    TASK_MODE = 32
    LCD_INTELLIGENT_MATERIALS_BOX = 33
    LCD_AUTO_OUT_IN_MATERIALS = 34
    M7PRO_AUTOMATIC_OPERATION = 35
    AI_DETECTION = 36
    AUTO_LEVELER = 37
    VIBRATION_COMPENSATION = 38
    TIME_LAPSE = 39
    VIDEO_LIGHT = 40
    BOX_LIGHT = 41
    MULTI_COLOR_BOX = 2006


def function_names(ids: Sequence[int]) -> tuple[str, ...]:
    """Names of the known function ids, in the order given; unknown ids skipped."""
    names = []
    for value in ids:
        try:
            names.append(Function(value).name)
        except ValueError:
            continue
    return tuple(names)


def _int_tuple(value: object) -> tuple[int, ...]:
    return tuple(i for i in (as_int(v) for v in as_list(value)) if i is not None)


@dataclass(frozen=True, slots=True)
class FirmwareInfo:
    """A ``version`` object, or one ``multi_color_box_version`` entry (B §2.3.5)."""

    firmware_version: str | None = None
    target_version: str | None = None
    need_update: bool = False
    """``need_update`` is 1; anything else is "no update"."""
    update_progress: int | None = None
    update_status: str | None = None
    update_date: int | None = None
    update_desc: str | None = None
    force_update: int | None = None
    time_cost: int | None = None
    box_id: int | None = None
    box_name: str | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> FirmwareInfo:
        return cls(
            firmware_version=as_text(data.get("firmware_version")),
            target_version=as_text(data.get("target_version")),
            need_update=as_int(data.get("need_update")) == 1,
            update_progress=as_int(data.get("update_progress")),
            update_status=as_str(data.get("update_status")),
            update_date=as_int(data.get("update_date")),
            update_desc=as_str(data.get("update_desc")),
            force_update=as_int(data.get("force_update")),
            time_cost=as_int(data.get("time_cost")),
            box_id=as_int(data.get("box_id")),
            box_name=as_text(data.get("box_name")),
            raw=data,
        )

    @property
    def latest_version(self) -> str | None:
        """The offered version; the installed one when none is offered (V17)."""
        return self.target_version or self.firmware_version


@dataclass(frozen=True, slots=True)
class FeedStatus:
    code: int = -1
    type: int | None = None
    current_status: int | None = None
    slot_index: int = -1


@dataclass(frozen=True, slots=True)
class DryingStatus:
    status: int = 0
    target_temp: Number = 0
    duration: Number = 0
    """Minutes."""
    remain_time: Number = 0
    """Minutes."""

    @property
    def is_drying(self) -> bool:
        return self.status == 1


@dataclass(frozen=True, slots=True)
class AceSlotInfo:
    """One slot of an ACE unit (PROTOCOL B §2.3.10)."""

    index: int | None
    sku: str | None = None
    material: str | None = None
    color: RGB | None = None
    color_group: tuple[tuple[int, ...], ...] = ()
    status: int | None = None
    edit_status: int | None = None
    icon_type: int | None = None
    consumables_percent: Number | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AceSlotInfo:
        return cls(
            index=as_int(data.get("index")),
            sku=as_str(data.get("sku")),
            material=as_str(data.get("type")),
            color=as_rgb(data.get("color")),
            color_group=tuple(
                _int_tuple(entry) for entry in as_list(data.get("color_group"))
            ),
            status=as_int(data.get("status")),
            edit_status=as_int(data.get("edit_status")),
            icon_type=as_int(data.get("icon_type")),
            consumables_percent=as_number(data.get("consumables_percent")),
            raw=data,
        )

    @property
    def is_loaded(self) -> bool:
        """Slot status 5: loaded into the printer."""
        return self.status == 5

    @property
    def is_empty(self) -> bool:
        """``edit_status`` 2: the slot is empty (the old material is still named)."""
        return self.edit_status == 2


@dataclass(frozen=True, slots=True)
class AceUnit:
    """One ACE unit (PROTOCOL B §2.3.10). ``position`` is its place in the list."""

    id: int
    position: int
    status: int = 0
    model_id: int | None = None
    auto_feed: bool | None = None
    loaded_slot_raw: int = -1
    temp: Number = 0
    humidity: Number | None = None
    feed_status: FeedStatus = FeedStatus()
    drying: DryingStatus = DryingStatus()
    curr_nozzle_temp: Number | None = None
    target_nozzle_temp: Number | None = None
    slots: tuple[AceSlotInfo, ...] = ()
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any], position: int) -> AceUnit | None:
        """Parse one box; a box without an ``id`` is rejected (``None``)."""
        box_id = as_int(data.get("id"))
        if box_id is None:
            return None
        feed = as_map(data.get("feed_status"))
        drying = as_map(data.get("drying_status"))
        loaded = as_int(data.get("loaded_slot"))
        return cls(
            id=box_id,
            position=position,
            status=as_int(data.get("status")) or 0,
            model_id=as_int(data.get("model_id")),
            auto_feed=as_bool(data.get("auto_feed")),
            loaded_slot_raw=-1 if loaded is None else loaded,
            temp=as_number(data.get("temp")) or 0,
            humidity=as_number(data.get("humidity")),
            feed_status=FeedStatus(
                code=_default(as_int(feed.get("code")), -1),
                type=as_int(feed.get("type")),
                current_status=as_int(feed.get("current_status")),
                slot_index=_default(as_int(feed.get("slot_index")), -1),
            ),
            drying=DryingStatus(
                status=as_int(drying.get("status")) or 0,
                target_temp=as_number(drying.get("target_temp")) or 0,
                duration=as_number(drying.get("duration")) or 0,
                remain_time=as_number(drying.get("remain_time")) or 0,
            ),
            curr_nozzle_temp=as_number(data.get("curr_nozzle_temp")),
            target_nozzle_temp=as_number(data.get("target_nozzle_temp")),
            slots=tuple(
                AceSlotInfo.from_data(slot)
                for slot in as_list(data.get("slots"))
                if isinstance(slot, Mapping)
            ),
            raw=data,
        )

    @property
    def loaded_slot(self) -> int | None:
        """0-based slot feeding the printer, falling back to a slot in status 5."""
        if self.loaded_slot_raw >= 0:
            return self.loaded_slot_raw
        for slot in self.slots:
            if slot.is_loaded:
                return slot.index
        return None

    def slot(self, index: int) -> AceSlotInfo | None:
        """The slot whose ``index`` is ``index`` (0-3)."""
        for slot in self.slots:
            if slot.index == index:
                return slot
        return None


def _default[T](value: T | None, default: T) -> T:
    return default if value is None else value


def parse_ace_units(value: object) -> tuple[AceUnit, ...]:
    """``multi_color_box``: an object for one ACE, a list for several (B §10 Q4)."""
    raw = [value] if isinstance(value, Mapping) else as_list(value)
    units: list[AceUnit] = []
    for position, entry in enumerate(e for e in raw if isinstance(e, Mapping)):
        unit = AceUnit.from_data(entry, position)
        if unit is not None:
            units.append(unit)
    return tuple(units)


@dataclass(frozen=True, slots=True)
class ExternalHolder:
    """The single-spool external holder (PROTOCOL B §2.3.8)."""

    id: int | None = None
    material: str | None = None
    color: RGB | None = None
    loaded: bool | None = None
    status_type: int | None = None
    current_status: int | None = None
    brand_name: str | None = None
    material_name: str | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ExternalHolder | None:
        """``None`` when absent: ``id`` null, ``type`` empty/null, ``loaded`` null."""
        holder_id = as_int(data.get("id"))
        material = as_text(data.get("type"))
        loaded = as_bool(data.get("loaded"))
        if data.get("id") is None and material is None and data.get("loaded") is None:
            return None
        return cls(
            id=holder_id,
            material=material,
            color=as_rgb(data.get("color")),
            loaded=loaded,
            status_type=as_int(data.get("status_type")),
            current_status=as_int(data.get("current_status")),
            brand_name=as_text(data.get("brand_name")),
            material_name=as_text(data.get("material_name")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class TempLimits:
    """``[min, max]`` °C pairs; ``None`` when not exactly two numbers."""

    nozzle: tuple[Number, Number] | None = None
    hotbed: tuple[Number, Number] | None = None

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TempLimits:
        return cls(
            nozzle=_pair(data.get("nozzle_temp_limit")),
            hotbed=_pair(data.get("hotbed_temp_limit")),
        )


def _pair(value: object) -> tuple[Number, Number] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    low, high = as_number(value[0]), as_number(value[1])
    if low is None or high is None:
        return None
    return (low, high)


def _features(value: object) -> Mapping[str, bool]:
    features = {}
    for entry in as_list(value):
        entry_map = as_map(entry)
        name = as_str(entry_map.get("name"))
        flag = entry_map.get("value")
        if name and isinstance(flag, bool):
            features[name] = flag
    return MappingProxyType(features)


@dataclass(frozen=True, slots=True)
class PrinterSummary:
    """One record of the printer list (E4, E5; PROTOCOL B §2.1)."""

    id: int | None
    name: str | None = None
    user_id: int | None = None
    key: str | None = field(default=None, repr=False)
    machine_type: int | None = None
    model: str | None = None
    img: str | None = None
    type: str | None = None
    device_status: int | None = None
    ready_status: int | None = None
    is_printing: int = 1
    """1 free, 2 busy; absent reads as 1."""
    material_type: str | None = None
    material_used_kg: float | None = None
    print_totaltime_minutes: Number | None = None
    machine_mac: str | None = field(default=None, repr=False)
    type_function_ids: tuple[int, ...] = ()
    firmware: FirmwareInfo | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PrinterSummary:
        version = data.get("version")
        return cls(
            id=as_int(data.get("id")),
            name=as_str(data.get("name")),
            user_id=as_int(data.get("user_id")),
            key=as_text(data.get("key")),
            machine_type=as_int(data.get("machine_type")),
            model=as_str(data.get("model")),
            img=as_text(data.get("img")),
            type=as_str(data.get("type")),
            device_status=as_int(data.get("device_status")),
            ready_status=as_int(data.get("ready_status")),
            is_printing=_default(as_int(data.get("is_printing")), 1),
            material_type=_title(as_text(data.get("material_type"))),
            material_used_kg=parse_kilograms(data.get("material_used")),
            print_totaltime_minutes=parse_duration_minutes(data.get("print_totaltime")),
            machine_mac=as_text(data.get("machine_mac")),
            type_function_ids=_int_tuple(data.get("type_function_ids")),
            firmware=FirmwareInfo.from_data(version)
            if isinstance(version, Mapping)
            else None,
            raw=data,
        )

    @property
    def is_online(self) -> bool:
        return self.device_status == 1

    @property
    def is_busy(self) -> bool:
        return self.is_printing == 2

    @property
    def supports_ace(self) -> bool:
        """Function 2006 (MULTI_COLOR_BOX) is listed."""
        return Function.MULTI_COLOR_BOX in self.type_function_ids


def _title(value: str | None) -> str | None:
    return value.title() if value else value


@dataclass(frozen=True, slots=True)
class PrinterBase:
    """``base`` of the printer detail (PROTOCOL B §2.3.2)."""

    print_count: int | None = None
    print_totaltime_minutes: Number | None = None
    material_used_kg: float | None = None
    material_type: str | None = None
    description: str | None = field(default=None, repr=False)
    create_time: int | None = None
    firmware_version: str | None = None
    machine_mac: str | None = field(default=None, repr=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PrinterBase:
        return cls(
            print_count=as_int(data.get("print_count")),
            print_totaltime_minutes=parse_duration_minutes(data.get("print_totaltime")),
            material_used_kg=parse_kilograms(data.get("material_used")),
            material_type=_title(as_text(data.get("material_type"))),
            description=as_text(data.get("description")),
            create_time=as_int(data.get("create_time")),
            firmware_version=as_text(data.get("firmware_version")),
            machine_mac=as_text(data.get("machine_mac")),
        )


@dataclass(frozen=True, slots=True)
class PrinterDetail:
    """The printer detail (E6, ``GET /v2/printer/info``; PROTOCOL B §2.3)."""

    id: int | None
    name: str | None = None
    key: str | None = field(default=None, repr=False)
    machine_type: int | None = None
    model: str | None = None
    img: str | None = None
    device_status: int | None = None
    is_printing: int = 1
    base: PrinterBase = PrinterBase()
    machine_data: Mapping[str, Any] = field(default=_EMPTY, repr=False)
    nozzle_temp: Number | None = None
    hotbed_temp: Number | None = None
    type_function_ids: tuple[int, ...] = ()
    firmware: FirmwareInfo | None = None
    ace_firmware: tuple[FirmwareInfo, ...] = ()
    tools: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    external_holder: ExternalHolder | None = None
    ace_units: tuple[AceUnit, ...] = ()
    features: Mapping[str, bool] = field(default=_EMPTY, repr=False)
    temp_limit: TempLimits = TempLimits()
    free_temp_limit: TempLimits = TempLimits()
    max_box_num: int | None = None
    nozzle_diameter: Number | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PrinterDetail:
        parameter = as_map(data.get("parameter"))
        version = data.get("version")
        holder = data.get("external_shelves")
        return cls(
            id=as_int(data.get("id")),
            name=as_str(data.get("name")),
            key=as_text(data.get("key")),
            machine_type=as_int(data.get("machine_type")),
            model=as_str(data.get("model")),
            img=as_text(data.get("img")),
            device_status=as_int(data.get("device_status")),
            is_printing=_default(as_int(data.get("is_printing")), 1),
            base=PrinterBase.from_data(as_map(data.get("base"))),
            machine_data=as_map(data.get("machine_data")),
            nozzle_temp=as_number(parameter.get("curr_nozzle_temp")),
            hotbed_temp=as_number(parameter.get("curr_hotbed_temp")),
            type_function_ids=_int_tuple(data.get("type_function_ids")),
            firmware=FirmwareInfo.from_data(version)
            if isinstance(version, Mapping)
            else None,
            ace_firmware=tuple(
                FirmwareInfo.from_data(entry)
                for entry in as_list(data.get("multi_color_box_version"))
                if isinstance(entry, Mapping)
            ),
            tools=tuple(
                entry
                for entry in as_list(data.get("tools"))
                if isinstance(entry, Mapping)
            ),
            external_holder=ExternalHolder.from_data(holder)
            if isinstance(holder, Mapping)
            else None,
            ace_units=parse_ace_units(data.get("multi_color_box")),
            features=_features(data.get("features")),
            temp_limit=TempLimits.from_data(as_map(data.get("temp_limit"))),
            free_temp_limit=TempLimits.from_data(as_map(data.get("free_temp_limit"))),
            max_box_num=as_int(data.get("max_box_num")),
            nozzle_diameter=as_number(data.get("nozzle_diameter")),
            raw=data,
        )

    @property
    def is_online(self) -> bool:
        return self.device_status == 1

    @property
    def is_busy(self) -> bool:
        return self.is_printing == 2

    @property
    def supported_functions(self) -> tuple[str, ...]:
        """Names of the known capability ids (BEHAVIOUR §2.14)."""
        return function_names(self.type_function_ids)

    @property
    def has_ace(self) -> bool:
        return bool(self.ace_units)


@dataclass(frozen=True, slots=True)
class PrinterModel:
    """One entry of the model catalogue (E8, PROTOCOL B §2.5)."""

    machine_type: int | None
    name: str | None = None
    img: str | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PrinterModel:
        return cls(
            machine_type=as_int(data.get("machine_type")),
            name=as_str(data.get("name")),
            img=as_text(data.get("img")),
            raw=data,
        )


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaintInfo:
    """One colour of a file (``paint_infos`` / G-code ``paint_info``).

    ``paint_color`` is the file's own colour when the entry carries one (Q3 in
    docs/QUESTIONS.md).
    """

    paint_index: int
    material_type: str | None = None
    filament_used: Number | None = None
    """Planned grams."""
    paint_color: RGB | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PaintInfo | None:
        index = as_int(data.get("paint_index"))
        if index is None:
            return None
        return cls(
            paint_index=index,
            material_type=as_str(data.get("material_type")),
            filament_used=as_number(data.get("filament_used")),
            paint_color=paint_color_of(data),
            raw=data,
        )


def paint_color_of(data: Mapping[str, Any]) -> RGB | None:
    """The file's colour for one paint entry, when one is present.

    The key is not specified (Q3 in docs/QUESTIONS.md): ``paint_color`` then
    ``color``, as ``[r, g, b(, a)]`` or ``"#RRGGBB"`` text.
    """
    for key in ("paint_color", "color"):
        value = data.get(key)
        if isinstance(value, list) and len(value) >= 3:
            rgb = as_rgb(value[:3])
            if rgb is not None:
                return rgb
        if isinstance(value, str):
            text = value.strip().lstrip("#")
            if re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", text):
                return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    return None


def _paint_infos(value: object) -> tuple[PaintInfo, ...]:
    infos = (PaintInfo.from_data(e) for e in as_list(value) if isinstance(e, Mapping))
    return tuple(info for info in infos if info is not None)


@dataclass(frozen=True, slots=True)
class SliceParam:
    """``slice_param`` decoded (PROTOCOL B §3.1.2)."""

    image_id: str | None = None
    paint_infos: tuple[PaintInfo, ...] = ()
    layer_height: Number | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_value(cls, value: object) -> SliceParam | None:
        data = as_json_map(value)
        if data is None:
            return None
        return cls(
            image_id=as_text(data.get("image_id")),
            paint_infos=_paint_infos(data.get("paint_infos")),
            layer_height=as_number(data.get("layer_height")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class JobSettings:
    """A job's ``settings`` decoded (PROTOCOL B §3.1.1)."""

    curr_layer: int | None = None
    total_layers: int | None = None
    supplies_usage: Number | None = None
    """Millimetres of filament extruded so far."""
    state: str | None = None
    slicer: str | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_value(cls, value: object) -> JobSettings | None:
        data = as_json_map(value)
        if data is None:
            return None
        return cls(
            curr_layer=as_int(data.get("curr_layer")),
            total_layers=as_int(data.get("total_layers")),
            supplies_usage=as_number(data.get("supplies_usage")),
            state=as_str(data.get("state")),
            slicer=as_str(data.get("slicer")),
            raw=data,
        )


def image_url(
    img: object, slice_param: SliceParam | None, region: Region
) -> str | None:
    """Job preview URL (PROTOCOL B §3.6): an absolute ``http…`` image, else the
    region's image base + ``slice_param.image_id``, else none."""
    if isinstance(img, str) and img.startswith("http"):
        return img
    if slice_param is not None and slice_param.image_id:
        return region.endpoints.image_base + slice_param.image_id
    return None


@dataclass(frozen=True, slots=True)
class Job:
    """One record of the job list (E12; PROTOCOL B §3.1)."""

    id: int | None
    printer_id: int | None = None
    taskid: int | None = None
    user_id: int | None = None
    gcode_id: int | None = None
    img: str | None = None
    image_url: str | None = None
    estimate: int | None = None
    remain_time: Number | None = None
    """Minutes."""
    print_time: Number | None = None
    """Minutes."""
    progress: Number | None = None
    pause: int | None = None
    print_status: int | None = None
    reason: str | None = None
    """Failure text; the server's ``0`` reads as none."""
    status: int | None = None
    create_time: int | None = None
    start_time: int | None = None
    end_time: int | None = None
    total_time_minutes: Number | None = None
    gcode_name: str | None = None
    settings: JobSettings | None = None
    slice_param: SliceParam | None = None
    slice_result: Mapping[str, Any] | None = field(default=None, repr=False)
    source: str | None = None
    machine_type: int | None = None
    printer_name: str | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(
        cls, data: Mapping[str, Any], region: Region = Region.INTERNATIONAL
    ) -> Job:
        slice_param = SliceParam.from_value(data.get("slice_param"))
        reason = data.get("reason")
        return cls(
            id=as_int(data.get("id")),
            printer_id=as_int(data.get("printer_id")),
            taskid=as_int(data.get("taskid")),
            user_id=as_int(data.get("user_id")),
            gcode_id=as_int(data.get("gcode_id")),
            img=as_text(data.get("img")),
            image_url=image_url(data.get("img"), slice_param, region),
            estimate=as_int(data.get("estimate")),
            remain_time=as_number(data.get("remain_time")),
            print_time=as_number(data.get("print_time")),
            progress=as_number(data.get("progress")),
            pause=as_int(data.get("pause")),
            print_status=as_int(data.get("print_status")),
            reason=reason
            if isinstance(reason, str) and reason not in ("", "0")
            else None,
            status=as_int(data.get("status")),
            create_time=as_int(data.get("create_time")),
            start_time=as_int(data.get("start_time")),
            end_time=as_int(data.get("end_time")),
            total_time_minutes=parse_duration_minutes(data.get("total_time")),
            gcode_name=as_text(data.get("gcode_name")),
            settings=JobSettings.from_value(data.get("settings")),
            slice_param=slice_param,
            slice_result=as_json_map(data.get("slice_result")),
            source=as_str(data.get("source")),
            machine_type=as_int(data.get("machine_type")),
            printer_name=as_str(data.get("printer_name")),
            raw=data,
        )

    @property
    def name(self) -> str | None:
        """``gcode_name`` without a trailing ``.gcode``."""
        if not self.gcode_name:
            return None
        return self.gcode_name.removesuffix(".gcode")

    @property
    def is_paused(self) -> bool:
        return bool(self.pause)


#: How far past the latest job an image is borrowed from (PROTOCOL B §9).
IMAGE_SEARCH_LIMIT = 200


def select_latest_job(jobs: Sequence[Job], printer_id: int) -> Job | None:
    """The latest job of one printer (PROTOCOL B §3.1.4).

    The first record for the printer, walking newest first. When it has a
    name but no image, the following records (any printer, at most 200) lend
    the image of the first with the same name.
    """
    for position, job in enumerate(jobs):
        if job.printer_id != printer_id:
            continue
        if job.image_url or not job.name:
            return job
        following = jobs[position + 1 : position + 1 + IMAGE_SEARCH_LIMIT]
        for other in following:
            if other.name == job.name and other.image_url:
                return _with_image(job, other.image_url)
        return job
    return None


def _with_image(job: Job, url: str) -> Job:
    return replace(job, image_url=url)


@dataclass(frozen=True, slots=True)
class SpeedModeOption:
    """One entry of ``print_speed_model_des`` (PROTOCOL B §3.2)."""

    mode: int
    title: str | None


@dataclass(frozen=True, slots=True)
class JobDetail:
    """Job detail (E13, ``GET /v2/project/info``; PROTOCOL B §3.2).

    The only source of speed-mode names and temperature limits.
    """

    z_thick: Number | None = None
    print_speed_mode: int | None = None
    print_speed_pct: Number | None = None
    fan_speed_pct: Number | None = None
    task_mode: int | None = None
    reason_id: int | None = None
    type_function_ids: tuple[int, ...] = ()
    target_nozzle_temp: Number | None = None
    target_hotbed_temp: Number | None = None
    limits: TempLimits = TempLimits()
    speed_modes: tuple[SpeedModeOption, ...] = ()
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> JobDetail:
        temp = as_map(data.get("temp"))
        modes = []
        for entry in as_list(data.get("print_speed_model_des")):
            entry_map = as_map(entry)
            mode = as_int(entry_map.get("print_speed_mode"))
            if mode is not None:
                modes.append(SpeedModeOption(mode, as_str(entry_map.get("title"))))
        return cls(
            z_thick=as_number(data.get("z_thick")),
            print_speed_mode=as_int(data.get("print_speed_mode")),
            print_speed_pct=as_number(data.get("print_speed_pct")),
            fan_speed_pct=as_number(data.get("fan_speed_pct")),
            task_mode=as_int(data.get("task_mode")),
            reason_id=as_int(data.get("reason_id")),
            type_function_ids=_int_tuple(data.get("type_function_ids")),
            target_nozzle_temp=as_number(temp.get("target_nozzle_temp")),
            target_hotbed_temp=as_number(temp.get("target_hotbed_temp")),
            limits=TempLimits.from_data(as_map(temp.get("limit"))),
            speed_modes=tuple(modes),
            raw=data,
        )

    def speed_mode_title(self, mode: int | None = None) -> str | None:
        """Display name of ``mode`` (default: the job's own mode)."""
        wanted = self.print_speed_mode if mode is None else mode
        for option in self.speed_modes:
            if option.mode == wanted:
                return option.title
        return None


@dataclass(frozen=True, slots=True)
class GcodeInfo:
    """Sliced-file detail (E16, ``GET /work/gcode/infoFdm``; PROTOCOL D §2.7)."""

    file_id: int | None
    gcode_id: int | None = None
    name: str | None = None
    size: int | None = None
    create_time: int | None = None
    estimate: int | None = None
    status: int | None = None
    progress: int | None = None
    machine_class: int | None = None
    image_id: str | None = None
    slice_param: SliceParam | None = None
    slice_result: Mapping[str, Any] | None = field(default=None, repr=False)
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> GcodeInfo:
        return cls(
            file_id=as_int(data.get("file_id")),
            gcode_id=as_int(data.get("gcode_id")),
            name=as_str(data.get("name")),
            size=as_int(data.get("size")),
            create_time=as_int(data.get("create_time")),
            estimate=as_int(data.get("estimate")),
            status=as_int(data.get("status")),
            progress=as_int(data.get("progress")),
            machine_class=as_int(data.get("machine_class")),
            image_id=as_text(data.get("image_id")),
            slice_param=SliceParam.from_value(data.get("slice_param")),
            slice_result=as_json_map(data.get("slice_result")),
            raw=data,
        )

    @property
    def paint_infos(self) -> tuple[PaintInfo, ...]:
        return self.slice_param.paint_infos if self.slice_param is not None else ()


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dimensions:
    x: Number | None
    y: Number | None
    z: Number | None


@dataclass(frozen=True, slots=True)
class CloudFile:
    """One cloud file (E17; PROTOCOL B §4.1, D §3.2)."""

    id: int | None
    name: str | None = None
    """The user's file name (``old_filename``)."""
    filename: str | None = None
    size: int | None = None
    thumbnail: str | None = None
    estimate: int | None = None
    """Seconds."""
    material: str | None = None
    layer_height: Number | None = None
    supplies_usage: Number | None = None
    """Planned filament, mm."""
    dimensions: Dimensions | None = None
    gcode_id: int | None = None
    """``None`` until the cloud has parsed the file."""
    is_temp_file: bool | None = None
    user_lock_space_id: int | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> CloudFile:
        size_x = as_number(data.get("size_x"))
        return cls(
            id=as_int(data.get("id")),
            name=as_str(data.get("old_filename")),
            filename=as_str(data.get("filename")),
            size=as_int(data.get("size")),
            thumbnail=as_text(data.get("thumbnail")),
            estimate=as_int(data.get("estimate")),
            material=as_text(data.get("material_name")),
            layer_height=as_number(data.get("layer_height")),
            supplies_usage=as_number(data.get("supplies_usage")),
            dimensions=Dimensions(
                size_x, as_number(data.get("size_y")), as_number(data.get("size_z"))
            )
            if size_x is not None
            else None,
            gcode_id=as_int(data.get("gcode_id")),
            is_temp_file=as_bool(data.get("is_temp_file")),
            user_lock_space_id=as_int(data.get("user_lock_space_id")),
            raw=data,
        )

    @property
    def size_mb(self) -> float | None:
        """Size ÷ 1 000 000."""
        return self.size / 1_000_000 if self.size is not None else None


@dataclass(frozen=True, slots=True)
class StorageQuota:
    """Cloud storage quota (E19; PROTOCOL B §4.3)."""

    used_bytes: int
    total_bytes: int
    used: str | None = None
    total: str | None = None
    user_file_exists: bool | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @property
    def available_bytes(self) -> int:
        return self.total_bytes - self.used_bytes


@dataclass(frozen=True, slots=True)
class PrinterFile:
    """One entry of a printer's local or USB file list (CMQTT ``file``)."""

    filename: str
    is_dir: bool
    size: int = 0
    timestamp: int = 0

    @property
    def size_mb(self) -> float:
        return self.size / 1_000_000


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class CameraCredentials:
    """Agora join credentials from the camera-open order (PROTOCOL D §1.4).

    Single use: never cache them. The ``repr`` shows no secret.
    """

    app_id: str
    channel: str
    rtc_token: str
    client_uid: int
    publisher_uid: int | None = None
    encryption_mode: str | None = None
    encryption_key: str | None = None
    encryption_kdf_salt: str | None = None
    event_id: str | None = None
    msgid: str | None = None
    raw: Mapping[str, Any] = field(default=_EMPTY, compare=False)

    def __repr__(self) -> str:
        return (
            f"CameraCredentials(client_uid={self.client_uid}, "
            f"publisher_uid={self.publisher_uid}, "
            f"encrypted={self.is_encrypted})"
        )

    @property
    def wire_encryption_mode(self) -> str | None:
        """``AES_256_GCM2`` → ``aes-256-gcm2``; ``None`` for an open channel."""
        mode = (self.encryption_mode or "").strip()
        if not mode or mode.lower() == "none":
            return None
        return mode.lower().replace("_", "-")

    @property
    def is_encrypted(self) -> bool:
        return self.wire_encryption_mode is not None

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> CameraCredentials | None:
        """``None`` when the ``shengwang`` block is missing or incomplete."""
        block = data.get("shengwang")
        if not isinstance(block, Mapping):
            return None
        app_id = as_text(block.get("appid"))
        channel = as_text(block.get("channel"))
        token = as_text(block.get("rtc_token"))
        client_uid = as_int(block.get("client_uid"))
        if app_id is None or channel is None or token is None or client_uid is None:
            return None
        device = as_map(data.get("shengwang_device"))
        publisher = as_int(device.get("uid"))
        return cls(
            app_id=app_id,
            channel=channel,
            rtc_token=token,
            client_uid=client_uid,
            publisher_uid=publisher
            if publisher is not None
            else as_int(block.get("uid")),
            encryption_mode=as_text(block.get("encryption_mode")),
            encryption_key=as_text(block.get("encryption_key")),
            encryption_kdf_salt=as_text(block.get("encryption_kdf_salt")),
            event_id=as_text(block.get("event_id")),
            msgid=as_text(data.get("msgid")),
            raw=data,
        )
