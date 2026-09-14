"""
The Pot Assist panel: which pot to turn next, and which way, to bring the
camera's shading or dynamic-focus errors to zero.

It only shows readings and reports clicks (Qt signals); the measuring happens
on the analysis thread (analysis.py, pot_assist.py).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..analysis import PotStatus
from ..pot_assist import MODE_HELP, MODE_LABELS, MODES, ORDER, POTS, TERM_HELP
from .theme import OK_GREEN, WARN_AMBER
from .widgets import muted_label, value_label

TURN_TEXT = {"turn CW": "turn CW  ↻", "turn CCW": "turn CCW  ↺", "OK": "OK  ✓", "learn": "press Learn"}
_BIG = "font-size: 13pt; font-weight: 600;"


class PotPanel(QWidget):
    mode_selected = Signal(str)
    noise_clicked = Signal()
    learn_clicked = Signal(str)  # the pot
    done_clicked = Signal()
    cancel_clicked = Signal()
    boxes_toggled = Signal(bool)

    def __init__(self, mode: str, show_boxes: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = mode
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        modes = QGroupBox("Adjusting")
        mode_col = QVBoxLayout(modes)
        mode_row = QHBoxLayout()
        self.mode_buttons = QButtonGroup(self)
        for key in MODES:
            button = QRadioButton(MODE_LABELS[key])
            button.setProperty("modeKey", key)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.mode_buttons.addButton(button)
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        self.mode_buttons.buttonClicked.connect(lambda b: self.mode_selected.emit(b.property("modeKey")))
        self.mode_help = muted_label()
        mode_col.addLayout(mode_row)
        mode_col.addWidget(self.mode_help)
        layout.addWidget(modes)

        readings = QGroupBox("Readings")
        grid = QGridLayout(readings)
        grid.setColumnStretch(2, 1)
        for col, text in enumerate(("Term", "Pot", "Error", "Turn", "")):
            grid.addWidget(muted_label(text), 0, col)
        self._rows: dict[str, tuple[QLabel, QLabel, QLabel, QPushButton]] = {}
        for row, name in enumerate(ORDER, start=1):
            label = QLabel(name)
            label.setToolTip(TERM_HELP[name])
            pot, error, turn = value_label("—"), value_label("—"), QLabel("—")
            error.setStyleSheet(_BIG)
            learn = QPushButton("Learn")
            learn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            learn.setToolTip("Teach the app which way this pot moves its reading: it reads, you turn the pot "
                             "a little clockwise and press Done, it reads again.")
            learn.clicked.connect(lambda _=False, n=name: self.learn_clicked.emit(POTS[self._mode][n]))
            for col, widget in enumerate((label, pot, error, turn, learn)):
                grid.addWidget(widget, row, col)
            self._rows[name] = (pot, error, turn, learn)
        self.boxes_value = muted_label()
        grid.addWidget(self.boxes_value, len(ORDER) + 1, 0, 1, 5)
        layout.addWidget(readings)

        self.next_label = QLabel("—")
        self.next_label.setWordWrap(True)
        self.next_label.setStyleSheet(_BIG)
        layout.addWidget(self.next_label)

        self.fill_bar = QProgressBar()
        self.fill_bar.setRange(0, 100)
        self.fill_bar.setTextVisible(False)
        self.fill_bar.setFixedHeight(8)
        self.status_label = muted_label("Waiting for video…")
        self.signal_label = QLabel()
        self.signal_label.setWordWrap(True)
        self.signal_label.setStyleSheet(f"color: {WARN_AMBER}; font-weight: 600;")
        self.signal_label.hide()
        layout.addWidget(self.fill_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(self.signal_label)

        tolerance = QHBoxLayout()
        self.tolerance_value = value_label("Tolerance: not measured")
        self.noise_button = QPushButton("Measure noise")
        self.noise_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.noise_button.setToolTip("Two readings a few seconds apart, with nothing touched. Twice their biggest "
                                     "difference becomes the tolerance: inside it, a term counts as OK.")
        self.noise_button.clicked.connect(self.noise_clicked)
        tolerance.addWidget(self.tolerance_value, 1)
        tolerance.addWidget(self.noise_button)
        layout.addLayout(tolerance)

        self.prompt_label = QLabel()
        self.prompt_label.setWordWrap(True)
        self.prompt_label.hide()
        self.done_button = QPushButton("Done")
        self.cancel_button = QPushButton("Cancel")
        for button, signal in ((self.done_button, self.done_clicked), (self.cancel_button, self.cancel_clicked)):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(signal)
            button.hide()
        task_row = QHBoxLayout()
        task_row.addWidget(self.done_button)
        task_row.addWidget(self.cancel_button)
        task_row.addStretch(1)
        layout.addWidget(self.prompt_label)
        layout.addLayout(task_row)

        self.boxes_check = QCheckBox("Show the five sample boxes on the picture")
        self.boxes_check.setChecked(show_boxes)
        self.boxes_check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.boxes_check.toggled.connect(self.boxes_toggled)
        layout.addWidget(self.boxes_check)
        layout.addStretch(1)
        self.set_mode(mode)

    # -- updates from the main window ------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Show ``mode``'s pots and help, and clear the old readings."""
        self._mode = mode
        for button in self.mode_buttons.buttons():
            button.setChecked(button.property("modeKey") == mode)
        self.mode_help.setText(MODE_HELP[mode])
        for name, (pot, error, turn, _) in self._rows.items():
            pot.setText(POTS[mode][name])
            error.setText("—")
            error.setStyleSheet(_BIG)
            turn.setText("—")
        self.boxes_value.setText("")
        self.next_label.setText("—")
        self.fill_bar.setValue(0)

    def set_signal(self, locked: bool | None) -> None:
        """Warn when there's no picture: the readings mean nothing then."""
        self.signal_label.setText("No picture signal: the readings mean nothing until the camera's picture is back.")
        self.signal_label.setVisible(locked is False)

    def show_status(self, status: PotStatus) -> None:
        if status.mode != self._mode:
            return  # from before a change of mode
        busy = bool(status.task)
        self.fill_bar.setValue(round(status.filled * 100))
        reading = status.reading
        if reading is None:
            self.status_label.setText(f"Collecting frames… {status.filled:.0%}")
            for _, error, turn, _ in self._rows.values():
                error.setText("—")
                error.setStyleSheet(_BIG)
                turn.setText("—")
            self.boxes_value.setText("")
            self.next_label.setText("Waiting for enough frames…")
        else:
            self.status_label.setText(f"Average of the last {reading.frames} frames")
            for term in reading.terms:
                _, error, turn, _ = self._rows[term.name]
                error.setText(self._format(term.error))
                colour = "" if reading.deadband is None else (OK_GREEN if term.ok else WARN_AMBER)
                error.setStyleSheet(_BIG + (f" color: {colour};" if colour else ""))
                turn.setText(TURN_TEXT[term.direction])
            self.boxes_value.setText("Boxes:  " + "   ".join(f"{name} {self._format(value)}"
                                                             for name, value in reading.boxes.items()))
            self.next_label.setText(self._next_text(reading))
        self.tolerance_value.setText("Tolerance: not measured" if status.deadband is None
                                     else f"Tolerance: ±{self._format(status.deadband, signed=False)}")
        self.prompt_label.setText(status.prompt)
        self.prompt_label.setVisible(bool(status.prompt))
        self.done_button.setVisible(status.waiting_for_user)
        self.cancel_button.setVisible(busy)
        self.noise_button.setEnabled(not busy)
        for *_, learn in self._rows.values():
            learn.setEnabled(not busy)

    # -- internals ---------------------------------------------------------------------

    def _format(self, value: float, signed: bool = True) -> str:
        sign = "+" if signed else ""
        if self._mode == "focus":  # detail energy: big numbers
            return f"{value:{sign},.0f}"
        return f"{value:{sign}.2f}"  # colour: code values from neutral

    @staticmethod
    def _next_text(reading) -> str:
        if reading.converged:
            return "✓  All four terms are inside the tolerance."
        term = next(t for t in reading.terms if t.name == reading.next_term)
        how = {"turn CW": "turn it clockwise", "turn CCW": "turn it anticlockwise",
               "learn": "press Learn on its row first"}.get(term.direction, "")
        text = f"Next: {term.pot} ({term.name}): {how}"
        if reading.deadband is None:
            text += ".  (Measure the noise, so the app knows how close to zero is close enough.)"
        return text
