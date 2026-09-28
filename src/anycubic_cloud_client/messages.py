"""Parsing of cloud MQTT messages (PROTOCOL C §3-§4).

Where Part C says a cloud body is the same as the LAN body (``tempature``,
``fan``, ``light``, ``peripherie``, ``axis``, ``multiColorBox``,
``aiSettings``, ``extfilbox``, ``info``), the report is parsed by
``anycubic_lan.parse_message`` and handed on as :attr:`CloudMessage.report`.
Only the cloud's differences are parsed here, into :attr:`CloudMessage.update`.

Nothing here raises on a bad field and nothing here does I/O: one bad value
never discards the rest of a message (BEHAVIOUR B3), and a ``data: null``
body is tolerated for every kind (PROTOCOL C §4.20).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from typing import Any, cast

from anycubic_lan import Job as LanJob
from anycubic_lan import Report, parse_message

from .models import (
    RGB,
    PrinterFile,
    SliceParam,
    as_bool,
    as_int,
    as_map,
    as_number,
    as_rgb,
    as_str,
    as_text,
)
from .mqtt_identity import TopicInfo, parse_topic, redact_topic
from .orders import FileSource

_LOGGER = logging.getLogger(__name__)

#: Envelope ``code`` values that mean "nothing wrong" (PROTOCOL C §3.2).
OK_CODES = frozenset({0, 200})
#: ``state`` words that mean "completed" (``success`` and ``done`` are equivalent).
COMPLETED = frozenset({"done", "success"})

#: Kinds whose body is the LAN body: parsed by ``anycubic_lan``.
LAN_KINDS = frozenset(
    {
        "tempature",
        "fan",
        "light",
        "peripherie",
        "axis",
        "multiColorBox",
        "aiSettings",
        "extfilbox",
        "info",
    }
)

EVENT_KINDS = frozenset({"event", "printerevent", "printer_event"})

ACE_ACTIONS = frozenset(
    {
        "getInfo",
        "setInfo",
        "refresh",
        "autoUpdateInfo",
        "autoUpdateDryStatus",
        "setDry",
        "feedFilament",
        "setAutoFeed",
    }
)


class WorkStatus(IntEnum):
    """Free/busy, the HTTP record's ``is_printing``."""

    FREE = 1
    BUSY = 2


class JobStatus(IntEnum):
    """Job status codes set from a ``print`` action/state pair (C §4.4)."""

    PRINTING = 1
    COMPLETE = 2
    CANCELLED = 3
    DOWNLOADING = 4
    CHECKING = 5
    PREHEATING = 6


@dataclass(frozen=True, slots=True)
class Fault:
    """A printer fault code from the envelope (``code`` not 0 or 200)."""

    code: int
    message: str | None = None


# --------------------------------------------------------------------------
# Cloud-specific updates
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OnlineUpdate:
    """``lastWill``/``onlineReport``: device status 1 online, 2 offline."""

    online: bool


@dataclass(frozen=True, slots=True)
class WorkStatusUpdate:
    """``status``/``workReport``: free or busy."""

    work_status: WorkStatus


@dataclass(frozen=True, slots=True)
class BindingUpdate:
    """``user``: the printer was bound to or unbound from the account."""

    bound: bool


@dataclass(frozen=True, slots=True)
class PrintUpdate:
    """A ``print`` report, interpreted by the cloud table (PROTOCOL C §4.4).

    ``None`` fields mean "unchanged". Job fields (:attr:`job`) apply only to
    the known job; use :meth:`applies_to`. :attr:`work_status` and
    :attr:`download_progress` (printer level) apply whatever the task.
    """

    action: str
    state: str
    work_status: WorkStatus | None = None
    job_status: JobStatus | None = None
    pause: int | None = None
    task_id: int | None = None
    """``data.taskid`` as a number; ``None`` when absent or not a number."""
    task_id_present: bool = False
    download_progress: int | None = None
    """Printer-level download %: set while downloading, 0 on every other status row."""
    job: LanJob | None = None
    """Job fields (progress, layers, times, file name, extruded mm)."""
    failure_reason: str | None = None
    nozzle_temp: float | int | None = None
    """Current temperatures from an ``updated`` body, only when both are present."""
    hotbed_temp: float | int | None = None
    fan_speed_pct: float | int | None = None
    print_speed_pct: float | int | None = None
    print_speed_mode: int | None = None
    target_nozzle_temp: float | int | None = None
    """Job targets from an ``updated`` body, only when both are present."""
    target_hotbed_temp: float | int | None = None
    slice_param: SliceParam | None = None

    def applies_to(self, job_id: int | None) -> bool:
        """Whether the job fields belong to the known job ``job_id``.

        A job must be known, and the task must be absent, not a number,
        negative, or equal to the job's id (PROTOCOL C §4.4).
        """
        if job_id is None:
            return False
        return self.task_id is None or self.task_id < 0 or self.task_id == job_id


@dataclass(frozen=True, slots=True)
class AxisMoveUpdate:
    """``axis``/``move``: ``doing`` moving, ``done`` finished, ``failed`` refused."""

    state: str

    @property
    def moving(self) -> bool:
        return self.state not in ("done", "failed")

    @property
    def refused(self) -> bool:
        return self.state == "failed"


@dataclass(frozen=True, slots=True)
class AceLoadedSlotUpdate:
    """``multiColorBox``/``autoUpdateInfo``: flat ``data.id``/``data.loaded_slot``."""

    box_id: int
    loaded_slot: int


@dataclass(frozen=True, slots=True)
class ExternalHolderUpdate:
    """``extfilbox``/``reportInfo``; the holder's ``id`` only comes from HTTP."""

    material: str | None = None
    color: RGB | None = None
    loaded: bool | None = None
    status_type: int | None = None
    current_status: int | None = None


@dataclass(frozen=True, slots=True)
class FileListUpdate:
    """``file``/``listLocal`` or ``listUdisk``.

    ``records`` is ``None`` when the reply carried no list (keep the previous
    one); an empty tuple is a real, empty list.
    """

    source: FileSource
    records: tuple[PrinterFile, ...] | None


@dataclass(frozen=True, slots=True)
class FileDeletedUpdate:
    """``file``/``deleteLocal`` or ``deleteUdisk``: acknowledged, no state."""

    source: FileSource


class FirmwareStep(StrEnum):
    VERSION = "version"
    START = "start"
    DOWNLOADING = "downloading"
    UPDATING = "updating"
    SUCCESS = "success"
    """ACE ``update-success`` / ``updateSuccessProcessed``: ignored."""


@dataclass(frozen=True, slots=True)
class FirmwareReport:
    """An ``ota`` message (PROTOCOL C §4.13, D §4.3)."""

    step: FirmwareStep
    is_ace: bool = False
    box_index: int = 0
    version: str | None = None
    download_progress: int | None = None
    install_progress: int | None = None


@dataclass(frozen=True, slots=True)
class PrinterEvent:
    """``event``, ``printerevent`` or ``printer_event`` (fault: the envelope code)."""

    data: Mapping[str, Any] = field(default_factory=dict, repr=False)


type CloudUpdate = (
    OnlineUpdate
    | WorkStatusUpdate
    | BindingUpdate
    | PrintUpdate
    | AxisMoveUpdate
    | AceLoadedSlotUpdate
    | ExternalHolderUpdate
    | FileListUpdate
    | FileDeletedUpdate
    | FirmwareReport
    | PrinterEvent
)


@dataclass(frozen=True, slots=True)
class CloudMessage:
    """One printer message from the cloud MQTT.

    :attr:`fault` is recorded even when the kind is not understood
    (BEHAVIOUR B35). :attr:`understood` is ``False`` for kind/action/state
    pairs Part C does not describe; they are logged at debug only (B36).
    """

    topic: str = field(repr=False)
    printer_key: str = field(repr=False)
    kind: str
    action: str | None
    state: str | None
    code: int | None = None
    msg: str | None = None
    msgid: str | None = None
    fault: Fault | None = None
    understood: bool = True
    report: Report | None = None
    """The ``anycubic_lan`` report, for the kinds whose body is the LAN body."""
    update: CloudUpdate | None = None
    """The cloud-specific interpretation, when there is one."""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def data(self) -> Any:
        return self.raw.get("data")

    @property
    def light_types(self) -> tuple[int, ...]:
        """The light types an understood ``light`` message reports (C §4.7).

        A query answer lists every light in ``data.lights``; a control answer
        or a pushed change carries one light as ``data``. Empty otherwise.
        """
        if self.kind != "light" or not self.understood:
            return ()
        body = as_map(self.data)
        lights = body.get("lights")
        items = lights if isinstance(lights, list) else [body]
        types = (as_int(as_map(item).get("type")) for item in items)
        return tuple(t for t in types if t is not None)


# --------------------------------------------------------------------------
# Decoding and routing
# --------------------------------------------------------------------------


def decode_payload(
    payload: bytes | str | Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """UTF-8 JSON object, or ``None`` when the payload is not one."""
    if isinstance(payload, Mapping):
        return payload
    try:
        decoded = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return cast("Mapping[str, Any]", decoded) if isinstance(decoded, Mapping) else None


def _fault(message: Mapping[str, Any]) -> Fault | None:
    code = message.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code not in OK_CODES:
        return Fault(code, as_str(message.get("msg")))
    return None


def parse_cloud_message(
    topic: str | TopicInfo, message: Mapping[str, Any]
) -> CloudMessage | None:
    """Parse one decoded printer message.

    Returns ``None`` for what the routing rule drops (PROTOCOL C §2.5): user
    topics, single-key messages on a ``response`` topic, and payloads without
    a ``type``.
    """
    info = topic if isinstance(topic, TopicInfo) else parse_topic(topic)
    topic_text = "/".join(info.segments)
    key = info.printer_key
    if info.is_user_topic or key is None:
        return None
    if info.is_response and len(message) == 1:
        return None
    kind = as_str(message.get("type"))
    if not kind:
        # Required by 2.x (an ERROR there); logged quietly here (Q15).
        _LOGGER.debug("Printer message without a type on %s", redact_topic(topic_text))
        return None
    action = as_str(message.get("action"))
    state = as_str(message.get("state"))
    base = CloudMessage(
        topic=topic_text,
        printer_key=key,
        kind=kind,
        action=action,
        state=state,
        code=as_int(message.get("code"))
        if not isinstance(message.get("code"), bool)
        else None,
        msg=as_str(message.get("msg")),
        msgid=as_str(message.get("msgid")),
        fault=_fault(message),
        raw=message,
    )
    data = message.get("data")
    try:
        return _dispatch(base, info, kind, action, state, data)
    except Exception:  # never let one message break the link
        _LOGGER.exception("Could not parse a %s message", kind)
        return _replace(base, understood=False)


def _replace(message: CloudMessage, **changes: Any) -> CloudMessage:
    return replace(message, **changes)


def _dispatch(
    base: CloudMessage,
    info: TopicInfo,
    kind: str,
    action: str | None,
    state: str | None,
    data: object,
) -> CloudMessage:
    body = as_map(data)
    match kind:
        case "lastWill":
            if action == "onlineReport" and state in ("online", "offline"):
                return _replace(base, update=OnlineUpdate(state == "online"))
        case "status":
            if action == "workReport" and state in ("free", "busy"):
                status = WorkStatus.FREE if state == "free" else WorkStatus.BUSY
                return _replace(base, update=WorkStatusUpdate(status))
        case "user":
            if state == "done" and action in ("bindQuery", "unbind"):
                return _replace(base, update=BindingUpdate(action == "bindQuery"))
        case "print":
            update = _print_update(action, state, base.msg, data)
            if update is not None:
                return _replace(base, update=update)
        case "file":
            return _file_message(base, action, state, body, data)
        case "ota":
            report = _firmware_report(info, action, state, body)
            if report is not None:
                return _replace(base, update=report)
        case _ if kind in EVENT_KINDS:
            return _replace(base, update=PrinterEvent(body))
        case _ if kind in LAN_KINDS:
            return _lan_message(base, kind, action, state, data)
    return _replace(base, understood=False)


def _lan_understood(kind: str, action: str | None, state: str | None) -> bool:
    match kind:
        case "tempature" | "fan" | "light":
            return state in COMPLETED
        case "peripherie":
            return action == "query" and state in COMPLETED
        case "axis":
            return (action == "query" and state in COMPLETED) or action == "move"
        case "multiColorBox":
            return action in ACE_ACTIONS and state in COMPLETED
        case "extfilbox":
            return action == "reportInfo" and state in COMPLETED
    return True  # aiSettings and info: any action and state


def _lan_message(
    base: CloudMessage, kind: str, action: str | None, state: str | None, data: object
) -> CloudMessage:
    if not _lan_understood(kind, action, state):
        return _replace(base, understood=False)
    message = dict(base.raw)
    update: CloudUpdate | None = None
    if kind == "multiColorBox":
        message["data"], update = _normalise_ace(action, data)
    elif kind == "axis" and action == "move":
        update = AxisMoveUpdate(state or "")
    elif kind == "extfilbox":
        body = as_map(data)
        update = ExternalHolderUpdate(
            material=as_text(body.get("type")),
            color=as_rgb(body.get("color")),
            loaded=as_bool(body.get("loaded")),
            status_type=as_int(body.get("status_type")),
            current_status=as_int(body.get("current_status")),
        )
    # The LAN envelope keeps the topic: hand it the redacted one.
    report = parse_message(message, redact_topic(base.topic))
    return _replace(base, report=report, update=update)


def _normalise_ace(action: str | None, data: object) -> tuple[Any, CloudUpdate | None]:
    """Bring cloud ACE bodies into the LAN shape the LAN parser reads.

    ``getInfo`` may carry a single box object instead of a list, and
    ``autoUpdateInfo`` carries a flat ``data.id`` / ``data.loaded_slot``.
    """
    body = as_map(data)
    boxes = body.get("multi_color_box")
    if isinstance(boxes, Mapping):
        return {**body, "multi_color_box": [boxes]}, None
    if action == "autoUpdateInfo" and boxes is None:
        box_id, loaded = as_int(body.get("id")), as_int(body.get("loaded_slot"))
        if box_id is not None and loaded is not None:
            normalised = {"multi_color_box": [{"id": box_id, "loaded_slot": loaded}]}
            return normalised, AceLoadedSlotUpdate(box_id, loaded)
    return data, None


# -- print (PROTOCOL C §4.4) ------------------------------------------------

_STATUS_ROWS: dict[tuple[str, str], tuple[WorkStatus, JobStatus, int | None]] = {
    ("start", "downloading"): (WorkStatus.BUSY, JobStatus.DOWNLOADING, None),
    ("start", "checking"): (WorkStatus.BUSY, JobStatus.CHECKING, None),
    ("start", "preheating"): (WorkStatus.BUSY, JobStatus.PREHEATING, None),
    ("start", "printing"): (WorkStatus.BUSY, JobStatus.PRINTING, None),
    ("start", "finished"): (WorkStatus.FREE, JobStatus.COMPLETE, None),
    ("pause", "pausing"): (WorkStatus.BUSY, JobStatus.PRINTING, 1),
    ("pause", "paused"): (WorkStatus.BUSY, JobStatus.PRINTING, 1),
    ("resume", "resuming"): (WorkStatus.BUSY, JobStatus.PRINTING, 1),
    ("resume", "resumed"): (WorkStatus.BUSY, JobStatus.PRINTING, 0),
    ("start", "stopping"): (WorkStatus.FREE, JobStatus.CANCELLED, None),
    ("start", "stoped"): (WorkStatus.FREE, JobStatus.CANCELLED, None),  # sic
    ("stop", "stopping"): (WorkStatus.FREE, JobStatus.CANCELLED, None),
    ("stop", "stoped"): (WorkStatus.FREE, JobStatus.CANCELLED, None),  # sic
    ("start", "failed"): (WorkStatus.FREE, JobStatus.CANCELLED, None),
    ("stop", "failed"): (WorkStatus.FREE, JobStatus.CANCELLED, None),
}


def _task_id(body: Mapping[str, Any]) -> tuple[int | None, bool]:
    if "taskid" not in body:
        return None, False
    return as_int(body.get("taskid")), True


def _print_update(
    action: str | None, state: str | None, msg: str | None, data: object
) -> PrintUpdate | None:
    if action is None or state is None:
        return None
    body = as_map(data)
    task_id, present = _task_id(body)
    row = _STATUS_ROWS.get((action, state))
    if row is not None:
        work, job_status, pause = row
        downloading = job_status is JobStatus.DOWNLOADING
        applies_fields = job_status not in (JobStatus.DOWNLOADING, JobStatus.CHECKING)
        return PrintUpdate(
            action=action,
            state=state,
            work_status=work,
            job_status=job_status,
            pause=pause,
            task_id=task_id,
            task_id_present=present,
            download_progress=as_int(body.get("progress")) if downloading else 0,
            job=LanJob.from_data(body) if applies_fields and body else None,
            failure_reason=msg if state == "failed" else None,
        )
    if state == "updated" and action in ("start", "update"):
        settings = as_map(body.get("settings"))
        nozzle, bed = (
            as_number(body.get("curr_nozzle_temp")),
            as_number(body.get("curr_hotbed_temp")),
        )
        t_nozzle = as_number(settings.get("target_nozzle_temp"))
        t_bed = as_number(settings.get("target_hotbed_temp"))
        both = nozzle is not None and bed is not None
        both_targets = t_nozzle is not None and t_bed is not None
        return PrintUpdate(
            action=action,
            state=state,
            task_id=task_id,
            task_id_present=present,
            nozzle_temp=nozzle if both else None,
            hotbed_temp=bed if both else None,
            fan_speed_pct=as_number(settings.get("fan_speed_pct")),
            print_speed_pct=as_number(settings.get("print_speed_pct")),
            print_speed_mode=as_int(settings.get("print_speed_mode")),
            target_nozzle_temp=t_nozzle if both_targets else None,
            target_hotbed_temp=t_bed if both_targets else None,
        )
    if action == "getSliceParam" and state == "done":
        return PrintUpdate(
            action=action,
            state=state,
            task_id=task_id,
            task_id_present=present,
            slice_param=SliceParam.from_value(body.get("slice_param")),
        )
    return None


# -- file (PROTOCOL C §4.12) ------------------------------------------------


def _file_records(value: object) -> tuple[PrinterFile, ...] | None:
    if not isinstance(value, list):
        return None
    records = []
    for entry in value:
        record = as_map(entry)
        name = as_str(record.get("filename"))
        is_dir = record.get("is_dir")
        if name is None or not isinstance(is_dir, bool):
            _LOGGER.debug("Skipping an unreadable file-list record")
            continue
        records.append(
            PrinterFile(
                filename=name,
                is_dir=is_dir,
                size=as_int(record.get("size")) or 0,
                timestamp=as_int(record.get("timestamp")) or 0,
            )
        )
    return tuple(records)


def _file_message(
    base: CloudMessage,
    action: str | None,
    state: str | None,
    body: Mapping[str, Any],
    data: object,
) -> CloudMessage:
    sources = {"Local": FileSource.LOCAL, "Udisk": FileSource.UDISK}
    for suffix, source in sources.items():
        if action == f"list{suffix}" and state in COMPLETED:
            records = _file_records(body.get("records"))
            return _replace(base, update=FileListUpdate(source, records))
        if action == f"delete{suffix}" and state in COMPLETED:
            return _replace(base, update=FileDeletedUpdate(source))
    if action == "cloudRecommendList":
        return base  # consumed and discarded
    return _replace(base, understood=False)


# -- ota (PROTOCOL C §4.13) --------------------------------------------------


def _firmware_report(
    info: TopicInfo, action: str | None, state: str | None, body: Mapping[str, Any]
) -> FirmwareReport | None:
    is_ace = info.is_ace
    box = info.ace_box_index if is_ace else 0
    if action == "reportVersion" and state in COMPLETED:
        return FirmwareReport(
            FirmwareStep.VERSION,
            is_ace,
            box,
            version=as_text(body.get("firmware_version")),
        )
    if action != "update":
        return None
    match state:
        case "start":
            return FirmwareReport(FirmwareStep.START, is_ace, box)
        case "downloading":
            return FirmwareReport(
                FirmwareStep.DOWNLOADING,
                is_ace,
                box,
                download_progress=as_int(body.get("progress")),
            )
        case "updating":
            return FirmwareReport(
                FirmwareStep.UPDATING,
                is_ace,
                box,
                install_progress=as_int(body.get("current_progress")),
            )
        case "update-success" | "updateSuccessProcessed" if is_ace:
            return FirmwareReport(FirmwareStep.SUCCESS, is_ace, box)
    return None
