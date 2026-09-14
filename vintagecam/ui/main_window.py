"""
Main window: wires capture, recording and device control to the UI.

Threads (the brief's architecture):
  CaptureThread (capture.py)   owns the device and fans frames out
  RecordThread  (recorder.py)  writes FFV1, only while recording
  GUI thread    (this file)    shows the newest frame and handles input
  AnalysisThread (analysis.py) runs the pot meter and white meter

Worker threads never touch widgets.  They call plain Python callbacks, which this
file turns into Qt signals (``WorkerBridge``); Qt delivers them on the GUI thread.
"""

from __future__ import annotations

import base64
import logging
import math
import textwrap
import time
from pathlib import Path

import av
from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, QUrl, Signal, Slot, qVersion
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import QDockWidget, QFileDialog, QFrame, QLabel, QMainWindow, QMessageBox, QScrollArea, QWidget

from .. import APP_NAME, __version__, dshow
from .. import audio as audio_io
from ..analysis import AnalysisThread, PotStatus
from ..audio import AUTO, AudioCapture, AudioError, AudioInput
from ..capture import CaptureRequest, CaptureState, CaptureStats, CaptureThread
from ..config import (AUDIO_PLUG_LABELS, DEINTERLACE_MODES, TYPICAL_GB_PER_HOUR, VIDEO_INPUT_LABELS, Settings,
                      save_settings)
from ..dshow import DShowError, ProcAmp, ProcAmpRange
from ..errors import CaptureError
from ..frames import CapturedFrame
from ..pot_meter import reference_from_rgb
from ..recorder import RecordingResult, RecordThread, free_disk_bytes, make_recording_path
from ..render import PreviewRenderer
from ..units import GB, format_bytes, format_duration, format_time_left
from ..video_format import STANDARDS, VideoStandard, standard_for_analog_flag
from ..white_meter import WhiteReading
from .device_panel import DevicePanel
from .export_queue import ExportQueue
from .log_panel import LogPanel, QtLogHandler
from .pot_panel import PotPanel, load_photo
from .preview import PreviewWidget
from .record_panel import RecordPanel
from .theme import ERROR_RED, MUTED, OK_GREEN, WARN_AMBER
from .white_panel import WhitePanel

log = logging.getLogger(__name__)

DEINTERLACE_LABELS = {
    "off": "Off — weave, exactly as recorded",
    "bob": "Bob — one field at a time, 59.94/s",
    "blend": "Blend — average the two fields",
}
DEINTERLACE_SHORT = {"off": "no deinterlace", "bob": "bob (view only)", "blend": "blend (view only)"}
FIELD_ORDER_LABELS = {"auto": "Auto (usual for the standard)", "tff": "Top field first", "bff": "Bottom field first"}
OVERLAY_SETTINGS = {
    "grid": "show_grid",
    "crosshair": "show_crosshair",
    "safe_areas": "show_safe_areas",
    "staircase": "show_staircase",
}
_STATE_COLORS = {
    CaptureState.RUNNING: OK_GREEN,
    CaptureState.WAITING: WARN_AMBER,
    CaptureState.FAILED: ERROR_RED,
}


class WorkerBridge(QObject):
    """Worker-thread callbacks in, GUI-thread Qt signals out.

    The ``int`` on the capture signals is a generation number: after the capture
    thread is restarted, late messages from the old thread are ignored.
    """

    frame_ready = Signal(int)
    capture_state = Signal(int, object, str, object)
    capture_stats = Signal(int, object)
    recording_finished = Signal(object)
    pot_status = Signal(object)
    white_reading = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, startup_warnings: list[str], log_handler: QtLogHandler,
                 log_dir: Path) -> None:
        super().__init__()
        self.settings = settings
        self._log_handler = log_handler
        self._log_dir = log_dir

        self.renderer = PreviewRenderer()
        self.capture: CaptureThread | None = None
        self.recorder: RecordThread | None = None
        self.controls: dshow.VideoDeviceControls | None = None
        self._capture_gen = 0
        self._capture_state = CaptureState.STOPPED
        self._capture_message = "Starting…"
        self._last_logged_message = ""
        self._stats: CaptureStats | None = None
        self._latest: CapturedFrame | None = None
        self._shown: CapturedFrame | None = None
        self._frozen = False
        self._bob_generation = 0
        self._render_failed = False
        self._signal_locked: bool | None = None
        self._proc_amp_ranges: dict[ProcAmp, ProcAmpRange] = {}
        self._recording_finishing = False
        self._stop_reason: str | None = None
        self._last_disk_check = 0.0
        self._disk_warned = False
        self.screenshot_on_close: Path | None = None
        """Developer aid (``main.py --screenshot FILE``): grab the window just before it closes."""
        self._stuck_capture: CaptureThread | None = None
        """A capture thread that never finished stopping because the driver hung (see _stop_capture)."""
        self._restart_pending = False
        self.audio: AudioCapture | None = None
        self._audio_inputs: list[AudioInput] = []
        self._audio_progress = (0, 0.0)  # (frames seen, when they last increased)
        self._audio_warned = False
        self._pot_on_screen = False  # tracked from the dock's visibilityChanged (see _on_pot_dock_visibility)
        self._white_on_screen = False  # the same for the white meter

        self.bridge = WorkerBridge(self)
        queued = Qt.ConnectionType.QueuedConnection
        self.bridge.frame_ready.connect(self._on_frame_ready, queued)
        self.bridge.capture_state.connect(self._on_capture_state, queued)
        self.bridge.capture_stats.connect(self._on_capture_stats, queued)
        self.bridge.recording_finished.connect(self._on_recording_finished, queued)
        self.bridge.pot_status.connect(self._on_pot_status, queued)
        self.bridge.white_reading.connect(self._on_white_reading, queued)
        # Phase 2's analysis thread: the pot meter and the white meter measure there, never on the GUI thread.
        self.analysis = AnalysisThread(self.bridge.pot_status.emit, self.bridge.white_reading.emit)
        self.analysis.start()

        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self._build_ui()
        self._build_actions()
        self._restore_layout()
        self._apply_view_settings()
        if settings.pot_reference_photo:
            self._use_reference_photo(Path(settings.pot_reference_photo), quiet=True)

        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(500)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start()
        self.signal_timer = QTimer(self)
        self.signal_timer.setInterval(1000)
        self.signal_timer.timeout.connect(self._poll_signal)
        self.signal_timer.start()
        self.stuck_timer = QTimer(self)
        self.stuck_timer.setInterval(1000)
        self.stuck_timer.timeout.connect(self._check_stuck_capture)

        log.info("%s %s · PyAV %s (FFmpeg %s) · Qt %s", APP_NAME, __version__, av.__version__,
                 getattr(av, "ffmpeg_version_info", "?"), qVersion())
        for warning in startup_warnings:
            log.warning(warning)
        QTimer.singleShot(0, self._startup)

    # ======================================================================
    # Construction
    # ======================================================================

    def _build_ui(self) -> None:
        self.preview = PreviewWidget()
        self.preview.double_clicked.connect(lambda: self.act_fit.trigger())
        self.preview_area = QScrollArea()
        self.preview_area.setObjectName("previewArea")
        self.preview_area.setWidget(self.preview)
        self.preview_area.setWidgetResizable(True)
        self.preview_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_area.setFrameShape(QFrame.Shape.NoFrame)
        self.preview_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCentralWidget(self.preview_area)

        self.device_panel = DevicePanel(self.settings)
        self.record_panel = RecordPanel(self.settings)
        self.log_panel = LogPanel(self._log_dir)
        self.pot_panel = PotPanel(self.settings.show_pot_grid)
        self.white_panel = WhitePanel()
        self._log_handler.bridge.record.connect(self.log_panel.append)

        self.device_dock = self._make_dock("Device", self.device_panel, Qt.DockWidgetArea.RightDockWidgetArea, "deviceDock")
        self.record_dock = self._make_dock("Recording", self.record_panel, Qt.DockWidgetArea.RightDockWidgetArea, "recordDock")
        self.log_dock = self._make_dock("Log", self.log_panel, Qt.DockWidgetArea.BottomDockWidgetArea, "logDock")
        self.pot_dock = self._make_dock("Pot meter", self.pot_panel, Qt.DockWidgetArea.RightDockWidgetArea, "potDock")
        self.white_dock = self._make_dock("White meter", self.white_panel, Qt.DockWidgetArea.RightDockWidgetArea,
                                          "whiteDock")
        self.tabifyDockWidget(self.record_dock, self.pot_dock)
        self.tabifyDockWidget(self.pot_dock, self.white_dock)
        self.record_dock.raise_()
        self.resizeDocks([self.device_dock], [370], Qt.Orientation.Horizontal)
        self.resizeDocks([self.device_dock, self.record_dock], [640, 380], Qt.Orientation.Vertical)
        self.resizeDocks([self.log_dock], [140], Qt.Orientation.Vertical)

        dp = self.device_panel
        dp.video_device_selected.connect(self._on_video_device_selected)
        dp.video_input_selected.connect(self._on_video_input_selected)
        dp.standard_selected.connect(self._on_standard_selected)
        dp.refresh_clicked.connect(self._refresh_devices)
        dp.retry_clicked.connect(self._reconnect_now)
        dp.proc_amp_edited.connect(self._on_proc_amp_edited)
        dp.reset_neutral_clicked.connect(self._reset_proc_amp)
        dp.audio_device_selected.connect(self._on_audio_device_selected)
        dp.audio_plug_selected.connect(self._on_audio_plug_selected)
        dp.record_audio_toggled.connect(self._on_record_audio_toggled)

        rp = self.record_panel
        rp.record_clicked.connect(self.toggle_recording)
        rp.export_clicked.connect(self._choose_exports)
        rp.export_after_toggled.connect(self._on_export_after_toggled)
        self.exports = ExportQueue(self)
        self.exports.status_changed.connect(rp.set_export_status)
        self.exports.finished_one.connect(self._on_export_finished)
        rp.export_cancel_clicked.connect(lambda: self.exports.cancel_all())
        rp.output_dir_changed.connect(self._on_output_dir_changed)
        rp.prefix_changed.connect(self._on_prefix_changed)

        pp = self.pot_panel
        pp.measure_clicked.connect(self.analysis.measure)
        pp.reset_clicked.connect(self.analysis.reset)
        pp.grid_toggled.connect(self._on_pot_grid_toggled)
        pp.photo_load_clicked.connect(self._choose_reference_photo)
        pp.photo_clear_clicked.connect(self._clear_reference_photo)
        self.pot_dock.visibilityChanged.connect(self._on_pot_dock_visibility)
        self.white_dock.visibilityChanged.connect(self._on_white_dock_visibility)

        self.status_state = QLabel()
        self.status_fps = QLabel()
        self.status_drops = QLabel()
        self.status_lag = QLabel()
        self.status_lag.setToolTip(
            "How long after a frame arrives from the card it is drawn on screen (this app's share of the\n"
            "delay). The card, USB transfer and your display add their own delay on top."
        )
        for widget in (self.status_drops, self.status_lag, self.status_fps, self.status_state):
            self.statusBar().addPermanentWidget(widget)

    def _make_dock(self, title: str, widget: QWidget, area: Qt.DockWidgetArea, name: str) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)  # needed for saving/restoring the layout
        scroll = QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        dock.setWidget(scroll)
        self.addDockWidget(area, dock)
        return dock

    def _action(self, text: str, shortcut: str, slot, *, checkable: bool = False, checked: bool = False,
                tip: str = "") -> QAction:
        """A menu action whose single-key shortcut works anywhere in the app.

        ApplicationShortcut context: the shortcut fires even when a floating
        panel has focus — your other hand is inside the camera.
        """
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
            act.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        if checkable:
            act.setCheckable(True)
            act.setChecked(checked)
        if tip:
            act.setStatusTip(tip)
            act.setToolTip(tip)
        act.triggered.connect(slot)
        self.addAction(act)
        return act

    def _build_actions(self) -> None:
        s = self.settings
        bar = self.menuBar()

        m_capture = bar.addMenu("&Capture")
        self.act_record = self._action("Record / stop", "R", lambda *_: self.toggle_recording(),
                                       tip="Start or stop a lossless FFV1 recording")
        self.act_freeze = self._action("Freeze preview", "Space", self._set_frozen, checkable=True,
                                       tip="Hold the picture on screen. Recording is not affected.")
        self.act_reconnect = self._action("Reconnect now", "Ctrl+R", lambda *_: self._reconnect_now())
        self.act_open_folder = self._action("Open recordings folder", "Ctrl+O", lambda *_: self._open_output_folder())
        self.act_export = self._action("Export MP4 viewing copies…", "Ctrl+E", lambda *_: self._choose_exports(),
                                       tip="Make .mp4 copies of recordings that play in any player")
        self.act_quit = self._action("Quit", "Ctrl+Q", lambda *_: self.close())
        m_capture.addActions([self.act_record, self.act_freeze])
        m_capture.addSeparator()
        m_capture.addActions([self.act_reconnect, self.act_open_folder, self.act_export])
        m_capture.addSeparator()
        m_capture.addAction(self.act_quit)

        m_view = bar.addMenu("&View")
        self.act_grid = self._action("Centering grid (10 × 10)", "G", lambda on: self._set_overlay("grid", on),
                                     checkable=True, checked=s.show_grid)
        self.act_cross = self._action("Center crosshair", "C", lambda on: self._set_overlay("crosshair", on),
                                      checkable=True, checked=s.show_crosshair)
        self.act_safe = self._action("Safe areas (90% action, 80% title)", "S",
                                     lambda on: self._set_overlay("safe_areas", on), checkable=True,
                                     checked=s.show_safe_areas)
        self.act_stairs = self._action("Luma staircase reference (16 steps)", "L",
                                       lambda on: self._set_overlay("staircase", on), checkable=True,
                                       checked=s.show_staircase)
        m_view.addActions([self.act_grid, self.act_cross, self.act_safe, self.act_stairs])
        m_view.addSeparator()

        m_deint = m_view.addMenu("Deinterlace (preview only)")
        self.deint_group = QActionGroup(self)
        self.deint_actions: dict[str, QAction] = {}
        for mode in DEINTERLACE_MODES:
            act = QAction(DEINTERLACE_LABELS[mode], self)
            act.setCheckable(True)
            act.setChecked(mode == s.deinterlace)
            act.triggered.connect(lambda _=False, m=mode: self._set_deinterlace(m))
            self.deint_group.addAction(act)
            m_deint.addAction(act)
            self.deint_actions[mode] = act
        m_deint.addSeparator()
        self.act_cycle_deint = self._action("Next mode", "D", lambda *_: self._cycle_deinterlace())
        m_deint.addAction(self.act_cycle_deint)

        m_field = m_view.addMenu("Bob field order")
        self.field_group = QActionGroup(self)
        for key, label in FIELD_ORDER_LABELS.items():
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(key == s.field_order)
            act.triggered.connect(lambda _=False, k=key: self._set_field_order(k))
            self.field_group.addAction(act)
            m_field.addAction(act)

        self.act_fit = self._action("Fit to window (off = 1:1 pixels)", "F", self._set_fit, checkable=True,
                                    checked=s.fit_to_window)
        self.act_aspect = self._action("Correct pixel aspect (true 4:3)", "A", self._set_correct_aspect,
                                       checkable=True, checked=s.correct_aspect)
        self.act_hud = self._action("On-screen info", "I", self._set_hud, checkable=True, checked=s.show_hud)
        self.act_fullscreen = self._action("Full screen", "F11", lambda *_: self._toggle_fullscreen())
        self._action("Back to the picture", "Esc", lambda *_: self._escape())
        m_view.addActions([self.act_fit, self.act_aspect, self.act_hud, self.act_fullscreen])
        m_view.addSeparator()
        for dock, key in ((self.device_dock, "Ctrl+1"), (self.record_dock, "Ctrl+2"), (self.log_dock, "Ctrl+3")):
            act = dock.toggleViewAction()
            act.setShortcut(QKeySequence(key))
            act.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            m_view.addAction(act)
            self.addAction(act)

        self.act_pot = self._action("Pot meter panel", "Ctrl+4", lambda *_: self._toggle_pot_dock(),
                                    tip="Show the pot meter, or hide it")
        self.act_white = self._action("White meter panel", "Ctrl+5", lambda *_: self._toggle_white_dock(),
                                      tip="Show the white meter, or hide it")
        m_view.addActions([self.act_pot, self.act_white])

        m_help = bar.addMenu("&Help")
        m_help.addAction(self._action("Keyboard shortcuts", "F1", lambda *_: self._show_shortcuts()))
        m_help.addAction(self._action("About", "", lambda *_: self._show_about()))

    def _apply_view_settings(self) -> None:
        s = self.settings
        ov = self.preview.overlays
        ov.grid, ov.crosshair = s.show_grid, s.show_crosshair
        ov.safe_areas, ov.staircase = s.show_safe_areas, s.show_staircase
        std = STANDARDS[s.video_standard]
        self.preview.set_frame_geometry(std.width, std.height, float(std.pixel_aspect))
        self.preview.set_fit(s.fit_to_window)
        self.preview.set_correct_aspect(s.correct_aspect)
        self.preview.hud_enabled = s.show_hud
        self._update_hud()

    def _restore_layout(self) -> None:
        try:
            if self.settings.window_geometry:
                self.restoreGeometry(QByteArray(base64.b64decode(self.settings.window_geometry)))
            if self.settings.window_state:
                self.restoreState(QByteArray(base64.b64decode(self.settings.window_state)))
        except (ValueError, TypeError) as exc:
            log.warning("Could not restore the window layout (%s); using the default.", exc)

    def _save_layout(self) -> None:
        self.settings.window_geometry = base64.b64encode(bytes(self.saveGeometry())).decode("ascii")
        self.settings.window_state = base64.b64encode(bytes(self.saveState())).decode("ascii")

    # ======================================================================
    # Start-up, capture thread lifecycle
    # ======================================================================

    def _startup(self) -> None:
        self._refresh_devices()
        self._apply_decoder_standard()
        self._start_capture()
        self.preview.setFocus()

    def _refresh_devices(self) -> None:
        try:
            videos = dshow.list_video_devices()
        except DShowError as exc:
            log.error("Could not list capture devices: %s", exc)
            videos = []
        self.device_panel.set_video_devices([d.name for d in videos], self.settings.video_device)
        log.debug("Video devices: %s", [d.name for d in videos])
        self._refresh_audio_inputs()

    def _refresh_audio_inputs(self) -> None:
        """Re-scan audio inputs.  Runs before video opens (see audio._sd for why)."""
        if self.audio is not None:  # never re-scan PortAudio under an open stream
            return
        try:
            self._audio_inputs = audio_io.list_inputs(refresh=True)
        except Exception as exc:  # PortAudio couldn't start: no audio at all
            self._audio_inputs = []
            log.warning("Audio inputs can't be listed: %s", exc)
        items = [("Automatic: the Elgato's line input", AUTO)] + [(d.label, d.key) for d in self._audio_inputs]
        self.device_panel.set_audio_inputs(items, self.settings.audio_device)
        if any(d.is_elgato for d in self._audio_inputs):
            note = ("Line-level sound into the Elgato's red/white RCA jacks, captured with Windows kernel "
                    "streaming (the usual Windows audio routes can't open this card). There's only sound "
                    "while the video is live.")
        else:
            note = "The Elgato's audio input wasn't found (is the card plugged in?). Other inputs still work."
        self.device_panel.set_audio_available(bool(self._audio_inputs), note)
        log.debug("Audio inputs: %s", [d.key for d in self._audio_inputs])

    def _start_capture(self) -> None:
        if self._stuck_capture is not None:  # the driver still holds the old stream; see _stop_capture
            self._restart_pending = True
            return
        s = self.settings
        std = STANDARDS[s.video_standard]
        self.preview.set_frame_geometry(std.width, std.height, float(std.pixel_aspect))
        self._capture_gen += 1
        gen = self._capture_gen
        request = CaptureRequest(s.video_device, std, s.video_input, s.rtbufsize)
        self.capture = CaptureThread(
            request,
            on_state=lambda state, msg, err: self.bridge.capture_state.emit(gen, state, msg, err),
            on_stats=lambda stats: self.bridge.capture_stats.emit(gen, stats),
            on_frame=lambda: self.bridge.frame_ready.emit(gen),
        )
        self.capture.start()
        self.analysis.set_source(self.capture.analysis_slot)

    def _stop_capture(self) -> bool:
        """Stop the capture thread.  Returns False if the driver never let it finish.

        Why this can happen: the thread's last act is asking the driver to stop
        streaming, and this Elgato driver can hang in that call — seen when a PAL
        stream was closed while an NTSC camera was connected.  Nothing in a user
        program can interrupt a call stuck inside a driver; unplugging the card
        makes the driver cancel it.  Until then, opening the device again would
        only fail, so we wait for the stuck thread instead (_check_stuck_capture).
        """
        self.analysis.set_source(None)
        capture, self.capture = self.capture, None
        if capture is None:
            return True
        capture.set_record_sink(None)
        capture.stop()
        capture.join(timeout=5.0)
        if not capture.is_alive():
            return True
        self._stuck_capture = capture
        self._capture_gen += 1  # ignore anything the stuck thread reports later
        self.stuck_timer.start()
        message = ("The Elgato's driver stopped responding while closing the video stream. Unplug the "
                   "Elgato, wait 5 seconds and plug it back in; capture restarts by itself.")
        self._capture_state, self._capture_message = CaptureState.FAILED, message
        self.device_panel.set_capture_status(CaptureState.FAILED, message)
        log.error(message)
        self._update_status_bar()
        self._update_hud()
        return False

    def _check_stuck_capture(self) -> None:
        """Once a second while the driver is stuck: has unplugging the card freed it?"""
        stuck = self._stuck_capture
        if stuck is not None and stuck.is_alive():
            return
        self._stuck_capture = None
        self.stuck_timer.stop()
        log.info("The capture driver has let go of the old stream.")
        if self._restart_pending or self.capture is None:
            self._restart_pending = False
            self._apply_decoder_standard()
            self._start_capture()

    def _restart_capture(self, why: str) -> None:
        if self.recorder is not None:
            log.warning("Stop recording before changing the device, input or standard.")
            return
        log.info(why)
        stopped = self._stop_capture()
        self._latest = self._shown = None
        self._stats = None
        self._signal_locked = None
        self.device_panel.set_signal(None)
        self.pot_panel.set_signal(None)
        self.white_panel.set_signal(None)
        self.preview.clear_image()
        if not stopped:
            self._restart_pending = True  # _check_stuck_capture restarts once the driver lets go
            return
        self._apply_decoder_standard()
        self._start_capture()

    def _reconnect_now(self) -> None:
        if self._stuck_capture is not None:
            log.warning("Still waiting for the Elgato's driver to let go: unplug the card and plug it back in.")
            return
        if self.capture is not None:
            log.info("Retrying the device now…")
            self.capture.retry_now()

    # ======================================================================
    # Signals from the capture thread
    # ======================================================================

    @Slot(int)
    def _on_frame_ready(self, gen: int) -> None:
        if gen != self._capture_gen or self.capture is None:
            return
        frame = self.capture.preview_slot.take()  # the newest frame; older ones were dropped
        if frame is None:
            return
        self._latest = frame
        if not self._frozen:
            self._display(frame)

    def _display(self, frame: CapturedFrame) -> None:
        """Convert and show one frame (or, in bob mode, its first field now and second soon)."""
        self._shown = frame
        mode = self.settings.deinterlace
        try:
            if mode == "bob":
                first = 0 if self._field_order() == "tff" else 1
                self.preview.set_image(self.renderer.render(frame, "bob", first), frame.arrival_time)
                self._bob_generation += 1
                gen = self._bob_generation
                half_frame_ms = max(1, round(frame.standard.frame_duration * 500))
                QTimer.singleShot(half_frame_ms, lambda: self._show_second_field(frame, 1 - first, gen))
            else:
                self.preview.set_image(self.renderer.render(frame, mode), frame.arrival_time)
            self._render_failed = False
        except Exception:
            if not self._render_failed:  # log once, not 30 times a second
                log.exception("Could not draw a frame")
            self._render_failed = True

    def _show_second_field(self, frame: CapturedFrame, field: int, gen: int) -> None:
        if gen == self._bob_generation and not self._frozen and self.settings.deinterlace == "bob":
            self.preview.set_image(self.renderer.render(frame, "bob", field))

    @Slot(int, object, str, object)
    def _on_capture_state(self, gen: int, state: CaptureState, message: str, error: CaptureError | None) -> None:
        if gen != self._capture_gen:
            return
        previous = self._capture_state
        self._capture_state, self._capture_message = state, message
        self.device_panel.set_capture_status(state, message)

        # Log state changes once (a retry loop would otherwise repeat them every few seconds).
        if state != CaptureState.OPENING and message != self._last_logged_message:
            level = {CaptureState.WAITING: logging.WARNING, CaptureState.FAILED: logging.ERROR}.get(state, logging.INFO)
            log.log(level, message)
            self._last_logged_message = message

        if state == CaptureState.RUNNING:
            self._signal_locked = None
            if self._ensure_controls():
                self._poll_signal()
        elif previous == CaptureState.RUNNING:
            # The stream stopped — most likely the device was unplugged, which
            # also kills our property-access filter.  Drop it; it's reopened on reconnect.
            self._close_controls()
            self._signal_locked = None
            self.device_panel.set_signal(None)
            if self.recorder is not None and not self._recording_finishing:
                self._stop_recording(f"The video from the card stopped. {message}", unexpected=True)
        self._update_status_bar()
        self._update_hud()
        self.setWindowTitle(f"{APP_NAME} — {self.settings.video_device} — {state.value}")

    @Slot(int, object)
    def _on_capture_stats(self, gen: int, stats: CaptureStats) -> None:
        if gen != self._capture_gen:
            return
        self._stats = stats
        self._update_status_bar()
        self._update_hud()

    # ======================================================================
    # Device controls: proc amp, TV standard, signal lock (dshow.py)
    # ======================================================================

    def _ensure_controls(self) -> bool:
        if self.controls is not None and self.controls.is_open:
            return True
        controls = dshow.VideoDeviceControls(self.settings.video_device)
        try:
            controls.open()
        except DShowError as exc:
            self.controls = None
            self.device_panel.set_proc_amp(None, None, f"Proc amp unavailable: {exc}")
            log.debug("Device controls unavailable: %s", exc)
            return False
        self.controls = controls
        self._load_proc_amp()
        return True

    def _close_controls(self) -> None:
        controls, self.controls = self.controls, None
        if controls is not None:
            controls.close()
        self._proc_amp_ranges = {}
        self.device_panel.set_proc_amp(None, None, "Waiting for the device…")
        self._update_hud()

    def _load_proc_amp(self) -> None:
        controls = self.controls
        if controls is None:
            return
        if not controls.has_proc_amp:
            self.device_panel.set_proc_amp(None, None, "This device has no proc amp controls.")
            return
        ranges: dict[ProcAmp, ProcAmpRange] = {}
        values: dict[ProcAmp, int] = {}
        try:
            for prop in ProcAmp:
                rng = controls.proc_amp_range(prop)
                if rng is not None:
                    ranges[prop], values[prop] = rng, controls.get_proc_amp(prop)
        except DShowError as exc:
            log.error("Could not read the proc amp: %s", exc)
            self._close_controls()
            return
        self._proc_amp_ranges = ranges
        self.device_panel.set_proc_amp(ranges, values)
        off = self.device_panel.off_neutral()
        if off:
            log.warning("Proc amp is NOT at neutral: %s. Use “Reset all to neutral” before calibrating.",
                        self._describe_off_neutral(off))
        else:
            log.info("Proc amp at neutral (%s).", ", ".join(f"{p.label} {values[p]}" for p in ranges))
        self._update_hud()

    @Slot(object, int)
    def _on_proc_amp_edited(self, prop: ProcAmp, value: int) -> None:
        if not self._ensure_controls():
            return
        assert self.controls is not None
        try:
            actual = self.controls.set_proc_amp(prop, value)
        except DShowError as exc:
            log.error("Could not change %s: %s", prop.label, exc)
            self._close_controls()
            return
        if actual != value:
            self.device_panel.update_proc_amp_value(prop, actual)
        log.debug("%s -> %d", prop.label, actual)
        self._update_hud()

    def _reset_proc_amp(self) -> None:
        if not self._ensure_controls():
            log.error("Can't reset the proc amp: the device isn't available.")
            return
        assert self.controls is not None
        try:
            for prop, rng in self._proc_amp_ranges.items():
                self.device_panel.update_proc_amp_value(prop, self.controls.set_proc_amp(prop, rng.default))
        except DShowError as exc:
            log.error("Could not reset the proc amp: %s", exc)
            self._close_controls()
            return
        log.info("Proc amp reset to neutral (%s).",
                 ", ".join(f"{p.label} {r.default}" for p, r in self._proc_amp_ranges.items()))
        self._update_hud()

    def _apply_decoder_standard(self) -> None:
        """Make the card's decoder chip match the selected standard before streaming."""
        if self._stuck_capture is not None:  # don't poke a driver that's already stuck
            return
        std = STANDARDS[self.settings.video_standard]
        if not self._ensure_controls() or self.controls is None or not self.controls.has_decoder:
            return
        try:
            current = self.controls.tv_format()
            if standard_for_analog_flag(current) is not std:
                self.controls.set_tv_format(std.analog_flag)
                log.info("Switched the card's decoder to %s.", std.name)
        except DShowError as exc:
            log.warning("Could not set the decoder's TV standard: %s", exc)

    def _poll_signal(self) -> None:
        if self._capture_state != CaptureState.RUNNING or self.controls is None or not self.controls.has_decoder:
            return
        try:
            locked = self.controls.horizontal_locked()
        except DShowError as exc:
            log.debug("Signal-lock check failed: %s", exc)
            self._close_controls()
            return
        if locked != self._signal_locked:
            first_reading = self._signal_locked is None
            self._signal_locked = locked
            self.device_panel.set_signal(locked)
            self.pot_panel.set_signal(locked)
            self.white_panel.set_signal(locked)
            if not locked:
                log.warning("NO SIGNAL: the card isn't locked to a picture. Is the camera on and plugged into "
                            "the %s input?", VIDEO_INPUT_LABELS[self.settings.video_input])
            elif not first_reading:
                log.info("Signal locked.")
            self._update_hud()

    @staticmethod
    def _describe_off_neutral(off: dict[ProcAmp, tuple[int, int]]) -> str:
        return ", ".join(f"{prop.label} {value} (neutral {neutral})" for prop, (value, neutral) in off.items())

    # ======================================================================
    # Recording
    # ======================================================================

    def is_live(self) -> bool:
        """True while live video is arriving."""
        return self._capture_state == CaptureState.RUNNING

    def toggle_recording(self) -> None:
        if self._recording_finishing:
            return
        if self.recorder is None:
            self._start_recording()
        else:
            self._stop_recording("Recording stopped.")

    def _start_recording(self) -> None:
        if self._capture_state != CaptureState.RUNNING or self.capture is None:
            self._warn("Can't record yet", "There's no live video to record.\n\n" + self._capture_message)
            return
        s = self.settings
        folder = Path(s.output_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._warn("Can't record", f"Couldn't create the recordings folder:\n{folder}\n\n{exc}")
            return
        free = free_disk_bytes(folder)
        if free is not None and free < s.low_disk_stop_gb * GB:
            self._warn("Not enough disk space",
                       f"Only {format_bytes(free)} free for {folder}.\nFFV1 needs about 30–45 GB per hour.")
            return

        std = STANDARDS[s.video_standard]
        path = make_recording_path(folder, s.filename_prefix)
        off = self.device_panel.off_neutral()
        comment = (
            f"Captured with {APP_NAME} {__version__} from {s.video_device} "
            f"({VIDEO_INPUT_LABELS[s.video_input]}, {std.name}). "
            + ("Proc amp at neutral." if not off else f"Proc amp NOT neutral: {self._describe_off_neutral(off)}.")
        )
        # Audio first (video is already live, so the card's audio path is on).  The
        # file only gets an audio track if the input really opened.
        audio = self._open_audio() if s.audio_enabled else None
        if audio is not None:
            comment += f" Audio: {audio.device.label}, {AUDIO_PLUG_LABELS[audio.plug]}, {audio.rate} Hz."
        recorder = RecordThread(
            path, std, on_finished=self.bridge.recording_finished.emit, comment=comment,
            audio_rate=audio.rate if audio else None, audio_channels=audio.channels if audio else 2,
            av_offset=s.av_sync_offset_ms / 1000,
        )
        recorder.start()
        recorder.opened.wait(5.0)
        if recorder.error or not recorder.opened.is_set():
            recorder.stop()
            if audio is not None:
                audio.stop()
            self._warn("Recording didn't start", recorder.error or "Timed out creating the file.")
            return

        self.recorder = recorder
        self._stop_reason = None
        self.capture.set_record_sink(recorder)
        if audio is not None:
            audio.sink = recorder.offer_audio
            self.audio = audio
            self._audio_progress = (audio.frames, time.monotonic())
            self._audio_warned = False
        self.record_panel.set_recording(True, path)
        self.record_panel.update_audio(None, "starting…" if audio else ("off" if not s.audio_enabled else "no input"))
        self.device_panel.set_device_controls_enabled(False, "Stop recording to change the device, input or standard.")
        log.info("● Recording to %s%s", path,
                 f" with sound from {audio.device.label}, {AUDIO_PLUG_LABELS[audio.plug]}" if audio else " (video only)")
        self._last_disk_check = 0.0
        self._tick()

    def _stop_recording(self, reason: str, unexpected: bool = False) -> None:
        recorder = self.recorder
        if recorder is None or self._recording_finishing:
            return
        if self.capture is not None:
            self.capture.set_record_sink(None)
        # The audio input keeps running: the recorder still needs the sound that's in
        # its buffers.  It's stopped in _on_recording_finished, once the file is closed.
        recorder.stop()
        self._recording_finishing = True
        self._stop_reason = reason if unexpected else None  # reported (in red) once the file is closed
        self.record_panel.set_recording(True, finishing=True)

    def _open_audio(self) -> AudioCapture | None:
        """Start the audio input.  Its samples go nowhere until a recorder takes them."""
        want = self.settings.audio_device
        for attempt in (1, 2):
            device = audio_io.find_input(want, self._audio_inputs)
            if device is not None:
                capture = AudioCapture(device, plug=self.settings.audio_plug)
                try:
                    capture.start()
                except AudioError as exc:
                    if attempt == 2:
                        log.error("Recording video only: %s", exc)
                        return None
                    log.info("Audio input didn't open (%s); re-scanning and trying again.", exc)
                else:
                    if want not in (AUTO, device.key):
                        log.warning("Audio input “%s” isn't connected; using %s instead.",
                                    want.split("::")[-1], device.label)
                    return capture
            elif attempt == 2:
                log.warning("Recording video only: no audio input found (is the Elgato plugged in?).")
                return None
            self._refresh_audio_inputs()  # the card may have been replugged since the last scan
        return None

    def _stop_audio(self) -> None:
        audio, self.audio = self.audio, None
        if audio is not None:
            audio.stop()
            if audio.overflows:
                log.warning("The audio input had to discard sound %d time(s) because the PC was too busy; "
                            "those moments are silent in the recording.", audio.overflows)

    def _update_audio_meter(self, now: float) -> None:
        audio = self.audio
        if audio is None:
            return
        level = audio.level_dbfs()
        self.record_panel.update_audio(level, "silence" if math.isinf(level) else f"{level:.0f} dB")
        frames, since = self._audio_progress
        if audio.frames != frames:
            self._audio_progress = (audio.frames, now)
        elif now - since > 2.0 and not self._audio_warned:
            self._audio_warned = True
            log.warning("No sound is arriving from %s; the recording continues without it.", audio.device.label)
        rate = audio.measured_rate()
        if rate and abs(rate / audio.rate - 1) > 0.01 and not self._audio_warned:
            self._audio_warned = True
            log.warning("The audio input delivers %.0f samples/s, not %d; sound may drift.", rate, audio.rate)

    @Slot(object)
    def _on_recording_finished(self, result: RecordingResult) -> None:
        self._stop_audio()
        recorder, self.recorder = self.recorder, None
        if recorder is not None:
            recorder.join(timeout=5.0)
        self._recording_finishing = False
        self.record_panel.set_recording(False)
        self.device_panel.set_device_controls_enabled(True)
        self.preview.rec_text = None

        size = result.path.stat().st_size if result.path.exists() else 0
        summary = (f"{result.path.name} — {format_duration(result.duration)}, "
                   f"{result.frames_written} frames, {format_bytes(size)}")
        if result.audio_seconds is not None:
            summary += f", sound {format_duration(result.audio_seconds)}"
            if result.audio_gaps:
                log.warning("%d gap(s) in the sound of %s were filled with silence.", result.audio_gaps,
                            result.path.name)
            log.debug("Audio sync: %d single-sample adjustments in %s", result.audio_adjustments, result.path.name)
        if result.frames_dropped:
            log.error("%d frames were LOST from %s because the disk couldn't keep up.",
                      result.frames_dropped, result.path.name)
        if result.device_gaps:
            log.warning("The capture device skipped %d frames during %s (kept as timing gaps).",
                        result.device_gaps, result.path.name)
        if result.error:
            self.record_panel.show_banner(result.error, "error")
            self._warn("Recording problem", f"{result.error}\n\n{summary}\n\nFolder: {result.path.parent}",
                       logging.ERROR)
        elif self._stop_reason:
            self.record_panel.show_banner(self._stop_reason, "error")
            self._warn("Recording stopped", f"{self._stop_reason}\n\nSaved: {summary}", logging.ERROR)
        else:
            log.info("■ Saved %s", summary)
            if self.settings.export_after_recording and result.frames_written:
                self.exports.add([result.path], self.settings.field_order)
        self._update_hud()

    def _tick(self) -> None:
        """Twice a second: recording numbers, free disk space, low-space protection."""
        s = self.settings
        now = time.monotonic()
        recorder = self.recorder
        rate: float | None = None
        if recorder is not None and not self._recording_finishing:
            size = recorder.file_size()
            duration = recorder.duration
            rate = size / duration if duration >= 2 else None
            self.record_panel.update_recording(duration, size, rate, recorder.frames_dropped, recorder.device_gaps)
            self._update_audio_meter(now)
            lost = f"   ⚠ {recorder.frames_dropped} LOST" if recorder.frames_dropped else ""
            self.preview.rec_text = f"● REC  {format_duration(duration)}   {format_bytes(size)}{lost}"
            self.preview.update()

        if recorder is None and now - self._last_disk_check < 5.0:
            return
        self._last_disk_check = now
        free = free_disk_bytes(s.output_dir)
        typical = TYPICAL_GB_PER_HOUR * GB / 3600
        seconds_left = free / (rate or typical) if free is not None else None
        self.record_panel.update_disk(free, seconds_left)
        if free is None:
            return
        if recorder is not None and not self._recording_finishing and free < s.low_disk_stop_gb * GB:
            self._stop_recording(
                f"The recordings drive is almost full ({format_bytes(free)} left), so recording was stopped "
                "while the file can still be finalised properly.", unexpected=True)
        elif free < s.low_disk_warning_gb * GB:
            text = f"Low disk space: {format_bytes(free)} left ({format_time_left(seconds_left)} of recording)."
            if recorder is not None or not self._recording_finishing:
                self.record_panel.show_banner(text, "warn")
            if not self._disk_warned:
                log.warning(text)
                self._disk_warned = True
        elif recorder is None:
            self._disk_warned = False

    # ======================================================================
    # User changes (device panel, record panel, view menu)
    # ======================================================================

    def _on_video_device_selected(self, name: str) -> None:
        if not name or name == self.settings.video_device:
            return
        self.settings.video_device = name
        self._close_controls()
        self._restart_capture(f"Switching to “{name}”.")

    def _on_video_input_selected(self, key: str) -> None:
        if key == self.settings.video_input:
            return
        self.settings.video_input = key
        self._restart_capture(f"Switching the input to {VIDEO_INPUT_LABELS[key]}.")

    def _on_standard_selected(self, key: str) -> None:
        if key == self.settings.video_standard:
            return
        current, new = STANDARDS[self.settings.video_standard], STANDARDS[key]
        if self._capture_state == CaptureState.RUNNING and self._signal_locked:
            # A locked picture means the source really is the current standard.  Leaving
            # it loses the picture, and closing a mismatched stream later is exactly
            # what hung this driver (PAL stream + NTSC camera).  So ask first.
            answer = QMessageBox.question(
                self, "Switch TV standard?",
                f"The card is locked to a {current.key} picture right now, so your source is {current.key}.\n\n"
                f"Switching to {new.key} will lose the picture. With this driver, switching back afterwards "
                f"can also freeze it until you unplug and replug the Elgato.\n\nSwitch to {new.key} anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.device_panel.select_standard(self.settings.video_standard)
                return
        self.settings.video_standard = key
        self._restart_capture(f"Switching the video standard to {STANDARDS[key].name}.")

    def _on_audio_device_selected(self, key: str) -> None:
        self.settings.audio_device = key or AUTO

    def _on_audio_plug_selected(self, key: str) -> None:
        self.settings.audio_plug = key

    def _on_record_audio_toggled(self, on: bool) -> None:
        self.settings.audio_enabled = on

    def _on_output_dir_changed(self, folder: str) -> None:
        self.settings.output_dir = folder
        self._last_disk_check = 0.0
        log.info("Recordings will be saved in %s", folder)
        self._tick()

    def _on_prefix_changed(self, prefix: str) -> None:
        self.settings.filename_prefix = prefix or "capture"
        self.preview.setFocus()

    def _open_output_folder(self) -> None:
        folder = Path(self.settings.output_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Can't open %s: %s", folder, exc)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _set_overlay(self, name: str, on: bool) -> None:
        setattr(self.preview.overlays, name, on)
        setattr(self.settings, OVERLAY_SETTINGS[name], on)
        self.preview.update()

    def _set_deinterlace(self, mode: str) -> None:
        self.settings.deinterlace = mode
        self.deint_actions[mode].setChecked(True)
        self.statusBar().showMessage(f"Deinterlace: {DEINTERLACE_LABELS[mode]}", 2500)
        frame = self._shown or self._latest
        if frame is not None:
            self._display(frame)  # re-render now, even when frozen (handy for comparing modes)
        self._update_hud()

    def _cycle_deinterlace(self) -> None:
        modes = DEINTERLACE_MODES
        self._set_deinterlace(modes[(modes.index(self.settings.deinterlace) + 1) % len(modes)])

    def _set_field_order(self, key: str) -> None:
        self.settings.field_order = key
        self.statusBar().showMessage(f"Bob field order: {FIELD_ORDER_LABELS[key]}", 2500)

    def _field_order(self) -> str:
        if self.settings.field_order != "auto":
            return self.settings.field_order
        return STANDARDS[self.settings.video_standard].field_order

    def _set_fit(self, on: bool) -> None:
        self.settings.fit_to_window = on
        self.preview.set_fit(on)
        self._update_hud()

    def _set_correct_aspect(self, on: bool) -> None:
        self.settings.correct_aspect = on
        self.preview.set_correct_aspect(on)
        self._update_hud()

    def _set_hud(self, on: bool) -> None:
        self.settings.show_hud = on
        self.preview.hud_enabled = on
        self.preview.update()

    def _set_frozen(self, on: bool) -> None:
        self._frozen = on
        self.preview.frozen = on
        if not on and self._latest is not None:
            self._display(self._latest)
        self.preview.update()

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _escape(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        self.preview.setFocus()

    # ======================================================================
    # Status display
    # ======================================================================

    def _update_status_bar(self) -> None:
        color = _STATE_COLORS.get(self._capture_state, MUTED)
        self.status_state.setText(f'<span style="color:{color}">●</span> {self._capture_state.value}')
        stats = self._stats
        running = self._capture_state == CaptureState.RUNNING
        self.status_fps.setText(f"{stats.fps:.2f} fps" if stats and running else "")
        lag = self.preview.lag_ms
        self.status_lag.setText(f"display lag {lag:.0f} ms" if running and lag is not None and not self._frozen else "")
        self.status_drops.setText(
            f'<span style="color:{WARN_AMBER}">device skipped {stats.device_drops} frames</span>'
            if stats and stats.device_drops else ""
        )

    def _update_hud(self) -> None:
        s = self.settings
        std: VideoStandard = STANDARDS[s.video_standard]
        stats = self._stats
        if self._capture_state == CaptureState.RUNNING:
            fps = f"{stats.fps:5.2f} fps" if stats and stats.fps else "  —  fps"
            line1 = f"● LIVE   {fps}   {std.key} {std.width}×{std.height}   {VIDEO_INPUT_LABELS[s.video_input]}"
        else:
            line1 = f"○ {self._capture_state.value.upper()}"
        view = "1:1 pixels" if not s.fit_to_window else ("fit · true 4:3" if s.correct_aspect else "fit · square pixels")
        parts = [view, DEINTERLACE_SHORT[s.deinterlace]]
        if stats and stats.device_drops:
            parts.append(f"device skipped {stats.device_drops} frames")
        self.preview.hud_lines = [line1, "   ·   ".join(parts)]

        off = self.device_panel.off_neutral()
        self.preview.banner = (
            (f"PROC AMP NOT NEUTRAL — {self._describe_off_neutral(off)}\n"
             "You're seeing the card's correction, not just the camera.", "warn") if off else None
        )

        if self._capture_state in (CaptureState.WAITING, CaptureState.FAILED):
            center = textwrap.fill(self._capture_message, 64)
        elif self._capture_state == CaptureState.OPENING and not self.preview.has_image():
            center = "Opening the capture device…"
        elif self._capture_state == CaptureState.RUNNING and self._signal_locked is False:
            center = ("NO SIGNAL\nThe card isn't locked to a picture.\n"
                      f"Is the camera on and plugged into the {VIDEO_INPUT_LABELS[s.video_input]} input?")
        else:
            center = None
        self.preview.center_message = center
        self.preview.update()

    def _warn(self, title: str, text: str, level: int = logging.WARNING) -> None:
        """A non-modal warning: it never blocks capture or recording."""
        log.log(level, "%s: %s", title, " ".join(text.split()))
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, self)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.show()

    def _show_shortcuts(self) -> None:
        rows = [
            ("R", "Start / stop recording"),
            ("Space", "Freeze / unfreeze the preview (recording continues)"),
            ("G", "Centering grid"),
            ("C", "Center crosshair"),
            ("S", "Safe areas"),
            ("L", "Luma staircase reference"),
            ("D", "Deinterlace: off → bob → blend (preview only)"),
            ("F", "Fit to window ↔ 1:1 pixels (or double-click the picture)"),
            ("A", "Correct pixel aspect (true 4:3)"),
            ("I", "On-screen info"),
            ("F11 / Esc", "Full screen / leave full screen"),
            ("Ctrl+R", "Reconnect to the device now"),
            ("Ctrl+O", "Open the recordings folder"),
            ("Ctrl+E", "Export MP4 viewing copies of recordings"),
            ("Ctrl+1 / 2 / 3", "Show or hide the Device, Recording and Log panels"),
            ("Ctrl+4", "Show the pot meter, or hide it"),
            ("Ctrl+5", "Show the white meter, or hide it"),
            ("Ctrl+Q", "Quit"),
        ]
        table = "".join(f"<tr><td style='padding-right:14px'><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows)
        QMessageBox.information(
            self, "Keyboard shortcuts",
            f"<table>{table}</table><p style='color:#8c8c8c'>Coming in Phase 2: V vectorscope, "
            "W waveform, H hold reading.</p>",
        )

    def _show_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {__version__}<br>Analog capture and calibration for a 1984 RCA CKC021."
            f"<br><br>PyAV {av.__version__} · FFmpeg {getattr(av, 'ffmpeg_version_info', '?')} · Qt {qVersion()}"
            f"<br>Settings: settings.json · Log: {self._log_dir}",
        )

    # ======================================================================
    # Pot meter (Phase 2: the analysis thread does the measuring)
    # ======================================================================

    def _on_pot_status(self, status: PotStatus) -> None:
        self.pot_panel.show_status(status)

    def _choose_reference_photo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a phone photo of the white card", str(Path.home() / "Pictures"),
            "Photos (*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff);;All files (*)",
        )
        if path:
            self._use_reference_photo(Path(path))

    def _use_reference_photo(self, path: Path, quiet: bool = False) -> None:
        """Zero the pot meter on this photo: it aims for how the card really looks, not plain white."""
        try:
            reference = reference_from_rgb(load_photo(path))
        except (OSError, ValueError) as exc:
            if quiet:  # at start-up: the remembered photo has gone or changed
                log.warning("The pot meter's photo %s can't be used any more (%s); aiming for plain white.",
                            path, exc)
            else:
                self._warn("Couldn't use that photo", f"{path.name}: {exc}.")
            self._clear_reference_photo()
            return
        self.settings.pot_reference_photo = str(path)
        self.analysis.set_reference(reference)
        self.pot_panel.show_reference(path.name, reference)
        log.info("Pot meter: aiming for how the card looks in %s, instead of plain white.", path.name)

    def _clear_reference_photo(self) -> None:
        self.settings.pot_reference_photo = ""
        self.analysis.set_reference(None)
        self.pot_panel.show_reference(None, None)

    def _on_pot_grid_toggled(self, on: bool) -> None:
        self.settings.show_pot_grid = on
        self._update_pot_grid()

    def _on_pot_dock_visibility(self, visible: bool) -> None:
        # Qt's isVisible() stays True for a dock hidden behind another tab, so "on
        # screen" is tracked from this signal instead.  It fires whenever a tab is
        # chosen or the dock is shown or hidden, and with the starting state when
        # the window first opens.
        self._pot_on_screen = visible
        self.analysis.set_pot_enabled(visible)  # measure only while the panel is on screen
        self._update_pot_grid()

    def _update_pot_grid(self) -> None:
        self.preview.overlays.pot_grid = self.settings.show_pot_grid and self._pot_on_screen
        self.preview.update()

    def _toggle_pot_dock(self) -> None:
        if self._pot_on_screen:
            self.pot_dock.hide()
        else:
            self.pot_dock.show()
            self.pot_dock.raise_()

    # ======================================================================
    # White meter (Phase 2: the analysis thread does the measuring)
    # ======================================================================

    def _on_white_reading(self, reading: WhiteReading) -> None:
        self.white_panel.show_reading(reading)

    def _on_white_dock_visibility(self, visible: bool) -> None:
        self._white_on_screen = visible  # tracked from this signal, like the pot meter's (see there)
        self.analysis.set_white_enabled(visible)  # measure only while the panel is on screen

    def _toggle_white_dock(self) -> None:
        if self._white_on_screen:
            self.white_dock.hide()
        else:
            self.white_dock.show()
            self.white_dock.raise_()

    # ======================================================================
    # MP4 viewing copies (export.py does the work, in a child process)
    # ======================================================================

    def _choose_exports(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose recordings to export as MP4", self.settings.output_dir, "Recordings (*.mkv)"
        )
        if paths:
            self.exports.add([Path(p) for p in paths], self.settings.field_order)

    def _on_export_after_toggled(self, on: bool) -> None:
        self.settings.export_after_recording = on

    def _on_export_finished(self, source: Path, ok: bool, message: str) -> None:
        if not ok and message:  # a cancelled export isn't a problem
            self._warn("Export failed", f"No MP4 was made from {source.name}: {message}", logging.ERROR)

    # ======================================================================
    # Shutdown
    # ======================================================================

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.recorder is not None and not self._recording_finishing:
            answer = QMessageBox.question(
                self, "Recording in progress", "A recording is in progress.\n\nStop it, save the file and quit?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self.exports.running:
            answer = QMessageBox.question(
                self, "Export in progress",
                f"An MP4 viewing copy is still being made ({self.exports.describe()}).\n\n"
                "Stop it and quit? The unfinished copy is deleted. The recording itself is safe, and you can "
                "export it again later (Capture → Export MP4 viewing copies).",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.exports.cancel_all(wait=True)
        if self.screenshot_on_close is not None:
            self.grab().save(str(self.screenshot_on_close))
            log.info("Screenshot saved to %s", self.screenshot_on_close)
        self.tick_timer.stop()
        self.signal_timer.stop()
        if self.preview.lag_ms is not None:
            log.info("Display lag this session (frame off the card → drawn): %.0f ms", self.preview.lag_ms)
        recorder = self.recorder
        if recorder is not None:
            if self.capture is not None:
                self.capture.set_record_sink(None)
            recorder.stop()
            recorder.join(timeout=30.0)
            log.info("Saved %s", recorder.path)
            self.recorder = None
        self._stop_audio()  # after the recorder: it takes the sound still in the buffers
        self.analysis.stop()
        self.analysis.join(timeout=2.0)
        self.stuck_timer.stop()
        if not self._stop_capture() or self._stuck_capture is not None:
            log.warning("The Elgato's driver is stuck, so Windows can't finish closing the app until you "
                        "unplug the Elgato (or restart the PC).")
        self._close_controls()
        self._save_layout()
        try:
            save_settings(self.settings)
        except OSError as exc:
            log.error("Could not save settings: %s", exc)
        logging.getLogger().removeHandler(self._log_handler)
        event.accept()
