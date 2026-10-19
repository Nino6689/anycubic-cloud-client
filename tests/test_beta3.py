"""Beta 3 field reports (hass-anycubic-next ACCEPTANCE D10, D13-D15)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from anycubic_cloud_client import (
    CloudSecrets,
    CurrentFilamentUpdate,
    FilamentStep,
    PrintUpdate,
    parse_cloud_message,
)

from .payloads import MACHINE_TYPE, PRINTER_KEY, mqtt
from .test_mqtt import make_link, settle

if TYPE_CHECKING:
    from .conftest import FakeSession

PUBLIC = f"anycubic/anycubicCloud/v1/printer/public/{MACHINE_TYPE}/{PRINTER_KEY}"


def parse(message: dict[str, Any]) -> Any:
    result = parse_cloud_message(f"{PUBLIC}/{message['type']}/report", message)
    assert result is not None
    return result


# -- D10: filament-change steps of a multi-colour print --------------------------


@pytest.mark.parametrize(
    ("state", "data", "step", "channel", "unit"),
    [
        (
            "extfilbox_filament_feeding",
            {"channel_no": 2, "taskid": "-1"},
            FilamentStep.PRINTER_FEEDING,
            2,
            None,
        ),
        (
            "extfilbox_filament_unfeeding",
            {"channel_no": 1, "taskid": 9},
            FilamentStep.PRINTER_UNFEEDING,
            None,
            None,
        ),
        (
            "ace_filament_feeding",
            {"channel_no": 1, "taskid": 9, "unit_no": 1},
            FilamentStep.ACE_FEEDING,
            1,
            1,
        ),
        (
            "nozzle_purging",
            {"channel_no": 2, "taskid": 9},
            FilamentStep.NOZZLE_PURGING,
            None,
            None,
        ),
        (
            "ace_filament_feeding",
            {"channel_no": 0, "taskid": 9, "unit_no": "2"},
            FilamentStep.ACE_FEEDING,
            None,
            2,
        ),
    ],
)
def test_filament_steps(
    state: str,
    data: dict[str, Any],
    step: FilamentStep,
    channel: int | None,
    unit: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    message = parse(mqtt("print", "start", state, data))
    assert message.understood
    assert message.fault is None
    update = message.update
    assert isinstance(update, PrintUpdate)
    assert update.filament_step is step
    assert update.feeding_channel == channel
    assert update.unit_no == unit
    # Not a job state: no status, no job fields, no download figure.
    assert update.work_status is None
    assert update.job_status is None
    assert update.job is None
    assert update.download_progress is None
    assert not [r for r in caplog.records if r.levelno > logging.DEBUG]


def test_feeding_channel_of_an_updated_report() -> None:
    message = parse(mqtt("print", "update", "updated", {"channel_no": 4}))
    update = message.update
    assert isinstance(update, PrintUpdate)
    assert update.filament_step is None
    assert update.feeding_channel == 4
    assert FilamentStep.ACE_FEEDING.is_feeding
    assert not FilamentStep.NOZZLE_PURGING.is_feeding


# -- D15: job numbers as text -------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("-1", -1), ("131189709", 131189709)])
def test_task_id_as_text(raw: str, expected: int) -> None:
    update = parse(mqtt("print", "start", "printing", {"taskid": raw})).update
    assert isinstance(update, PrintUpdate)
    assert update.task_id == expected
    # -1 names no job: the report applies to the known one.
    assert update.applies_to(5) is (expected == -1)


# -- D13: the printer's own current_filament ----------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0-3", CurrentFilamentUpdate(0, 3)),
        ("1-2", CurrentFilamentUpdate(1, 2)),
        ("", CurrentFilamentUpdate()),
        ("0-", CurrentFilamentUpdate()),
        ("x-1", CurrentFilamentUpdate()),
        (3, CurrentFilamentUpdate()),
        (None, CurrentFilamentUpdate()),
    ],
)
def test_current_filament(
    value: object, expected: CurrentFilamentUpdate, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    message = parse(mqtt("extfilbox", "auto", "done", {"current_filament": value}))
    assert message.understood
    assert message.update == expected
    # The holder report is not touched by it.
    assert message.report is None
    assert not [r for r in caplog.records if r.levelno > logging.DEBUG]


def test_current_filament_needs_a_completed_report() -> None:
    message = parse(mqtt("extfilbox", "auto", "failed", {"current_filament": "0-3"}))
    assert not message.understood
    assert message.update is None


# -- D14: message logs carry no printer identifier ------------------------------------


async def test_message_log_has_no_identifiers(
    http: FakeSession, secrets: CloudSecrets, caplog: pytest.LogCaptureFixture
) -> None:
    h = make_link(http, secrets, debug_messages=True)
    h.link.subscribe_printer(PRINTER_KEY, MACHINE_TYPE)
    await h.link.connect()
    caplog.set_level(logging.DEBUG, logger="anycubic_cloud_client")
    buried = {
        "type": "buried",
        "action": "PrintStart",
        "state": "done",
        "code": 200,
        "msg": "done",
        "data": {
            "cn_code": "CN-SECRET-123",
            "sn": "SERIAL-SECRET-9",
            "mac": "a4:e8:8d:12:34:56",
            "ip": "192.168.77.12",
            "token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        },
    }
    h.paho.deliver(f"{PUBLIC}/buried/report", json.dumps(buried).encode())
    await settle()
    assert [m.kind for m in h.messages] == ["buried"]
    assert "buried/PrintStart/done" in caplog.text
    for secret in (
        PRINTER_KEY,
        "CN-SECRET-123",
        "SERIAL-SECRET-9",
        "a4:e8:8d:12:34:56",
        "192.168.77.12",
        "eyJhbGciOiJIUzI1NiJ9",
    ):
        assert secret not in caplog.text
