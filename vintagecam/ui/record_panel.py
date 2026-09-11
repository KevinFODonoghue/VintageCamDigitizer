"""
The Recording panel: the big record button, where files go, and live numbers
(elapsed time, file size, data rate, free disk space, dropped frames).
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..units import format_bytes, format_duration, format_time_left, gb_per_hour
from .theme import ERROR_RED
from .widgets import muted_label, repolish, value_label


class RecordPanel(QWidget):
    record_clicked = Signal()
    output_dir_changed = Signal(str)
    prefix_changed = Signal(str)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.record_button = QPushButton()
        self.record_button.setObjectName("recordButton")
        self.record_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.record_button.clicked.connect(self.record_clicked)
        layout.addWidget(self.record_button)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)

        stats = QGroupBox("This recording")
        form = QFormLayout(stats)
        self.file_value = value_label("—")
        self.file_value.setWordWrap(True)
        self.elapsed_value = value_label("—")
        self.size_value = value_label("—")
        self.rate_value = value_label("—")
        self.dropped_value = value_label("—")
        self.dropped_value.setToolTip(
            "Frames lost because the disk couldn't keep up (should always be 0),\n"
            "and frames the capture device itself skipped (kept as gaps in the timeline)."
        )
        form.addRow("File", self.file_value)
        form.addRow("Elapsed", self.elapsed_value)
        form.addRow("Size", self.size_value)
        form.addRow("Data rate", self.rate_value)
        form.addRow("Dropped", self.dropped_value)
        # Audio level: the loudest sample of the last half second, -60 dB (left) to 0 dB (clipping).
        self.audio_meter = QProgressBar()
        self.audio_meter.setRange(-60, 0)
        self.audio_meter.setValue(-60)
        self.audio_meter.setTextVisible(False)
        self.audio_meter.setFixedHeight(10)
        self.audio_meter.setToolTip("Peak audio level. Keep loud passages below about -6 dB; 0 dB is clipping.")
        self.audio_value = value_label("—")
        self.audio_value.setMinimumWidth(70)
        audio_row = QHBoxLayout()
        audio_row.addWidget(self.audio_meter, 1)
        audio_row.addWidget(self.audio_value)
        form.addRow("Audio", audio_row)
        layout.addWidget(stats)

        disk = QGroupBox("Output")
        dform = QFormLayout(disk)
        self.folder_edit = QLineEdit(settings.output_dir)
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        browse = QPushButton("Browse…")
        browse.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        browse.clicked.connect(self._browse)
        open_btn = QPushButton("Open")
        open_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        open_btn.clicked.connect(self._open_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(open_btn)
        self.browse_button = browse
        self.prefix_edit = QLineEdit(settings.filename_prefix)
        self.prefix_edit.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.prefix_edit.setToolTip("Files are named <prefix>_YYYYMMDD_HHMMSS.mkv")
        self.prefix_edit.editingFinished.connect(lambda: self.prefix_changed.emit(self.prefix_edit.text().strip()))
        self.free_value = value_label("—")
        self.left_value = value_label("—")
        self.left_value.setToolTip("Recording time until the disk is full, at the current data rate")
        dform.addRow("Folder", folder_row)
        dform.addRow("File prefix", self.prefix_edit)
        dform.addRow("Free space", self.free_value)
        dform.addRow("Time left", self.left_value)
        layout.addWidget(disk)

        layout.addWidget(muted_label(
            "Video: FFV1 version 3 lossless (16 slices, per-slice CRC, every frame a keyframe), "
            "4:2:2, in Matroska (.mkv). Audio: uncompressed 16-bit PCM, 48 kHz, stereo or mono (Device panel)."
            "Plays in VLC. About 30–45 GB per hour."
        ))
        layout.addStretch(1)
        self.set_recording(False)

    # -- updates from the main window ------------------------------------------------

    def set_recording(self, on: bool, path: Path | None = None, finishing: bool = False) -> None:
        if finishing:
            self.record_button.setText("Finishing file…")
            self.record_button.setEnabled(False)
        else:
            self.record_button.setEnabled(True)
            self.record_button.setText("■  Stop recording   (R)" if on else "●  Record   (R)")
        self.record_button.setProperty("recording", on and not finishing)
        repolish(self.record_button)
        self.browse_button.setEnabled(not on)
        self.prefix_edit.setEnabled(not on)
        if on and path is not None:
            self.file_value.setText(path.name)
            self.file_value.setToolTip(str(path))
            for label in (self.elapsed_value, self.size_value, self.rate_value, self.dropped_value):
                label.setText("—")
        if not on:
            self.update_audio(None, "—")

    def update_audio(self, level_dbfs: float | None, text: str) -> None:
        """Show the audio level (None = no audio) and a short status text."""
        if level_dbfs is None or math.isinf(level_dbfs):
            self.audio_meter.setValue(-60)
        else:
            self.audio_meter.setValue(int(max(-60.0, min(0.0, level_dbfs))))
        self.audio_value.setText(text)

    def update_recording(self, elapsed: float, size: int, bytes_per_second: float | None,
                         dropped: int, device_gaps: int) -> None:
        self.elapsed_value.setText(format_duration(elapsed))
        self.size_value.setText(format_bytes(size))
        self.rate_value.setText(gb_per_hour(bytes_per_second))
        text = f"{dropped} lost" + (f" · {device_gaps} skipped by device" if device_gaps else "")
        self.dropped_value.setText(text)
        self.dropped_value.setStyleSheet(f"color: {ERROR_RED}; font-weight: 700;" if dropped else "")

    def update_disk(self, free_bytes: int | None, seconds_left: float | None) -> None:
        self.free_value.setText(format_bytes(free_bytes))
        self.left_value.setText(format_time_left(seconds_left))

    def show_banner(self, text: str | None, level: str = "warn") -> None:
        if not text:
            self.banner.hide()
            return
        self.banner.setObjectName("errorBanner" if level == "error" else "warningBanner")
        repolish(self.banner)
        self.banner.setText(text)
        self.banner.show()

    def set_folder(self, folder: str) -> None:
        self.folder_edit.setText(folder)

    # -- internals ---------------------------------------------------------------------

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose where recordings are saved", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)
            self.output_dir_changed.emit(folder)

    def _open_folder(self) -> None:
        folder = Path(self.folder_edit.text())
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
