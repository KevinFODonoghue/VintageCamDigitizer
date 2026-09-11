"""
Turning FFmpeg/DirectShow failures into messages a person can act on.

FFmpeg reports almost every device problem as a bare "Input/output error" and
puts the real reason in its log.  So while opening a device we capture FFmpeg's
log lines and look for known phrases.  The phrases are FFmpeg's own wording
(libavdevice/dshow.c), each one observed on the target machine.
"""

from __future__ import annotations


class CaptureError(Exception):
    """A capture problem with a message fit for the UI."""

    title = "Capture error"
    #: Seconds to wait before retrying automatically, or None to wait for the user.
    retry_after: float | None = 5.0

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class DeviceBusyError(CaptureError):
    title = "Device in use"
    retry_after = 3.0


class DeviceNotFoundError(CaptureError):
    title = "Device not found"
    retry_after = 1.5


class DeviceLostError(CaptureError):
    title = "Device disconnected"
    retry_after = 1.0


class UnsupportedFormatError(CaptureError):
    title = "Format not supported"
    #: Usually the user has to change a setting, so retrying soon is pointless.  But
    #: a card that was just plugged back in can refuse briefly while its driver
    #: starts up, so keep retrying slowly rather than never: reconnection must
    #: work without anyone touching the keyboard.
    retry_after = 10.0


def classify_open_error(
    exc: BaseException, log_lines: list[str], device: str, what: str = "video"
) -> CaptureError:
    """Map a failed ``av.open`` on a dshow device to a specific CaptureError."""
    # Look in the exception text too: PyAV appends FFmpeg's last error line to it
    # ("…; last error log: [dshow] Could not run graph…"), which still works when
    # the log capture came back empty.
    text = "\n".join([*log_lines, str(exc)])
    detail = "\n".join(dict.fromkeys(filter(None, [str(exc), *log_lines])))

    # Order matters: FFmpeg prints several of these phrases for one failure.
    if "Could not run graph" in text or "already in use" in text:
        return DeviceBusyError(
            f"“{device}” is in use by another program. Close anything else using the card "
            "(an ffplay window, OBS, Elgato Game Capture, the Windows Camera app…). "
            "Capture will resume automatically.",
            detail,
        )
    if "Could not set" in text and "options" in text:
        return UnsupportedFormatError(
            f"“{device}” refused the requested {what} format. "
            "Check the video standard (NTSC/PAL) and that the right device is selected.",
            detail,
        )
    if "Could not find output pin" in text or "Could not RenderStream" in text:
        return UnsupportedFormatError(
            f"“{device}” opened, but its {what} output could not be connected.", detail
        )
    if "Could not find" in text and "device with name" in text:
        return DeviceNotFoundError(
            f"“{device}” was not found. Is it plugged in? Waiting for it to appear…", detail
        )
    return CaptureError(f"Could not open “{device}”: {exc}", detail)
