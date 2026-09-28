"""Firmware update progress (PROTOCOL C §4.13, D §4.3-§4.4, BEHAVIOUR §2.17)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .messages import FirmwareReport, FirmwareStep

if TYPE_CHECKING:
    from .models import FirmwareInfo

#: Without any ``ota`` message for this long an update stops counting as in
#: progress (Q7 in docs/QUESTIONS.md).
DEFAULT_TIMEOUT = 3600.0


@dataclass(slots=True)
class FirmwareProgress:
    """Tracks one firmware target (the printer, or one ACE).

    *In progress* starts with ``update``/``start`` (or any progress message)
    and ends when the installed version changes, from **either** source: an
    ``ota`` ``reportVersion`` or a cloud refresh (:meth:`apply_installed`).
    In 2.x only a ``reportVersion`` with a *different* version cleared it,
    so a refresh that saw the new version first left it stuck (D §4.4). It
    also ends after ``timeout`` seconds without any ``ota`` message.
    """

    installed_version: str | None = None
    updating: bool = False
    downloading: bool = False
    download_percent: int = 0
    install_percent: int = 0
    timeout: float | None = DEFAULT_TIMEOUT
    _version_at_start: str | None = field(default=None, repr=False)
    _last_activity: float | None = field(default=None, repr=False)

    def _clear(self) -> None:
        self.updating = False
        self.downloading = False
        self.download_percent = 0
        self.install_percent = 0
        self._version_at_start = None
        self._last_activity = None

    def _start(self, now: float) -> None:
        if not self.updating:
            self._version_at_start = self.installed_version
        self.updating = True
        self._last_activity = now

    def apply_installed(self, version: str | None) -> None:
        """A version from any source; a new one ends the update."""
        if version is None:
            return
        reference = self._version_at_start or self.installed_version
        if self.updating and reference is not None and version != reference:
            self._clear()
        self.installed_version = version

    def apply_cloud_record(self, info: FirmwareInfo | None) -> None:
        """A cloud refresh: take its installed version (clears a finished update)."""
        if info is not None:
            self.apply_installed(info.firmware_version)

    def apply_report(self, report: FirmwareReport, now: float | None = None) -> None:
        """Apply one ``ota`` message for this target."""
        now = time.monotonic() if now is None else now
        match report.step:
            case FirmwareStep.VERSION:
                version = report.version
                if version is not None and version != self.installed_version:
                    self._clear()
                self.apply_installed(version)
            case FirmwareStep.START:
                self._start(now)
            case FirmwareStep.DOWNLOADING:
                self._start(now)
                self.downloading = True
                if report.download_progress is not None:
                    self.download_percent = report.download_progress
            case FirmwareStep.UPDATING:
                self._start(now)
                self.downloading = False
                if report.install_progress is not None:
                    self.install_percent = report.install_progress
            case FirmwareStep.SUCCESS:
                pass  # ACE only; the new version arrives as reportVersion

    def expire(self, now: float | None = None) -> bool:
        """End a stalled update; returns ``True`` when it was ended."""
        if not self.updating or self.timeout is None or self._last_activity is None:
            return False
        now = time.monotonic() if now is None else now
        if now - self._last_activity > self.timeout:
            self._clear()
            return True
        return False

    @property
    def in_progress(self) -> bool:
        return self.updating or self.downloading

    @property
    def percent(self) -> int | None:
        """Combined progress, or ``None`` when no update is in progress.

        Download phase 0-50 (half the download %), install phase 50-100
        (50 + half the install %), and 1 when started without figures.
        """
        if not self.in_progress:
            return None
        if self.install_percent == 0 and self.download_percent > 0:
            return min(self.download_percent // 2, 100)
        if self.install_percent > 0:
            return min(self.install_percent // 2 + 50, 100)
        return 1
