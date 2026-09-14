"""
The pot meter panel.  For any pot: turn it fully clockwise and press Measure CW,
fully anticlockwise and press Measure CCW, then turn it until the light is green.
Optionally, zero it with a phone photo of the card first (see pot_meter.py).

It only shows the verdict and reports clicks (Qt signals); the measuring happens
on the analysis thread (analysis.py, pot_meter.py).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..analysis import PotStatus
from .theme import ERROR_RED, OK_GREEN, WARN_AMBER
from .widgets import muted_label

_LIGHT_COLOURS = {"green": OK_GREEN, "red": ERROR_RED, "grey": "#4a4a4a"}


def load_photo(path: str | Path, longest: int = 1280) -> np.ndarray:
    """Read a photo as an (height, width, 3) RGB array, the right way up.

    Phones store which way up they were held; Qt applies that.  Big photos are
    decoded straight to about ``longest`` pixels across (plenty for a 20 × 15
    grid, and much quicker).  Raises ValueError with a reason to show.
    """
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > longest:
        scale = longest / max(size.width(), size.height())
        reader.setScaledSize(QSize(round(size.width() * scale), round(size.height() * scale)))
    image = reader.read()
    if image.isNull():
        raise ValueError(f"it couldn't be read ({reader.errorString()}). Save it as a JPEG or PNG and try again")
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = image.width(), image.height()
    rows = np.frombuffer(image.constBits(), np.uint8, count=image.sizeInBytes()).reshape(height, image.bytesPerLine())
    return rows[:, :width * 3].reshape(height, width, 3).copy()


class PotPanel(QWidget):
    measure_clicked = Signal(str)  # "cw" or "ccw"
    reset_clicked = Signal()
    grid_toggled = Signal(bool)
    photo_load_clicked = Signal()
    photo_clear_clicked = Signal()

    def __init__(self, show_grid: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(muted_label(
            "Point the camera at an evenly lit white card that fills the frame. For the pot you're adjusting: "
            "turn it fully clockwise and press Measure CW, then fully anticlockwise and press Measure CCW. "
            "Then turn it until the light goes green."
        ))

        buttons = QHBoxLayout()
        self.cw_button = QPushButton("Measure CW")
        self.ccw_button = QPushButton("Measure CCW")
        self.reset_button = QPushButton("New pot")
        self.reset_button.setToolTip("Forget both ends, to start on another pot")
        self.cw_button.clicked.connect(lambda: self.measure_clicked.emit("cw"))
        self.ccw_button.clicked.connect(lambda: self.measure_clicked.emit("ccw"))
        self.reset_button.clicked.connect(self.reset_clicked)
        for button in (self.cw_button, self.ccw_button, self.reset_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.light = QLabel()
        self.light.setFixedSize(120, 120)
        light_row = QHBoxLayout()
        light_row.addStretch(1)
        light_row.addWidget(self.light)
        light_row.addStretch(1)
        layout.addLayout(light_row)
        self.light_text = QLabel()
        self.light_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.light_text.setWordWrap(True)
        self.light_text.setStyleSheet("font-size: 13pt; font-weight: 600;")
        layout.addWidget(self.light_text)

        self.note_label = muted_label()
        self.note_label.hide()
        self.signal_label = QLabel()
        self.signal_label.setWordWrap(True)
        self.signal_label.setStyleSheet(f"color: {WARN_AMBER}; font-weight: 600;")
        self.signal_label.hide()
        layout.addWidget(self.note_label)
        layout.addWidget(self.signal_label)

        target = QGroupBox("Aiming for")
        target_col = QVBoxLayout(target)
        target_row = QHBoxLayout()
        self.reference_thumb = QLabel()
        self.reference_thumb.setFixedSize(120, 88)
        self.reference_thumb.setToolTip("The colour each grid cell aims for, from your photo")
        self.reference_thumb.hide()
        self.reference_label = QLabel()
        self.reference_label.setWordWrap(True)
        target_row.addWidget(self.reference_thumb)
        target_row.addWidget(self.reference_label, 1)
        target_col.addLayout(target_row)
        photo_buttons = QHBoxLayout()
        self.photo_button = QPushButton("Load phone photo…")
        self.photo_button.setToolTip(
            "Zero the meter on how the card really looks under your light. Photograph the white card with "
            "your phone from where the camera sits, in landscape, framed like the camera's picture."
        )
        self.white_button = QPushButton("Use plain white")
        self.photo_button.clicked.connect(self.photo_load_clicked)
        self.white_button.clicked.connect(self.photo_clear_clicked)
        for button in (self.photo_button, self.white_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            photo_buttons.addWidget(button)
        photo_buttons.addStretch(1)
        target_col.addLayout(photo_buttons)
        layout.addWidget(target)

        self.grid_check = QCheckBox("Show the measuring grid on the picture")
        self.grid_check.setChecked(show_grid)
        self.grid_check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.grid_check.toggled.connect(self.grid_toggled)
        layout.addWidget(self.grid_check)
        layout.addStretch(1)

        self.light_state = ""
        """"green", "red" or "grey": what the light shows."""
        self._set_light("grey", "Measure both ends first")
        self.show_reference(None, None)

    # -- updates from the main window ------------------------------------------------

    def set_signal(self, locked: bool | None) -> None:
        """Warn when there's no picture: the meter means nothing then."""
        self.signal_label.setText("No picture signal: the meter means nothing until the camera's picture is back.")
        self.signal_label.setVisible(locked is False)

    def show_reference(self, name: str | None, grid: np.ndarray | None) -> None:
        """Show what the meter aims for: plain white, or a photo (``grid`` from pot_meter.reference_from_rgb)."""
        if name is None or grid is None:
            self.reference_label.setText("Plain white, at the card's own brightness.")
            self.reference_thumb.hide()
            self.white_button.setEnabled(False)
            return
        rgb = np.ascontiguousarray(np.clip(np.rint(grid), 0, 255).astype(np.uint8))
        rows, columns = rgb.shape[:2]
        image = QImage(rgb.data, columns, rows, columns * 3, QImage.Format.Format_RGB888).copy()
        self.reference_thumb.setPixmap(QPixmap.fromImage(image).scaled(
            self.reference_thumb.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation))
        self.reference_thumb.show()
        self.reference_label.setText(f"Your photo: {name}")
        self.white_button.setEnabled(True)

    def show_status(self, status: PotStatus) -> None:
        self.cw_button.setText("Measure CW  ✓" if status.has_cw else "Measure CW")
        self.ccw_button.setText("Measure CCW  ✓" if status.has_ccw else "Measure CCW")
        busy = bool(status.measuring)
        for button in (self.cw_button, self.ccw_button, self.reset_button):
            button.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.progress.setValue(round(status.progress * 100))
        if busy:
            end = "clockwise" if status.measuring == "cw" else "anticlockwise"
            self._set_light("grey", f"Measuring fully {end}: hold still…")
        elif status.at_best is True:
            self._set_light("green", "Best position")
        elif status.at_best is False:
            self._set_light("red", "Not there yet: keep turning")
        elif status.has_cw and status.has_ccw:
            self._set_light("grey", "This pot can't be judged")
        else:
            self._set_light("grey", "Measure both ends first")
        self.note_label.setText(status.note)
        self.note_label.setVisible(bool(status.note))

    # -- internals ---------------------------------------------------------------------

    def _set_light(self, state: str, text: str) -> None:
        self.light_state = state
        self.light.setStyleSheet(
            f"background-color: {_LIGHT_COLOURS[state]}; border-radius: 60px; border: 2px solid #222;"
        )
        self.light_text.setText(text)
