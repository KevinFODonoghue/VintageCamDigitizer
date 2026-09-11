"""Small reusable widgets."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QSlider, QStyle, QStyleOptionSlider, QWidget


class NeutralSlider(QSlider):
    """A horizontal slider with an amber notch marking its neutral value.

    Used for the proc amp, where "neutral" (the driver's default) is the only
    correct setting during calibration, so it should be visible at a glance.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._neutral: int | None = None
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep single-key shortcuts working

    def set_neutral(self, value: int | None) -> None:
        self._neutral = value
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().paintEvent(event)
        if self._neutral is None or self.maximum() <= self.minimum():
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        style = self.style()
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self)
        span = groove.width() - handle.width()
        x = groove.x() + handle.width() / 2 + QStyle.sliderPositionFromValue(
            self.minimum(), self.maximum(), self._neutral, span
        )
        painter = QPainter(self)
        painter.setPen(QPen(QColor(240, 160, 32), 2))
        painter.drawLine(QPointF(x, 1), QPointF(x, 5))
        painter.drawLine(QPointF(x, self.height() - 6), QPointF(x, self.height() - 2))
        painter.end()


def muted_label(text: str = "", wrap: bool = True) -> QLabel:
    """Small grey explanatory text."""
    label = QLabel(text)
    label.setProperty("muted", True)
    label.setWordWrap(wrap)
    return label


def value_label(text: str = "") -> QLabel:
    """Monospaced label for numbers that change, so digits don't jiggle."""
    label = QLabel(text)
    label.setProperty("valueLabel", True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after changing a dynamic property used in it."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
