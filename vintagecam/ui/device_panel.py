"""
The Device panel: which device, which input, which TV standard, the card's proc
amp, whether a picture signal is arriving, and where the sound comes from.

It only shows state and reports what the user changed (Qt signals).  The main
window does the actual work.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..capture import CaptureState
from ..config import AUDIO_PLUG_LABELS, VIDEO_INPUT_LABELS, Settings
from ..dshow import ProcAmp, ProcAmpRange
from ..video_format import STANDARDS
from .theme import ERROR_RED, MUTED, OK_GREEN, WARN_AMBER
from .widgets import NeutralSlider, muted_label, repolish, value_label

#: Display order for the proc amp rows (the usual order on a TV's menu).
PROC_AMP_ORDER = (ProcAmp.BRIGHTNESS, ProcAmp.CONTRAST, ProcAmp.SATURATION, ProcAmp.HUE)

PROC_AMP_HELP = {
    ProcAmp.BRIGHTNESS: "Adds an offset to luma (moves black and white up or down together).",
    ProcAmp.CONTRAST: "Scales luma (stretches the distance between black and white).",
    ProcAmp.SATURATION: "Scales Cb/Cr — how strong colours are. At minimum the picture is grey.",
    ProcAmp.HUE: "Rotates Cb/Cr — shifts every colour around the colour wheel (the NTSC 'tint' knob).",
}

_STATE_COLORS = {
    CaptureState.RUNNING: OK_GREEN,
    CaptureState.OPENING: MUTED,
    CaptureState.WAITING: WARN_AMBER,
    CaptureState.FAILED: ERROR_RED,
    CaptureState.STOPPED: MUTED,
}


def _no_focus(*widgets: QWidget) -> None:
    """Buttons must not keep keyboard focus, or Space would 'click' them instead of freezing."""
    for w in widgets:
        w.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class DevicePanel(QWidget):
    video_device_selected = Signal(str)
    video_input_selected = Signal(str)
    standard_selected = Signal(str)
    refresh_clicked = Signal()
    retry_clicked = Signal()
    proc_amp_edited = Signal(object, int)  # (ProcAmp, value)
    reset_neutral_clicked = Signal()
    audio_device_selected = Signal(str)
    audio_plug_selected = Signal(str)
    record_audio_toggled = Signal(bool)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ranges: dict[ProcAmp, ProcAmpRange] = {}
        self._values: dict[ProcAmp, int] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._build_device_box(settings))
        layout.addWidget(self._build_proc_amp_box())
        layout.addWidget(self._build_audio_box(settings))
        layout.addStretch(1)

    # -- construction ------------------------------------------------------------

    def _build_device_box(self, settings: Settings) -> QGroupBox:
        box = QGroupBox("Capture device")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)

        self.device_combo = QComboBox()
        self.device_combo.setToolTip("DirectShow video capture devices on this PC")
        self.device_combo.activated.connect(
            lambda i: self.video_device_selected.emit(self.device_combo.itemData(i) or "")
        )
        self.refresh_button = QToolButton()
        self.refresh_button.setText("⟳")
        self.refresh_button.setToolTip("Re-scan for devices")
        self.refresh_button.clicked.connect(self.refresh_clicked)

        self.input_buttons = QButtonGroup(self)
        input_row = QHBoxLayout()
        for key, label in VIDEO_INPUT_LABELS.items():
            radio = QRadioButton(label)
            radio.setChecked(key == settings.video_input)
            radio.setProperty("inputKey", key)
            self.input_buttons.addButton(radio)
            input_row.addWidget(radio)
        input_row.addStretch(1)
        self.input_buttons.buttonClicked.connect(lambda b: self.video_input_selected.emit(b.property("inputKey")))

        self.standard_combo = QComboBox()
        for key, std in STANDARDS.items():
            self.standard_combo.addItem(std.name, key)
        self.standard_combo.setCurrentIndex(max(0, self.standard_combo.findData(settings.video_standard)))
        self.standard_combo.setToolTip(
            "The decoder chip needs to know the TV standard. Your RCA camera is NTSC-M (North America)."
        )
        self.standard_combo.activated.connect(lambda i: self.standard_selected.emit(self.standard_combo.itemData(i)))

        self.status_label = QLabel("Starting…")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        self.signal_label = QLabel("Signal: —")
        self.signal_label.setTextFormat(Qt.TextFormat.RichText)
        self.signal_label.setToolTip(
            "Whether the card's decoder has locked onto the camera's sync pulses.\n"
            "No lock = no picture signal (camera off, cable in the wrong input…)."
        )
        self.retry_button = QPushButton("Retry now")
        self.retry_button.setToolTip("Try to open the device again immediately (Ctrl+R)")
        self.retry_button.clicked.connect(self.retry_clicked)
        self.retry_button.hide()
        self.controls_note = muted_label()
        self.controls_note.hide()

        grid.addWidget(QLabel("Device"), 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.refresh_button)
        grid.addLayout(row, 0, 1)
        grid.addWidget(QLabel("Input"), 1, 0)
        grid.addLayout(input_row, 1, 1)
        grid.addWidget(QLabel("Standard"), 2, 0)
        grid.addWidget(self.standard_combo, 2, 1)
        grid.addWidget(self.controls_note, 3, 0, 1, 2)
        grid.addWidget(self.status_label, 4, 0, 1, 2)
        grid.addWidget(self.signal_label, 5, 0, 1, 2)
        grid.addWidget(self.retry_button, 6, 0, 1, 2)
        _no_focus(self.device_combo, self.refresh_button, self.standard_combo, self.retry_button,
                  *self.input_buttons.buttons())
        return box

    def _build_proc_amp_box(self) -> QGroupBox:
        box = QGroupBox("Proc amp  (card picture controls)")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        intro = muted_label(
            "Keep these at neutral (amber notch) while calibrating. Otherwise you'd be measuring "
            "the card's correction, not the camera."
        )
        grid.addWidget(intro, 0, 0, 1, 3)

        self.procamp_warning = QLabel("⚠  Proc amp is NOT at neutral — readings include the card's correction.")
        self.procamp_warning.setObjectName("warningBanner")
        self.procamp_warning.setWordWrap(True)
        self.procamp_warning.hide()
        grid.addWidget(self.procamp_warning, 1, 0, 1, 3)

        self.sliders: dict[ProcAmp, NeutralSlider] = {}
        self.value_labels: dict[ProcAmp, QLabel] = {}
        for row, prop in enumerate(PROC_AMP_ORDER, start=2):
            name = QLabel(prop.label)
            name.setToolTip(PROC_AMP_HELP[prop])
            slider = NeutralSlider()
            slider.setToolTip(PROC_AMP_HELP[prop])
            slider.valueChanged.connect(lambda v, p=prop: self._on_slider(p, v))
            value = value_label("—")
            value.setMinimumWidth(92)
            self.sliders[prop], self.value_labels[prop] = slider, value
            grid.addWidget(name, row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(value, row, 2)

        self.reset_button = QPushButton("Reset all to neutral")
        self.reset_button.setObjectName("resetNeutral")
        self.reset_button.setToolTip("Put brightness, contrast, saturation and hue back to the driver's defaults")
        self.reset_button.clicked.connect(self.reset_neutral_clicked)
        self.procamp_note = muted_label()
        grid.addWidget(self.reset_button, 6, 0, 1, 3)
        grid.addWidget(self.procamp_note, 7, 0, 1, 3)
        _no_focus(self.reset_button)
        self._proc_amp_box = box
        self.set_proc_amp(None, None, "Waiting for the device…")
        return box

    def _build_audio_box(self, settings: Settings) -> QGroupBox:
        box = QGroupBox("Audio")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        self.audio_combo = QComboBox()
        self.audio_combo.setToolTip("Where the sound comes from. Automatic = the Elgato's red/white RCA jacks.")
        self.audio_combo.activated.connect(lambda i: self.audio_device_selected.emit(self.audio_combo.itemData(i) or ""))
        self.audio_plug_combo = QComboBox()
        for key, label in AUDIO_PLUG_LABELS.items():
            self.audio_plug_combo.addItem(label, key)
        self.audio_plug_combo.setCurrentIndex(max(0, self.audio_plug_combo.findData(settings.audio_plug)))
        self.audio_plug_combo.setToolTip(
            "Which of the Elgato's RCA audio plugs to record (red = right, white = left).\n"
            "Stereo records both; the mono choices record one plug's channel on its own."
        )
        self.audio_plug_combo.activated.connect(
            lambda i: self.audio_plug_selected.emit(self.audio_plug_combo.itemData(i))
        )
        self.record_audio_check = QCheckBox("Record audio with the video (48 kHz PCM)")
        self.record_audio_check.setChecked(settings.audio_enabled)
        self.record_audio_check.toggled.connect(self.record_audio_toggled)
        self.audio_note = muted_label()
        grid.addWidget(QLabel("Input"), 0, 0)
        grid.addWidget(self.audio_combo, 0, 1)
        grid.addWidget(QLabel("Plug"), 1, 0)
        grid.addWidget(self.audio_plug_combo, 1, 1)
        grid.addWidget(self.record_audio_check, 2, 0, 1, 2)
        grid.addWidget(self.audio_note, 3, 0, 1, 2)
        _no_focus(self.audio_combo, self.audio_plug_combo, self.record_audio_check)
        return box

    # -- updates from the main window --------------------------------------------

    @staticmethod
    def _fill(combo: QComboBox, names: list[str], current: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        for name in names:
            combo.addItem(name, name)
        if current and current not in names:
            combo.addItem(f"{current}  (not connected)", current)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.blockSignals(False)

    def set_video_devices(self, names: list[str], current: str) -> None:
        self._fill(self.device_combo, names, current)

    def set_audio_inputs(self, items: list[tuple[str, str]], current: str) -> None:
        """``items`` are (label, key) pairs; ``current`` is the saved key."""
        combo = self.audio_combo
        combo.blockSignals(True)
        combo.clear()
        for label, key in items:
            combo.addItem(label, key)
        index = combo.findData(current)
        if index < 0 and current:
            combo.addItem(f"{current.split('::')[-1]}  (not connected)", current)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def set_audio_available(self, available: bool, note: str) -> None:
        for w in (self.audio_combo, self.audio_plug_combo, self.record_audio_check):
            w.setEnabled(available)
        self.audio_note.setText(note)
        self.audio_note.setVisible(bool(note))

    def set_capture_status(self, state: CaptureState, message: str) -> None:
        color = _STATE_COLORS.get(state, MUTED)
        self.status_label.setText(f'<span style="color:{color}">●</span> {message}')
        self.retry_button.setVisible(state in (CaptureState.WAITING, CaptureState.FAILED))

    def set_signal(self, locked: bool | None) -> None:
        if locked is None:
            self.signal_label.setText("Signal: —")
        elif locked:
            self.signal_label.setText(f'Signal: <span style="color:{OK_GREEN}">● locked</span>')
        else:
            self.signal_label.setText(
                f'Signal: <span style="color:{ERROR_RED}">● NO SIGNAL</span> — is the camera on and cabled?'
            )

    def set_device_controls_enabled(self, enabled: bool, note: str = "") -> None:
        """Device, input and standard can't change mid-recording (it would reopen the device)."""
        for w in (self.device_combo, self.refresh_button, self.standard_combo, *self.input_buttons.buttons(),
                  self.audio_combo, self.audio_plug_combo, self.record_audio_check):
            w.setEnabled(enabled)
        self.controls_note.setText(note)
        self.controls_note.setVisible(bool(note) and not enabled)

    def select_input(self, key: str) -> None:
        for button in self.input_buttons.buttons():
            button.setChecked(button.property("inputKey") == key)

    def select_standard(self, key: str) -> None:
        self.standard_combo.setCurrentIndex(max(0, self.standard_combo.findData(key)))

    def set_proc_amp(
        self, ranges: dict[ProcAmp, ProcAmpRange] | None, values: dict[ProcAmp, int] | None, note: str = ""
    ) -> None:
        """Configure the sliders from the driver's ranges, or disable them (None)."""
        self._ranges = dict(ranges or {})
        self._values = dict(values or {})
        for prop, slider in self.sliders.items():
            rng = self._ranges.get(prop)
            slider.blockSignals(True)
            if rng is None:
                slider.setEnabled(False)
                slider.set_neutral(None)
                self.value_labels[prop].setText("—")
            else:
                slider.setEnabled(True)
                slider.setRange(rng.minimum, rng.maximum)
                slider.setSingleStep(max(1, rng.step))
                slider.setPageStep(max(1, rng.step) * 8)
                slider.set_neutral(rng.default)
                slider.setValue(self._values.get(prop, rng.default))
                self._show_value(prop)
            slider.blockSignals(False)
        self.reset_button.setEnabled(bool(self._ranges))
        self.procamp_note.setText(note)
        self.procamp_note.setVisible(bool(note))
        self._update_warning()

    def update_proc_amp_value(self, prop: ProcAmp, value: int) -> None:
        self._values[prop] = value
        slider = self.sliders[prop]
        slider.blockSignals(True)
        slider.setValue(value)
        slider.blockSignals(False)
        self._show_value(prop)
        self._update_warning()

    def off_neutral(self) -> dict[ProcAmp, tuple[int, int]]:
        """Controls not at neutral: {prop: (value, neutral)}."""
        return {
            prop: (self._values[prop], rng.default)
            for prop, rng in self._ranges.items()
            if prop in self._values and self._values[prop] != rng.default
        }

    # -- internals -----------------------------------------------------------------

    def _on_slider(self, prop: ProcAmp, value: int) -> None:
        self._values[prop] = value
        self._show_value(prop)
        self._update_warning()
        self.proc_amp_edited.emit(prop, value)

    def _show_value(self, prop: ProcAmp) -> None:
        rng = self._ranges.get(prop)
        value = self._values.get(prop)
        if rng is None or value is None:
            self.value_labels[prop].setText("—")
            return
        delta = value - rng.default
        text = f"{value}" if delta == 0 else f"{value} ({delta:+d})"
        self.value_labels[prop].setText(text)
        self.value_labels[prop].setToolTip(f"Range {rng.minimum}–{rng.maximum}, neutral {rng.default}")
        self.value_labels[prop].setStyleSheet("" if delta == 0 else f"color: {WARN_AMBER}; font-weight: 700;")

    def _update_warning(self) -> None:
        off = self.off_neutral()
        self.procamp_warning.setVisible(bool(off))
        self.reset_button.setProperty("offNeutral", bool(off))
        repolish(self.reset_button)
