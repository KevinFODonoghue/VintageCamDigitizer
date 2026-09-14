"""
Runs MP4 exports (export.py) one after another, each in a child process.

Why a child process rather than a thread: an export decodes, deinterlaces and
compresses video as fast as the computer allows.  In a thread it would compete
with the capture and recording threads for Python's lock (only one thread runs
Python code at a time), which could delay frames.  A separate process runs at
below-normal priority, so the live picture and any recording always come first;
and if an export crashes, the app doesn't.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .. import export

log = logging.getLogger(__name__)

_PROGRESS = re.compile(r"^progress: ([\d.]+)%$")


class ExportQueue(QObject):
    status_changed = Signal(str, object, bool)
    """(text for the Recording panel, fraction done or None, whether an export is running)."""
    finished_one = Signal(object, bool, str)
    """(recording, succeeded, why it failed: empty when it worked or was cancelled)."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: deque[tuple[Path, str]] = deque()
        self._proc: QProcess | None = None
        self._current: Path | None = None
        self._fraction = 0.0
        self._buffer = ""
        self._error = ""
        self._tail: list[str] = []  # the child's last lines, for a failure it didn't explain
        self._cancelled = False

    @property
    def running(self) -> bool:
        return self._proc is not None

    def describe(self) -> str:
        """What's running, for a question like "stop it and quit?"."""
        if self._current is None:
            return "nothing"
        more = f", {len(self._queue)} more waiting" if self._queue else ""
        return f"{self._current.name}, {self._fraction:.0%} done{more}"

    def add(self, sources: list[Path], field_order: str) -> None:
        """Export these recordings after any already waiting.  ``field_order`` is the setting: auto, tff or bff."""
        for source in map(Path, sources):
            if source != self._current and all(source != waiting for waiting, _ in self._queue):
                self._queue.append((source, field_order))
        if self.running:
            self._report_running()
        else:
            self._start_next()

    def cancel_all(self, wait: bool = False) -> None:
        """Forget the waiting recordings and stop the running export; its unfinished file is deleted."""
        self._queue.clear()
        proc = self._proc
        if proc is None:
            return
        self._cancelled = True
        proc.kill()  # the child can't tidy up after this, so _finish does
        if wait:
            proc.waitForFinished(5000)

    # -- internals -------------------------------------------------------------------

    def _start_next(self) -> None:
        if not self._queue:
            return
        source, field_order = self._queue.popleft()
        self._current, self._fraction, self._buffer, self._error, self._tail = source, 0.0, "", "", []
        self._cancelled = False
        program, *args = export.child_command([source], field_order)
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(self._on_output)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        log.info("Making an MP4 viewing copy of %s…", source.name)
        self._report_running()
        proc.start(program, args)

    def _on_output(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._buffer += bytes(proc.readAllStandardOutput().data()).decode("utf-8", errors="replace")
        *lines, self._buffer = self._buffer.replace("\r", "").split("\n")
        for line in filter(None, (line.strip() for line in lines)):
            self._tail = (self._tail + [line])[-5:]
            match = _PROGRESS.match(line)
            if match:
                self._fraction = float(match.group(1)) / 100
                self._report_running()
            elif line.startswith("error: "):
                self._error = line.removeprefix("error: ")
            elif line.startswith("field order: "):
                log.info("MP4 field order: %s", line.removeprefix("field order: "))
            elif line.startswith("done: "):
                log.info("Viewing copy saved: %s", line.removeprefix("done: "))
            elif not line.startswith("exporting: "):
                log.debug("export: %s", line)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:  # no finished() follows this one
            self._error = f"the export process couldn't start ({self._proc.errorString() if self._proc else '?'})"
            self._finish(False)

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._on_output()  # whatever is still buffered
        self._finish(code == 0 and status == QProcess.ExitStatus.NormalExit and not self._cancelled)

    def _finish(self, ok: bool) -> None:
        source, proc = self._current, self._proc
        self._current, self._proc = None, None
        if proc is not None:
            proc.deleteLater()
        if source is None:
            return
        message = ""
        if not ok:
            export.partial_path(export.output_path(source)).unlink(missing_ok=True)
            if self._cancelled:
                log.info("Export of %s cancelled.", source.name)
            else:
                message = self._error or " / ".join(self._tail[-2:]) or "the export stopped unexpectedly"
        self.finished_one.emit(source, ok, message)
        if self._queue:
            self._start_next()
            return
        if ok:
            text = f"Saved {export.output_path(source).name}"
        elif self._cancelled:
            text = "Export cancelled."
        else:
            text = f"Export of {source.name} failed: {message}"
        self.status_changed.emit(text, None, False)

    def _report_running(self) -> None:
        if self._current is None:
            return
        more = f"  (+{len(self._queue)} waiting)" if self._queue else ""
        self.status_changed.emit(f"Exporting {self._current.name}…  {self._fraction:.0%}{more}", self._fraction, True)
