"""Start-print payloads, the ACE slot mapping and G-code metadata (PROTOCOL D §2)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import GcodeMetadataError, SlotMappingError
from .models import AceUnit, PaintInfo, as_int, as_number, paint_color_of
from .orders import FileSource

#: Slots per ACE unit (PROTOCOL B §9).
SLOTS_PER_ACE = 4


@dataclass(frozen=True, slots=True)
class TaskSettings:
    """``task_settings`` of order 1; both default to 0 as in 2.x (D §2.2)."""

    ai_detect: int = 0
    camera_timelapse: int = 0

    def to_data(self) -> dict[str, int]:
        return {
            "ai_detect": int(self.ai_detect),
            "camera_timelapse": int(self.camera_timelapse),
        }


def _common(filetype: int, task_settings: TaskSettings | None) -> dict[str, Any]:
    return {
        "filetype": filetype,
        "file_key": "",
        "file_name": "",
        "task_settings": (task_settings or TaskSettings()).to_data(),
    }


def cloud_file_print_data(
    file_id: int,
    *,
    delete_after: bool = False,
    task_settings: TaskSettings | None = None,
) -> dict[str, Any]:
    """Order 1 ``data`` for a cloud file (``filetype`` 0; PROTOCOL D §2.3)."""
    data = _common(0, task_settings)
    data.update(
        {
            "file_id": int(file_id),
            "hollow_param": None,
            "is_delete_file": 1 if delete_after else 0,
            "matrix": "",
            "project_type": 1,
            "punching_param": None,
            "slice_param": None,
            "slice_size": None,
            "template_id": 0,
        }
    )
    return data


def printer_file_print_data(
    filename: str,
    source: FileSource = FileSource.LOCAL,
    *,
    folder: str = "",
    task_settings: TaskSettings | None = None,
) -> dict[str, Any]:
    """Order 1 ``data`` for a file on the printer or its USB stick (D §2.3).

    ``filepath`` is ``/`` followed by ``folder`` (``/`` for the top level).
    """
    data = _common(source.filetype, task_settings)
    data.update({"filename": filename, "filepath": "/" + folder.strip("/")})
    return data


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    """One entry of ``ams_info.ams_box_mapping`` (PROTOCOL D §2.4)."""

    ams_index: int
    paint_index: int
    material_type: str | None
    filament_used: float | int | None
    ams_color: tuple[int, int, int]
    paint_color: tuple[int, int, int]

    def to_data(self) -> dict[str, Any]:
        return {
            "ams_color": list(self.ams_color),
            "ams_index": self.ams_index,
            "filament_used": self.filament_used,
            "material_type": self.material_type,
            "paint_color": list(self.paint_color),
            "paint_index": self.paint_index,
        }


def validate_slots(slots: Sequence[int] | None, ace_units: Sequence[AceUnit]) -> None:
    """A printer with an ACE needs a slot list; one without must not get one."""
    if ace_units and not slots:
        raise SlotMappingError("this printer has an ACE: give one slot per colour")
    if not ace_units and slots:
        raise SlotMappingError("this printer has no ACE: do not give slots")


def build_slot_mapping(
    colors: Sequence[PaintInfo],
    slots: Sequence[int],
    ace_units: Sequence[AceUnit],
) -> list[SlotAssignment]:
    """Pair the file's colours with ACE slots (PROTOCOL D §2.4).

    ``slots`` are 0-based **global** slot numbers (0-3 first ACE, 4-7 second),
    one per colour, in the file's colour order. Each unit owns the slots
    ``4 x position`` to ``4 x position + 3``: the unit's place in the list, not
    its reported ``id``, because a single ACE has been captured reporting
    ``id`` 1 (INTEGRATION-SPEC §11, Q2 in docs/QUESTIONS.md).

    ``ams_color`` is the slot's colour; ``paint_color`` is the file's own
    colour when the colour list carries one (Q3), else the slot's.
    """
    if len(slots) != len(colors):
        raise SlotMappingError(
            f"{len(colors)} colours in the file but {len(slots)} slots given"
        )
    if any(slot < 0 for slot in slots):
        raise SlotMappingError("slot numbers start at 0")
    if slots and max(slots) // SLOTS_PER_ACE + 1 > len(ace_units):
        raise SlotMappingError("not enough ACE units for the slots given")
    mapping: list[SlotAssignment] = []
    for unit in ace_units:
        for color, slot in zip(colors, slots, strict=True):
            in_box = slot - SLOTS_PER_ACE * unit.position
            if not 0 <= in_box < SLOTS_PER_ACE:
                continue
            ace_slot = unit.slot(in_box)
            slot_color = ace_slot.color if ace_slot is not None else None
            if slot_color is None:
                raise SlotMappingError(f"ACE slot {slot + 1} has no known colour")
            mapping.append(
                SlotAssignment(
                    ams_index=slot,
                    paint_index=color.paint_index,
                    material_type=color.material_type,
                    filament_used=color.filament_used,
                    ams_color=slot_color,
                    paint_color=color.paint_color or slot_color,
                )
            )
    mapping.sort(key=lambda entry: entry.paint_index)
    return mapping


def ams_info(mapping: Sequence[SlotAssignment]) -> dict[str, Any] | None:
    """``ams_info`` for order 1; an empty mapping is sent as ``null``."""
    if not mapping:
        return None
    return {
        "ams_box_mapping": [entry.to_data() for entry in mapping],
        "use_ams": True,
    }


# --------------------------------------------------------------------------
# G-code header (PROTOCOL D §2.6)
# --------------------------------------------------------------------------

_HEADER_LINE = re.compile(r"^;\s*([A-Za-z0-9_\[\]() ]+?)\s*=\s*(.*)$")
_FIRST_LINE = "; filament used"


def _normalise_key(key: str) -> str:
    for char in " []()":
        key = key.replace(char, "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.removesuffix("_")


def _parse_scalar(text: str) -> Any:
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        pass
    if "," in text:
        return [_parse_scalar(item.strip()) for item in text.split(",")]
    return _parse_scalar(text)


def parse_gcode_header(content: bytes | str) -> dict[str, Any]:
    """Collect ``; <key> = <value>`` lines from the first ``; filament used`` on.

    Keys are normalised (``; filament used [g]`` → ``filament_used_g``); the
    values ``begin`` and ``end`` are skipped.
    """
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError as err:
            raise GcodeMetadataError("the G-code file is not UTF-8 text") from err
    header: dict[str, Any] = {}
    started = False
    for line in content.splitlines():
        if not started:
            if not line.startswith(_FIRST_LINE):
                continue
            started = True
        match = _HEADER_LINE.match(line.strip())
        if match is None:
            continue
        value = match.group(2).strip()
        if value in ("begin", "end"):
            continue
        header[_normalise_key(match.group(1).strip())] = _parse_value(value)
    return header


@dataclass(frozen=True, slots=True)
class GcodeColor:
    """One colour of a G-code file, with the per-filament figures."""

    paint: PaintInfo
    filament_used_mm: float | int | None = None
    filament_used_cm3: float | int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


def _per_filament(values: object, index: int) -> float | int | None:
    if isinstance(values, list):
        return as_number(values[index]) if 0 <= index < len(values) else None
    return as_number(values) if index == 0 else None


def gcode_colors(header: Mapping[str, Any]) -> list[GcodeColor]:
    """The colour list of an uploaded file from its header (PROTOCOL D §2.6).

    Each colour's grams are looked up by its ``paint_index``, not its
    position.
    """
    paint_info = header.get("paint_info")
    if not isinstance(paint_info, list) or not paint_info:
        raise GcodeMetadataError("empty paint info")
    grams = header.get("filament_used_g")
    if grams in (None, "", []):
        raise GcodeMetadataError("the header has no 'filament used [g]'")
    gram_list = grams if isinstance(grams, list) else [grams]
    if len(gram_list) < len(paint_info):
        raise GcodeMetadataError("'filament used [g]' is shorter than the colour list")
    colors: list[GcodeColor] = []
    for entry in paint_info:
        if not isinstance(entry, Mapping):
            raise GcodeMetadataError("a paint info entry is not an object")
        index = as_int(entry.get("paint_index"))
        if index is None or not 0 <= index < len(gram_list):
            raise GcodeMetadataError("a paint info entry has no usable paint_index")
        data = dict(entry)
        data["filament_used"] = as_number(gram_list[index])
        paint = PaintInfo(
            paint_index=index,
            material_type=data.get("material_type")
            if isinstance(data.get("material_type"), str)
            else None,
            filament_used=data["filament_used"],
            paint_color=paint_color_of(data),
            raw=data,
        )
        colors.append(
            GcodeColor(
                paint=paint,
                filament_used_mm=_per_filament(header.get("filament_used_mm"), index),
                filament_used_cm3=_per_filament(header.get("filament_used_cm3"), index),
                raw=data,
            )
        )
    return colors
