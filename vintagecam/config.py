"""
Settings: device names, capture defaults, output paths and UI toggles.

Stored as ``settings.json`` next to ``main.py`` (git-ignored — it holds paths that
only make sense on this laptop).  Delete it any time to get the defaults back.

Why a JSON file rather than QSettings (which would use the Windows registry)?
You can open JSON in a text editor, see exactly what the app remembered, and fix
or back it up by hand.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .video_format import STANDARDS

log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Hardware constants, verified on the target machine (CLAUDE_CODE_PROMPT.md).
# -----------------------------------------------------------------------------

#: Exact DirectShow "friendly name".  FFmpeg finds devices by this string, so a
#: single wrong character means "device not found".
DEFAULT_VIDEO_DEVICE = "Elgato Video Capture"
#: DirectShow's name for the card's audio.  That route doesn't work on this PC
#: (audio.py explains); kept only to recognise it in older settings files.
DEFAULT_AUDIO_DEVICE = "Analog Audio In (Elgato Video Capture)"

#: Settings from earlier versions that are now ignored without a warning.
_OBSOLETE_SETTINGS = {"record_audio"}

#: Crossbar input pins.  A crossbar is the switch inside the card that picks which
#: physical connector feeds the decoder chip.  The numbers come from
#: ``ffmpeg -f dshow -list_options true -i video="Elgato Video Capture"``.
CROSSBAR_VIDEO_PINS: dict[str, int] = {"composite": 0, "svideo": 1}
VIDEO_INPUT_LABELS: dict[str, str] = {"composite": "Composite", "svideo": "S-Video"}

#: The crossbar's "Audio Decoder" output can only take pin 2 ("Audio Line").  On
#: this machine it was found *unrouted* (-1) after a replug, which leaves the
#: card's audio path dead, so we route it explicitly every time we open.
CROSSBAR_AUDIO_LINE_PIN = 2

#: The Elgato's two RCA audio plugs are the two channels of its stereo input, with
#: the usual colour code: white = left (channel 0), red = right (channel 1).
#: Stereo (both plugs) is the default; the mono choices record one plug's channel
#: on its own.  (With the RCA camera's lead in the red jack alone, the Elgato
#: delivers its sound on both channels, equally: measured 2026-09-11.)
AUDIO_PLUGS: dict[str, tuple[int, ...]] = {"both": (0, 1), "red": (1,), "white": (0,)}
AUDIO_PLUG_LABELS: dict[str, str] = {
    "both": "Red + white (stereo)",
    "red": "Red plug only (mono)",
    "white": "White plug only (mono)",
}

DEINTERLACE_MODES = ("off", "bob", "blend")
FIELD_ORDERS = ("auto", "tff", "bff")

#: FFV1 at 720×480 4:2:2 runs roughly 30–45 GB per hour; noise and fine detail
#: compress worse.  Used for "time left on disk" until a recording has measured
#: its own data rate.
TYPICAL_GB_PER_HOUR = 40.0


def _default_output_dir() -> str:
    return str(Path.home() / "Videos" / "VintageCam")


def app_dir() -> Path:
    """Folder holding settings.json and logs/.

    From source this is the repository root (the folder with main.py).  When
    packaged with PyInstaller (Phase 3) it's the folder containing the .exe —
    PyInstaller sets ``sys.frozen``.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def settings_path() -> Path:
    return app_dir() / "settings.json"


@dataclass
class Settings:
    """Everything the app remembers between runs.  The field defaults ARE the defaults."""

    # --- device ---------------------------------------------------------------
    video_device: str = DEFAULT_VIDEO_DEVICE
    #: "auto" = the Elgato's line input; otherwise an audio.AudioInput key.
    audio_device: str = "auto"
    #: Which of the Elgato's audio plugs to record: a key of AUDIO_PLUGS.
    audio_plug: str = "both"
    video_input: str = "composite"  # key of CROSSBAR_VIDEO_PINS
    video_standard: str = "NTSC"  # key of video_format.STANDARDS
    #: FFmpeg's real-time buffer: how much video DirectShow may queue while the
    #: capture thread is momentarily busy.  Large, so a hiccup never costs a
    #: recorded frame.  Matches the proven command line.
    rtbufsize: str = "512M"

    # --- recording ------------------------------------------------------------
    output_dir: str = field(default_factory=_default_output_dir)
    filename_prefix: str = "capture"
    audio_enabled: bool = True  # record 48 kHz PCM with the video when an audio input works
    #: Nudge the sound relative to the picture: positive = sound later (ms).
    av_sync_offset_ms: float = 0.0
    low_disk_warning_gb: float = 20.0  # warn below this much free space
    low_disk_stop_gb: float = 2.0  # stop recording cleanly below this
    #: Also make an MP4 viewing copy (export.py) after each recording.
    export_after_recording: bool = True

    # --- preview --------------------------------------------------------------
    show_grid: bool = True  # on by default, like the old ffplay drawgrid/drawbox overlay
    show_crosshair: bool = True
    show_safe_areas: bool = False
    show_staircase: bool = False
    show_hud: bool = True
    deinterlace: str = "off"  # one of DEINTERLACE_MODES
    field_order: str = "auto"  # one of FIELD_ORDERS; "auto" = the standard's usual order
    fit_to_window: bool = True
    correct_aspect: bool = True

    # --- window layout (Qt saveGeometry/saveState, base64) ---------------------
    window_geometry: str = ""
    window_state: str = ""

    def validate(self) -> list[str]:
        """Repair out-of-range values in place; return one message per repair."""
        problems: list[str] = []
        defaults = Settings()

        def reset(name: str, why: str) -> None:
            problems.append(f"settings.json: {name} {why}; using {getattr(defaults, name)!r}")
            setattr(self, name, getattr(defaults, name))

        if self.video_input not in CROSSBAR_VIDEO_PINS:
            reset("video_input", f"{self.video_input!r} is not one of {list(CROSSBAR_VIDEO_PINS)}")
        if self.audio_plug not in AUDIO_PLUGS:
            reset("audio_plug", f"{self.audio_plug!r} is not one of {list(AUDIO_PLUGS)}")
        if self.video_standard not in STANDARDS:
            reset("video_standard", f"{self.video_standard!r} is not one of {list(STANDARDS)}")
        if self.deinterlace not in DEINTERLACE_MODES:
            reset("deinterlace", f"{self.deinterlace!r} is not one of {list(DEINTERLACE_MODES)}")
        if self.field_order not in FIELD_ORDERS:
            reset("field_order", f"{self.field_order!r} is not one of {list(FIELD_ORDERS)}")
        if not self.video_device.strip():
            reset("video_device", "is empty")
        if not self.output_dir.strip():
            reset("output_dir", "is empty")
        if self.audio_device in ("", DEFAULT_AUDIO_DEVICE):  # the old, unusable DirectShow route
            self.audio_device = "auto"
        if not (self.low_disk_warning_gb >= self.low_disk_stop_gb >= 0):
            reset("low_disk_warning_gb", "must be >= low_disk_stop_gb >= 0")
            reset("low_disk_stop_gb", "must be >= 0")
        return problems


def load_settings(path: Path | None = None) -> tuple[Settings, list[str]]:
    """Read settings.json.  Never raises: a missing or broken file gives defaults.

    Returns the settings plus a human-readable warning for anything that had to
    be ignored, so the UI can show it.  (A silently ignored setting is the kind
    of bug you spend an evening hunting.)
    """
    path = path or settings_path()
    warnings: list[str] = []
    if not path.exists():
        return Settings(), warnings

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("the top level is not a JSON object")
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        backup = path.with_suffix(".json.bad")
        try:
            path.replace(backup)
            where = f"moved it to {backup.name}"
        except OSError:
            where = "left it in place"
        warnings.append(f"Could not read {path.name} ({exc}); {where} and started with defaults.")
        return Settings(), warnings

    settings = Settings()
    known = {f.name for f in fields(Settings)}
    for key, value in raw.items():
        if key in _OBSOLETE_SETTINGS:
            continue
        if key not in known:
            warnings.append(f"settings.json: ignoring unknown setting {key!r}")
            continue
        default = getattr(settings, key)
        # bool is a subclass of int in Python, so it has to be checked first.
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if ok else value
        else:
            ok = isinstance(value, type(default))
        if not ok:
            warnings.append(
                f"settings.json: {key} should be {type(default).__name__}, got {value!r}; using default"
            )
            continue
        setattr(settings, key, value)

    warnings += settings.validate()
    return settings, warnings


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write settings.json atomically: write a temp file, then rename over the old one.

    If the app crashes halfway through writing, you keep the previous settings
    instead of a half-written (unreadable) file.
    """
    path = path or settings_path()
    text = json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
