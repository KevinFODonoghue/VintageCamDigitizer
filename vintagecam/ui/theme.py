"""
Dark, strictly neutral-grey theme.

A video monitor's surroundings should be dark and colourless.  A bright or tinted
UI next to the picture shifts your eye's white balance and brightness judgement —
the very things you're calibrating.  So every grey here has R = G = B (even the
selection highlight), and colour is reserved for status: red = recording,
amber = warning, green = OK.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

REC_RED = "#e53935"
WARN_AMBER = "#f0a020"
OK_GREEN = "#4caf50"
ERROR_RED = "#ff6b6b"
MUTED = "#8c8c8c"
TEXT = "#d7d7d7"


def _grey(level: int) -> QColor:
    return QColor(level, level, level)


STYLESHEET = f"""
QToolTip {{ color: #e6e6e6; background: #323232; border: 1px solid #5a5a5a; padding: 5px; }}
QGroupBox {{ border: 1px solid #444; border-radius: 5px; margin-top: 16px; padding: 10px 8px 8px 8px;
             font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #c8c8c8; }}
QDockWidget {{ font-weight: 600; }}
QDockWidget::title {{ background: #2b2b2b; padding: 5px 8px; }}
QLabel[muted="true"] {{ color: {MUTED}; font-weight: normal; }}
QLabel[valueLabel="true"] {{ font-family: Consolas, "Cascadia Mono", monospace; }}
QLabel#warningBanner {{ background: #463300; color: #ffd27a; border: 1px solid {WARN_AMBER};
                        border-radius: 4px; padding: 6px; font-weight: 600; }}
QLabel#errorBanner {{ background: #481818; color: #ffc0c0; border: 1px solid {REC_RED};
                      border-radius: 4px; padding: 6px; font-weight: 600; }}
QPushButton {{ padding: 5px 10px; }}
QPushButton#recordButton {{ font-size: 16px; font-weight: 700; padding: 12px; border-radius: 6px;
                            background: #3a3a3a; border: 1px solid #5c5c5c; }}
QPushButton#recordButton:hover {{ background: #454545; }}
QPushButton#recordButton[recording="true"] {{ background: #b71c1c; border: 1px solid {REC_RED}; color: white; }}
QPushButton#resetNeutral[offNeutral="true"] {{ background: #5e4200; border: 1px solid {WARN_AMBER};
                                              color: white; font-weight: 700; }}
QProgressBar#whiteBar {{ border: 1px solid #555; border-radius: 4px; background: #181818; }}
QProgressBar#whiteBar::chunk {{ background: #e6e6e6; border-radius: 3px; }}
QPlainTextEdit#log {{ font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; }}
QScrollArea#previewArea, QScrollArea#previewArea > QWidget > QWidget {{ background: black; }}
"""


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    roles = QPalette.ColorRole
    pal.setColor(roles.Window, _grey(34))
    pal.setColor(roles.WindowText, _grey(215))
    pal.setColor(roles.Base, _grey(24))
    pal.setColor(roles.AlternateBase, _grey(40))
    pal.setColor(roles.ToolTipBase, _grey(50))
    pal.setColor(roles.ToolTipText, _grey(230))
    pal.setColor(roles.PlaceholderText, _grey(120))
    pal.setColor(roles.Text, _grey(215))
    pal.setColor(roles.Button, _grey(50))
    pal.setColor(roles.ButtonText, _grey(222))
    pal.setColor(roles.BrightText, QColor(ERROR_RED))
    pal.setColor(roles.Light, _grey(72))
    pal.setColor(roles.Midlight, _grey(60))
    pal.setColor(roles.Mid, _grey(46))
    pal.setColor(roles.Dark, _grey(20))
    pal.setColor(roles.Shadow, _grey(8))
    pal.setColor(roles.Highlight, _grey(96))
    pal.setColor(roles.HighlightedText, _grey(255))
    pal.setColor(roles.Link, _grey(205))
    for role in (roles.Text, roles.WindowText, roles.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, _grey(105))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
