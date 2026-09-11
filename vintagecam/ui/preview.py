"""
The live picture, with calibration overlays and on-screen info drawn on top.

This widget is deliberately simple: it's handed a ready-to-draw image and paints
it.  Colour conversion happens in ``render.py``; deciding *which* frame to show
(and when) happens in ``main_window.py``.

**Coordinates.**  Overlays are defined in *image* pixels (0–720 across, 0–480
down) and then scaled with the picture, so they always land on the same video
pixels.  The centre of a 720×480 frame is the point (360.0, 240.0): the corner
shared by pixels 359/360 and rows 239/240.  With an even number of pixels
there's no single middle pixel, so every centred overlay is drawn symmetric
about that point — the same convention as the old ffplay ``drawbox`` overlay.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..color import luma_to_display, staircase_levels

GRID_COLOR = QColor(255, 255, 255, 90)  # white at 35%, as in the old ffplay drawgrid
CROSS_LINE_COLOR = QColor(255, 220, 0, 140)  # thin full-length lines
CROSS_BOLD_COLOR = QColor(255, 45, 45)  # bold centre cross
ACTION_SAFE_COLOR = QColor(90, 220, 130, 220)
TITLE_SAFE_COLOR = QColor(255, 170, 60, 220)
HUD_BG = QColor(0, 0, 0, 165)
HUD_FG = QColor(225, 225, 225)
REC_BG = QColor(183, 28, 28, 230)
WARN_BG = QColor(90, 62, 0, 225)
WARN_FG = QColor(255, 214, 130)
ERROR_BG = QColor(80, 16, 16, 230)


@dataclass
class Overlays:
    grid: bool = False
    crosshair: bool = False
    safe_areas: bool = False
    staircase: bool = False


class PreviewWidget(QWidget):
    """Paints the newest frame, overlays and the HUD.  GUI thread only."""

    double_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)  # we paint every pixel ourselves
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.overlays = Overlays()
        self.hud_enabled = True
        self.hud_lines: list[str] = []
        self.rec_text: str | None = None
        self.frozen = False
        self.banner: tuple[str, str] | None = None  # (text, "warn" | "error")
        self.center_message: str | None = None

        self.lag_ms: float | None = None
        """Smoothed time from a frame arriving off the card to it being painted."""

        self._image: QImage | None = None
        self._pixels: np.ndarray | None = None  # owns the memory the QImage points at
        self._arrival: float | None = None
        self._frame_size = QSize(720, 480)
        self._pixel_aspect = 10 / 11
        self._fit = True
        self._correct_aspect = True
        self._update_minimum_size()

    # -- configuration -------------------------------------------------------

    def set_frame_geometry(self, width: int, height: int, pixel_aspect: float) -> None:
        self._frame_size = QSize(width, height)
        self._pixel_aspect = pixel_aspect
        self._update_minimum_size()
        self.update()

    def set_fit(self, fit: bool) -> None:
        """True: scale to fit the window.  False: one video pixel per screen pixel."""
        self._fit = fit
        self._update_minimum_size()
        self.update()

    def set_correct_aspect(self, on: bool) -> None:
        """In fit mode, stretch to true 4:3 geometry (non-square pixels)."""
        self._correct_aspect = on
        self.update()

    def set_image(self, pixels: np.ndarray, arrival_time: float | None = None) -> None:
        """Show a (height, width, 4) BGRA uint8 image.  Keeps a reference to it.

        ``arrival_time`` (``time.perf_counter()`` when the frame came off the
        card) lets the widget measure its own lag; see ``lag_ms``.
        """
        if not pixels.flags.c_contiguous:
            pixels = np.ascontiguousarray(pixels)
        height, width = pixels.shape[:2]
        self._arrival = arrival_time
        self._pixels = pixels
        self._image = QImage(pixels.data, width, height, pixels.strides[0], QImage.Format.Format_RGB32)
        if (width, height) != (self._frame_size.width(), self._frame_size.height()):
            self._frame_size = QSize(width, height)
            self._update_minimum_size()
        self.update()

    def clear_image(self) -> None:
        self._image = None
        self._pixels = None
        self.update()

    def has_image(self) -> bool:
        return self._image is not None

    # -- geometry --------------------------------------------------------------

    def _update_minimum_size(self) -> None:
        if self._fit:
            self.setMinimumSize(320, 220)
        else:
            dpr = self.devicePixelRatioF() or 1.0
            self.setMinimumSize(
                math.ceil(self._frame_size.width() / dpr), math.ceil(self._frame_size.height() / dpr)
            )
        self.updateGeometry()

    def image_rect(self) -> QRectF:
        """Where the picture is drawn, in widget coordinates."""
        iw, ih = self._frame_size.width(), self._frame_size.height()
        dpr = self.devicePixelRatioF() or 1.0
        if self._fit:
            shown_w = iw * (self._pixel_aspect if self._correct_aspect else 1.0)
            scale = min(self.width() / shown_w, self.height() / ih)
            w, h = shown_w * scale, ih * scale
        else:
            w, h = iw / dpr, ih / dpr  # 1:1 in *physical* pixels, whatever Windows scaling is set to
        # Snap the corner to a physical pixel so 1:1 mode is truly sharp.
        x = round((self.width() - w) / 2 * dpr) / dpr
        y = round((self.height() - h) / 2 * dpr) / dpr
        return QRectF(x, y, w, h)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._frame_size.width(), self._frame_size.height())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.double_clicked.emit()

    # -- painting ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        target = self.image_rect()
        if self._image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._fit)
            p.drawImage(target, self._image)
            if self._arrival is not None:  # measure each new image once, not every repaint
                lag = (time.perf_counter() - self._arrival) * 1000
                self.lag_ms = lag if self.lag_ms is None else 0.9 * self.lag_ms + 0.1 * lag
                self._arrival = None
        self._draw_overlays(p, target)
        if self.hud_enabled:
            self._draw_hud(p, target)
        elif self.center_message:
            self._draw_center_message(p, target)
        p.end()

    def _draw_overlays(self, p: QPainter, target: QRectF) -> None:
        ov = self.overlays
        if not (ov.grid or ov.crosshair or ov.safe_areas or ov.staircase):
            return
        iw, ih = float(self._frame_size.width()), float(self._frame_size.height())
        sx, sy = target.width() / iw, target.height() / ih
        labels: list[tuple[QPointF, str, QColor]] = []

        p.save()
        p.translate(target.topLeft())
        p.scale(sx, sy)

        if ov.grid:
            # 10 × 10 cells: every 72 px across and 48 px down on a 720×480 frame.
            pen = QPen(GRID_COLOR)
            pen.setCosmetic(True)  # 1 screen pixel wide at any zoom
            p.setPen(pen)
            for k in range(1, 10):
                x, y = iw * k / 10, ih * k / 10
                p.drawLine(QPointF(x, 0), QPointF(x, ih))
                p.drawLine(QPointF(0, y), QPointF(iw, y))

        if ov.safe_areas:
            # Old CRTs hid the picture's edges under the bezel, by varying amounts.
            # Action safe (90%): where anything important must be.  Title safe
            # (80%): where text must be.  A centred camera test chart's border
            # should sit just outside action safe.
            for frac, color, name in ((0.90, ACTION_SAFE_COLOR, "ACTION SAFE 90%"),
                                      (0.80, TITLE_SAFE_COLOR, "TITLE SAFE 80%")):
                pen = QPen(color)
                pen.setCosmetic(True)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                mx, my = iw * (1 - frac) / 2, ih * (1 - frac) / 2
                p.drawRect(QRectF(mx, my, iw * frac, ih * frac))
                # Label in the top-right inside corner (top-left is where the info box sits).
                labels.append((p.transform().map(QPointF(mx + iw * frac, my)), name, color))

        if ov.crosshair:
            cx, cy = iw / 2, ih / 2
            # Thin full-width / full-height lines, 2 video pixels wide, straddling the centre.
            p.fillRect(QRectF(0, cy - 1, iw, 2), CROSS_LINE_COLOR)
            p.fillRect(QRectF(cx - 1, 0, 2, ih), CROSS_LINE_COLOR)
            # Bold short cross, 4 px thick, with a small gap so the chart's own
            # centre mark (and the thin lines crossing at the exact centre) stay visible.
            arm, gap, t = 24.0, 4.0, 4.0
            for rect in (
                QRectF(cx - arm, cy - t / 2, arm - gap, t),
                QRectF(cx + gap, cy - t / 2, arm - gap, t),
                QRectF(cx - t / 2, cy - arm, t, arm - gap),
                QRectF(cx - t / 2, cy + gap, t, arm - gap),
            ):
                p.fillRect(rect, CROSS_BOLD_COLOR)

        stair_rects: list[tuple[QRectF, int]] = []
        if ov.staircase:
            # 16 equal luma steps from black (Y'=16) to white (Y'=235), drawn in
            # exactly the grey the preview uses for that luma.  Compare the
            # camera's rendering of a grey scale chart against it.
            levels = staircase_levels(16)
            strip_h = max(14.0, ih * 0.065)
            y0 = ih - strip_h
            width = iw / len(levels)
            for i, level in enumerate(levels):
                grey = luma_to_display(level)
                rect = QRectF(i * width, y0, width, strip_h)
                p.fillRect(rect, QColor(grey, grey, grey))
                stair_rects.append((p.transform().mapRect(rect), level))
            pen = QPen(QColor(128, 128, 128))
            pen.setCosmetic(True)
            p.setPen(pen)
            p.drawRect(QRectF(0, y0, iw, strip_h))
        p.restore()

        font = QFont(self.font())
        font.setPointSizeF(8.0)
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetricsF(font)
        for pos, text, color in labels:
            width = fm.horizontalAdvance(text) + 8
            rect = QRectF(pos.x() - width - 3, pos.y() + 3, width, fm.height() + 2)
            p.fillRect(rect, QColor(0, 0, 0, 150))
            p.setPen(color)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        if stair_rects:
            show_all = stair_rects[0][0].width() > fm.horizontalAdvance("235") + 6
            for i, (rect, level) in enumerate(stair_rects):
                if not show_all and i not in (0, len(stair_rects) - 1):
                    continue
                p.setPen(QColor(0, 0, 0) if luma_to_display(level) > 110 else QColor(230, 230, 230))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(level))

    # -- HUD -----------------------------------------------------------------------

    def _box(self, p: QPainter, text: str, anchor: QPointF, align: Qt.AlignmentFlag,
             fg: QColor, bg: QColor, font: QFont) -> QRectF:
        fm = QFontMetricsF(font)
        lines = text.split("\n")
        w = max(fm.horizontalAdvance(line) for line in lines) + 14
        h = fm.lineSpacing() * len(lines) + 8
        x, y = anchor.x(), anchor.y()
        if align & Qt.AlignmentFlag.AlignRight:
            x -= w
        elif align & Qt.AlignmentFlag.AlignHCenter:
            x -= w / 2
        if align & Qt.AlignmentFlag.AlignVCenter:
            y -= h / 2
        rect = QRectF(x, y, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(fg)
        p.setFont(font)
        p.drawText(rect.adjusted(7, 4, -7, -4), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), text)
        return rect

    def _draw_hud(self, p: QPainter, target: QRectF) -> None:
        font = QFont(self.font())
        font.setPointSizeF(9.0)
        area = QRectF(self.rect()).intersected(target.adjusted(-2000, 0, 2000, 0)) if target.isValid() else QRectF(self.rect())
        top = max(8.0, area.top() + 8.0)
        left, right = 8.0, self.width() - 8.0

        if self.hud_lines:
            self._box(p, "\n".join(self.hud_lines), QPointF(left, top), Qt.AlignmentFlag.AlignLeft, HUD_FG, HUD_BG, font)
        if self.rec_text:
            bold = QFont(font)
            bold.setBold(True)
            bold.setPointSizeF(11.0)
            self._box(p, self.rec_text, QPointF(right, top), Qt.AlignmentFlag.AlignRight, QColor(255, 255, 255), REC_BG, bold)
        y = top + 50
        if self.frozen:
            bold = QFont(font)
            bold.setBold(True)
            bold.setPointSizeF(12.0)
            self._box(p, "❚❚  FROZEN — press Space for live", QPointF(self.width() / 2, top),
                      Qt.AlignmentFlag.AlignHCenter, QColor(20, 20, 20), QColor(240, 160, 32, 235), bold)
        if self.banner:
            text, level = self.banner
            bg, fg = (ERROR_BG, QColor(255, 190, 190)) if level == "error" else (WARN_BG, WARN_FG)
            bold = QFont(font)
            bold.setBold(True)
            self._box(p, text, QPointF(self.width() / 2, y), Qt.AlignmentFlag.AlignHCenter, fg, bg, bold)
        if self.center_message:
            self._draw_center_message(p, target)

    def _draw_center_message(self, p: QPainter, target: QRectF) -> None:
        font = QFont(self.font())
        font.setPointSizeF(13.0)
        font.setBold(True)
        center = target.center() if target.isValid() else QRectF(self.rect()).center()
        self._box(p, self.center_message or "", center,
                  Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                  QColor(255, 225, 225), ERROR_BG, font)
