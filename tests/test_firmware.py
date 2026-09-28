"""Firmware progress (PROTOCOL C §4.13, D §4.3-§4.4, BEHAVIOUR §2.17)."""

from __future__ import annotations

from anycubic_cloud_client import FirmwareProgress, FirmwareReport, FirmwareStep
from anycubic_cloud_client.models import FirmwareInfo


def report(step: FirmwareStep, **kw: object) -> FirmwareReport:
    return FirmwareReport(step, **kw)  # type: ignore[arg-type]


def test_progress_formula() -> None:
    progress = FirmwareProgress(installed_version="1.0")
    assert progress.percent is None and not progress.in_progress
    progress.apply_report(report(FirmwareStep.START), now=0)
    assert progress.in_progress and progress.percent == 1
    progress.apply_report(report(FirmwareStep.DOWNLOADING, download_progress=42), now=1)
    assert progress.downloading and progress.percent == 21
    progress.apply_report(report(FirmwareStep.DOWNLOADING), now=2)
    assert progress.download_percent == 42
    progress.apply_report(report(FirmwareStep.UPDATING, install_progress=30), now=3)
    assert not progress.downloading and progress.percent == 65
    progress.apply_report(report(FirmwareStep.UPDATING, install_progress=100), now=4)
    assert progress.percent == 100
    progress.apply_report(report(FirmwareStep.UPDATING), now=5)
    assert progress.install_percent == 100
    progress.apply_report(report(FirmwareStep.SUCCESS), now=6)
    assert progress.in_progress


def test_report_version_ends_the_update() -> None:
    progress = FirmwareProgress(installed_version="1.0")
    progress.apply_report(report(FirmwareStep.DOWNLOADING, download_progress=10), now=0)
    progress.apply_report(report(FirmwareStep.VERSION, version="1.0"), now=1)
    assert progress.in_progress  # same version: still updating
    progress.apply_report(report(FirmwareStep.VERSION, version="2.0"), now=2)
    assert not progress.in_progress and progress.percent is None
    assert progress.installed_version == "2.0"
    assert progress.download_percent == 0 and progress.install_percent == 0


def test_cloud_refresh_first_still_clears() -> None:
    """D §4.4: the refresh sees the new version before ``reportVersion``."""
    progress = FirmwareProgress()
    progress.apply_cloud_record(FirmwareInfo(firmware_version="1.0"))
    progress.apply_report(report(FirmwareStep.UPDATING, install_progress=50), now=0)
    progress.apply_cloud_record(FirmwareInfo(firmware_version="1.0"))
    assert progress.in_progress
    progress.apply_cloud_record(FirmwareInfo(firmware_version="2.0"))
    assert not progress.in_progress
    # the late reportVersion carries the version we already know
    progress.apply_report(report(FirmwareStep.VERSION, version="2.0"), now=1)
    assert not progress.in_progress
    progress.apply_cloud_record(None)
    progress.apply_installed(None)
    assert progress.installed_version == "2.0"


def test_first_version_seen_while_updating() -> None:
    progress = FirmwareProgress()
    progress.apply_report(report(FirmwareStep.START), now=0)
    progress.apply_installed("1.0")  # nothing known before: just record it
    assert progress.in_progress and progress.installed_version == "1.0"


def test_timeout() -> None:
    progress = FirmwareProgress(installed_version="1.0", timeout=60)
    assert not progress.expire(now=0)
    progress.apply_report(report(FirmwareStep.START), now=100)
    assert not progress.expire(now=150)
    assert progress.expire(now=161)
    assert not progress.in_progress
    never = FirmwareProgress(timeout=None)
    never.apply_report(report(FirmwareStep.START))
    assert not never.expire()
