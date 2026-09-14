"""
The white meter panel: how white the picture is, as a live percentage and a
bar.  Point the camera at a white card and turn a pot: the number goes up as
the picture gets whiter (white_meter.py explains the number).

It only shows readings; the measuring happens on the analysis thread
(analysis.py).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from ..white_meter import WhiteReading
from .theme import WARN_AMBER
from .widgets import muted_label, value_label

#: With no reading for this long (no picture coming in), the panel goes back to "—" instead of showing an old number.
STALE_MS = 1500


class WhitePanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(muted_label(
            "Point the camera at a white card and turn a pot: the number goes up as the picture gets whiter. "
            "100% is pure white. Darker, or tinted (red, green and blue unequal), reads lower."
        ))

        self.percent_label = value_label()
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.percent_label.setStyleSheet("font-size: 40pt; font-weight: 600;")
        layout.addWidget(self.percent_label)

        self.bar = QProgressBar()
        self.bar.setObjectName("whiteBar")  # styled in theme.py
        self.bar.setRange(0, 1000)  # tenths of a percent, so the bar moves as finely as the number
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(26)
        layout.addWidget(self.bar)

        colour_row = QHBoxLayout()
        self.swatch = QLabel()
        self.swatch.setFixedSize(40, 24)
        self.swatch.setToolTip("The picture's average colour")
        self.colour_label = value_label()
        colour_row.addWidget(self.swatch)
        colour_row.addWidget(self.colour_label, 1)
        layout.addLayout(colour_row)

        self.signal_label = QLabel()
        self.signal_label.setWordWrap(True)
        self.signal_label.setStyleSheet(f"color: {WARN_AMBER}; font-weight: 600;")
        self.signal_label.hide()
        layout.addWidget(self.signal_label)
        layout.addStretch(1)

        self._stale = QTimer(self)
        self._stale.setSingleShot(True)
        self._stale.setInterval(STALE_MS)
        self._stale.timeout.connect(self.clear)
        self.clear()

    def show_reading(self, reading: WhiteReading) -> None:
        self.percent_label.setText(f"{reading.percent:.1f}%")
        self.bar.setValue(round(reading.percent * 10))
        r, g, b = (round(v) for v in reading.rgb)
        self.swatch.setStyleSheet(f"background-color: rgb({r}, {g}, {b}); border: 1px solid #555;")
        self.swatch.show()
        self.colour_label.setText(f"Average colour   R {r}   G {g}   B {b}")
        self._stale.start()

    def clear(self) -> None:
        """Nothing to show: no picture coming in (yet)."""
        self._stale.stop()
        self.percent_label.setText("—")
        self.bar.setValue(0)
        self.swatch.hide()
        self.colour_label.setText("Waiting for the picture…")

    def set_signal(self, locked: bool | None) -> None:
        """Warn when there's no picture: the number means nothing then."""
        self.signal_label.setText("No picture signal: the number means nothing until the camera's picture is back.")
        self.signal_label.setVisible(locked is False)
