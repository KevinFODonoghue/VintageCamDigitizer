"""
The Log panel: every warning and error, on screen, with a timestamp.

"No silent failures" — anything worth telling the user goes through Python's
``logging`` module, and this panel shows it (INFO and above).  A full DEBUG log
is also written to ``logs/vintagecam.log`` for troubleshooting.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from .theme import ERROR_RED, MUTED, TEXT, WARN_AMBER


class _LogBridge(QObject):
    record = Signal(int, str)


class QtLogHandler(logging.Handler):
    """A logging handler that forwards records to the GUI thread via a Qt signal.

    Records can come from any thread (capture, recorder…).  Emitting a signal
    is thread-safe; Qt queues it and the panel appends the line on the GUI thread.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.bridge = _LogBridge()
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # never let logging itself crash the app
            text = record.getMessage()
        try:
            self.bridge.record.emit(record.levelno, text)
        except RuntimeError:  # the bridge was deleted during shutdown
            pass


class LogPanel(QWidget):
    def __init__(self, log_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._log_dir = log_dir
        self.view = QPlainTextEdit()
        self.view.setObjectName("log")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        self.view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        clear = QPushButton("Clear")
        clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        clear.clicked.connect(self.view.clear)
        folder = QPushButton("Open log folder")
        folder.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._log_dir))))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(folder)
        buttons.addWidget(clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.view, 1)
        layout.addLayout(buttons)

    @Slot(int, str)
    def append(self, levelno: int, text: str) -> None:
        if levelno >= logging.ERROR:
            color = ERROR_RED
        elif levelno >= logging.WARNING:
            color = WARN_AMBER
        elif levelno >= logging.INFO:
            color = TEXT
        else:
            color = MUTED
        self.view.appendHtml(f'<span style="color:{color}; white-space:pre-wrap;">{html.escape(text)}</span>')
