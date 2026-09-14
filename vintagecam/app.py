"""
Start-up: logging, crash reporting, Qt and the theme, then the main window.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path

from . import APP_NAME, __version__
from .config import app_dir, load_settings

log = logging.getLogger("vintagecam")


def _setup_logging() -> Path:
    """Console (INFO), a rotating file ``logs/vintagecam.log`` (DEBUG), and later the Log panel."""
    log_dir = app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "vintagecam.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stderr is not None:  # None when started with pythonw.exe (no console)
        # A redirected console uses a legacy code page that can't encode ● or —;
        # replace such characters rather than failing to log the line.
        try:
            sys.stderr.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)
        root.addHandler(console)
    return log_dir


def _install_crash_hooks() -> None:
    """Uncaught exceptions are logged with a traceback and shown — never silent."""
    last_dialog = [0.0]

    def excepthook(exc_type, exc, tb) -> None:
        log.critical("Unexpected error", exc_info=(exc_type, exc, tb))
        if threading.current_thread() is not threading.main_thread():
            return
        if time.monotonic() - last_dialog[0] < 10:  # don't bury the user in dialogs
            return
        last_dialog[0] = time.monotonic()
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(None, f"{APP_NAME} — unexpected error",
                                     f"{exc_type.__name__}: {exc}\n\nThe details are in the Log panel "
                                     "and logs/vintagecam.log.")
        except Exception:
            pass

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        name = args.thread.name if args.thread else "?"
        log.critical("Unexpected error in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="main.py", description=f"{APP_NAME}: analog video capture and calibration.")
    # Developer aids: save a screenshot of the window when it closes, and/or close after N seconds.
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--quit-after", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--auto-record", type=float, help=argparse.SUPPRESS)  # record N s once live, then quit
    parser.add_argument("--export", nargs="+", type=Path, metavar="RECORDING",
                        help="make an MP4 viewing copy of each recording (next to it), then exit")
    parser.add_argument("--field-order", default="auto",
                        help="with --export: auto (measure it), tff or bff")
    args = parser.parse_args(argv)
    if args.export:  # no window and no log file: the app runs this as a child process for each export
        from .export import main as export_main

        return export_main([*map(str, args.export), "--field-order", args.field_order])

    log_dir = _setup_logging()
    _install_crash_hooks()
    log.info("Starting %s %s (Python %s)", APP_NAME, __version__, sys.version.split()[0])

    from PySide6.QtCore import QtMsgType, QTimer, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    from .ui.log_panel import QtLogHandler
    from .ui.main_window import MainWindow
    from .ui.theme import apply_dark_theme

    qt_log = logging.getLogger("qt")

    def qt_message(mode: QtMsgType, _context, message: str) -> None:
        level = logging.WARNING if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg) else logging.DEBUG
        qt_log.log(level, message)

    qInstallMessageHandler(qt_message)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    apply_dark_theme(app)

    ui_handler = QtLogHandler()
    logging.getLogger().addHandler(ui_handler)
    settings, warnings = load_settings()
    window = MainWindow(settings, warnings, ui_handler, log_dir)
    window.show()

    if args.screenshot:
        window.screenshot_on_close = args.screenshot  # taken in closeEvent, however the window closes
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), window.close)
    if args.auto_record:  # exercises the whole record path, exactly as pressing R would
        def begin() -> None:
            if not window.is_live():
                QTimer.singleShot(250, begin)
                return
            window.toggle_recording()
            QTimer.singleShot(int(args.auto_record * 1000), end)

        def end() -> None:
            if args.screenshot:  # mid-recording, while the level meter is moving
                window.grab().save(str(args.screenshot))
                log.info("Screenshot saved to %s", args.screenshot)
                window.screenshot_on_close = None
            window.toggle_recording()
            QTimer.singleShot(3000, window.close)  # give the file a moment to be finalised

        QTimer.singleShot(500, begin)

    return app.exec()
