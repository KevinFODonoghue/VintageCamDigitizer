"""
Talking directly to the capture card's DirectShow driver: the device list, the
proc amp, and the decoder's TV standard and signal-lock status.

**Why this exists.**  FFmpeg (and so PyAV) can open the Elgato and switch its
crossbar, but it has no way to read or set the card's *proc amp* (brightness,
contrast, hue, saturation) or its *video standard*.  Those sit behind two
DirectShow COM interfaces, IAMVideoProcAmp and IAMAnalogVideoDecoder, so we call
them ourselves.

**How.**  A COM object is a C++-style object whose first field points at a table
of function pointers (its "vtable").  Python's built-in ``ctypes`` can call
through that table if we tell it each method's position and argument types,
which come from the Windows SDK header ``strmif.h``.  That's all the code below
does, so no extra packages are needed.

**Two filters, one device.**  PyAV gives us no handle on the DirectShow filter it
streams from, so this module creates a *second* filter object for the same
device, just for property access.  Verified on the target machine: the Elgato
driver allows that (proc amp and lock status were read while ffplay was
streaming).  What it refuses is a second *stream*.

**Threads.**  COM objects belong to the thread that created them.  Create and use
a ``VideoDeviceControls`` on one thread only — the GUI thread in this app.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import HRESULT, POINTER, WINFUNCTYPE, byref, c_long, c_ulong, c_void_p
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache

log = logging.getLogger(__name__)


class DShowError(Exception):
    """A DirectShow call failed.  ``hresult`` is the Windows error code, if any."""

    def __init__(self, what: str, hresult: int | None = None) -> None:
        self.hresult = None if hresult is None else hresult & 0xFFFFFFFF
        code = f" (0x{self.hresult:08X})" if self.hresult is not None else ""
        super().__init__(what + code)

    @property
    def unsupported(self) -> bool:
        """True if the driver simply doesn't implement the thing we asked for."""
        # 0x80070490 = "element not found" (how KS drivers say "no such property"),
        # 0x80004001 = E_NOTIMPL, 0x80004002 = E_NOINTERFACE.
        return self.hresult in (0x80070490, 0x80004001, 0x80004002)


# ---------------------------------------------------------------------------
# Low-level COM plumbing
# ---------------------------------------------------------------------------


class GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, text: str) -> GUID:
        guid = cls()
        ctypes.memmove(byref(guid), uuid.UUID(text).bytes_le, 16)
        return guid


class VARIANT(ctypes.Structure):
    """Just enough of the COM VARIANT to read a string (24 bytes on 64-bit)."""

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("p1", c_void_p),
        ("p2", c_void_p),
    ]


_VT_BSTR = 8
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_RPC_E_CHANGED_MODE = 0x80010106
_VIDEOPROCAMP_FLAGS_AUTO = 0x1
_VIDEOPROCAMP_FLAGS_MANUAL = 0x2

_ole32 = ctypes.OleDLL("ole32")  # OleDLL: failing HRESULTs raise OSError for us
_ole32.CoInitializeEx.argtypes = [c_void_p, ctypes.c_uint32]
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None  # returns void; don't treat garbage as an HRESULT
_ole32.CoCreateInstance.argtypes = [
    POINTER(GUID), c_void_p, ctypes.c_uint32, POINTER(GUID), POINTER(c_void_p)
]
_oleaut32 = ctypes.WinDLL("oleaut32")
_oleaut32.VariantClear.argtypes = [POINTER(VARIANT)]
_oleaut32.VariantClear.restype = c_long

# Class and interface ids (strmif.h / uuids.h).
CLSID_SystemDeviceEnum = GUID.from_str("62BE5D10-60EB-11d0-BD3B-00A0C911CE86")
CLSID_VideoInputDeviceCategory = GUID.from_str("860BB310-5D01-11d0-BD3B-00A0C911CE86")
CLSID_AudioInputDeviceCategory = GUID.from_str("33D9A762-90C8-11d0-BD43-00A0C911CE86")
IID_ICreateDevEnum = GUID.from_str("29840822-5B84-11D0-BD3B-00A0C911CE86")
IID_IPropertyBag = GUID.from_str("55272A00-42CB-11CE-8135-00AA004BB851")
IID_IBaseFilter = GUID.from_str("56a86895-0ad4-11ce-b03a-0020af0ba770")
IID_IAMVideoProcAmp = GUID.from_str("C6E13360-30AC-11d0-A18C-00A0C9118956")
IID_IAMAnalogVideoDecoder = GUID.from_str("C6E13350-30AC-11d0-A18C-00A0C9118956")


@lru_cache(maxsize=None)
def _prototype(argtypes: tuple) -> type:
    """A ctypes function type for a COM method: HRESULT method(this, *argtypes)."""
    return WINFUNCTYPE(HRESULT, c_void_p, *argtypes)


_Release = WINFUNCTYPE(c_ulong, c_void_p)


class _ComPtr:
    """Owns one reference to a COM interface and calls its methods by vtable slot.

    Slot numbers count from the start of the vtable: 0–2 are IUnknown's
    QueryInterface/AddRef/Release, then each interface's own methods in the
    order they're declared in strmif.h.
    """

    __slots__ = ("_ptr", "name")

    def __init__(self, ptr: c_void_p, name: str) -> None:
        self._ptr = ptr
        self.name = name

    def __bool__(self) -> bool:
        return bool(self._ptr)

    def call(self, slot: int, argtypes: tuple, *args: object, what: str = "") -> int:
        if not self._ptr:
            raise DShowError(f"{self.name} has already been released")
        vtable = ctypes.cast(self._ptr, POINTER(POINTER(c_void_p)))[0]
        try:
            return _prototype(argtypes)(vtable[slot])(self._ptr, *args)
        except OSError as exc:
            raise DShowError(what or f"{self.name} method {slot} failed", exc.winerror) from None

    def query(self, iid: GUID, name: str) -> _ComPtr:
        out = c_void_p()
        self.call(0, (POINTER(GUID), POINTER(c_void_p)), byref(iid), byref(out), what=f"{name} not available")
        return _ComPtr(out, name)

    def release(self) -> None:
        if self._ptr:
            vtable = ctypes.cast(self._ptr, POINTER(POINTER(c_void_p)))[0]
            _Release(vtable[2])(self._ptr)
            self._ptr = c_void_p()

    def __enter__(self) -> _ComPtr:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _co_initialize() -> bool:
    """Initialise COM on this thread.  Returns True if a matching CoUninitialize is owed.

    Uses the "apartment-threaded" model — the same one FFmpeg's dshow code and Qt
    use — so their COM calls and ours follow the same rules on a given thread.
    """
    try:
        _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)  # S_OK, or S_FALSE if already done
        return True
    except OSError as exc:
        if (exc.winerror & 0xFFFFFFFF) == _RPC_E_CHANGED_MODE:
            return False  # thread already uses the other COM model; our calls still work
        raise DShowError("CoInitializeEx failed", exc.winerror) from None


@contextmanager
def com_initialized() -> Iterator[None]:
    """Keep COM initialised on the current thread for the duration of the block."""
    owed = _co_initialize()
    try:
        yield
    finally:
        if owed:
            _ole32.CoUninitialize()


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    """DirectShow friendly name — the string FFmpeg's ``video=``/``audio=`` wants."""
    index: int
    """Position among devices with the same name (FFmpeg's ``video_device_number``)."""
    path: str | None
    """Driver device path: unique per physical device and USB port.  None for
    most audio devices, which don't report one."""


def _read_string(bag: _ComPtr, prop: str) -> str | None:
    var = VARIANT()
    try:
        bag.call(3, (ctypes.c_wchar_p, POINTER(VARIANT), c_void_p), prop, byref(var), None)
    except DShowError:
        return None  # e.g. audio devices have no "DevicePath"
    try:
        return ctypes.wstring_at(var.p1) if var.vt == _VT_BSTR and var.p1 else None
    finally:
        _oleaut32.VariantClear(byref(var))


def _enumerate(category: GUID) -> list[tuple[DeviceInfo, _ComPtr]]:
    """(info, moniker) for every device in a category.  Caller releases the monikers."""
    raw = c_void_p()
    try:
        _ole32.CoCreateInstance(
            byref(CLSID_SystemDeviceEnum), None, _CLSCTX_INPROC_SERVER, byref(IID_ICreateDevEnum), byref(raw)
        )
    except OSError as exc:
        raise DShowError("Could not create the DirectShow device enumerator", exc.winerror) from None

    enum_raw = c_void_p()
    with _ComPtr(raw, "ICreateDevEnum") as devenum:
        # ICreateDevEnum::CreateClassEnumerator (slot 3).  Returns S_FALSE and a
        # NULL enumerator when the category is simply empty.
        devenum.call(
            3, (POINTER(GUID), POINTER(c_void_p), ctypes.c_uint32), byref(category), byref(enum_raw), 0,
            what="CreateClassEnumerator failed",
        )
    results: list[tuple[DeviceInfo, _ComPtr]] = []
    if not enum_raw:
        return results

    seen: dict[str, int] = {}
    with _ComPtr(enum_raw, "IEnumMoniker") as enum:
        while True:
            mon_raw, fetched = c_void_p(), c_ulong()
            hr = enum.call(3, (c_ulong, POINTER(c_void_p), POINTER(c_ulong)), 1, byref(mon_raw), byref(fetched))
            if hr != 0 or not mon_raw:
                break
            moniker = _ComPtr(mon_raw, "IMoniker")
            bag_raw = c_void_p()
            try:
                # IMoniker::BindToStorage (slot 9) -> the device's property bag.
                moniker.call(
                    9, (c_void_p, c_void_p, POINTER(GUID), POINTER(c_void_p)),
                    None, None, byref(IID_IPropertyBag), byref(bag_raw),
                )
                with _ComPtr(bag_raw, "IPropertyBag") as bag:
                    name, path = _read_string(bag, "FriendlyName"), _read_string(bag, "DevicePath")
            except DShowError as exc:
                log.debug("Skipping a device whose properties can't be read: %s", exc)
                moniker.release()
                continue
            if not name:
                moniker.release()
                continue
            index = seen.get(name, 0)
            seen[name] = index + 1
            results.append((DeviceInfo(name, index, path), moniker))
    return results


def _list(category: GUID) -> list[DeviceInfo]:
    with com_initialized():
        pairs = _enumerate(category)
        for _, moniker in pairs:
            moniker.release()
    return [info for info, _ in pairs]


def list_video_devices() -> list[DeviceInfo]:
    """DirectShow video capture devices, in the order FFmpeg sees them."""
    return _list(CLSID_VideoInputDeviceCategory)


def list_audio_devices() -> list[DeviceInfo]:
    """DirectShow audio capture devices, in the order FFmpeg sees them."""
    return _list(CLSID_AudioInputDeviceCategory)


# ---------------------------------------------------------------------------
# Proc amp and decoder
# ---------------------------------------------------------------------------


class ProcAmp(IntEnum):
    """IAMVideoProcAmp property ids (VideoProcAmpProperty in strmif.h).

    The *proc amp* ("processing amplifier") is the card's own picture
    correction, applied after decoding.  During calibration it must sit at the
    driver's neutral defaults, otherwise you'd be measuring the card's
    correction instead of the camera.
    """

    BRIGHTNESS = 0
    CONTRAST = 1
    HUE = 2
    SATURATION = 3

    @property
    def label(self) -> str:
        return self.name.capitalize()


@dataclass(frozen=True)
class ProcAmpRange:
    minimum: int
    maximum: int
    step: int
    default: int
    """The driver's neutral value — what "Reset to neutral" restores.  On the
    Elgato every control runs 0–10000 with 5000 as neutral.  (The 128/64/64/0
    figures quoted for this chip are its internal register units; DirectShow
    rescales them.)"""
    auto_capable: bool


class VideoDeviceControls:
    """Proc amp and decoder access for one video capture device.

    Use from a single thread.  Every method raises ``DShowError`` on failure —
    for example once the device has been unplugged.
    """

    def __init__(self, device_name: str, device_index: int = 0) -> None:
        self.device_name = device_name
        self.device_index = device_index
        self.device_path: str | None = None
        self._thread: int | None = None
        self._com_owed = False
        self._filter: _ComPtr | None = None
        self._procamp: _ComPtr | None = None
        self._decoder: _ComPtr | None = None

    # -- lifetime -------------------------------------------------------------

    def open(self) -> None:
        self.close()
        self._thread = threading.get_ident()
        self._com_owed = _co_initialize()
        try:
            pairs = _enumerate(CLSID_VideoInputDeviceCategory)
            try:
                for info, moniker in pairs:
                    if info.name == self.device_name and info.index == self.device_index:
                        raw = c_void_p()
                        # IMoniker::BindToObject (slot 8) creates a filter instance for the device.
                        moniker.call(
                            8, (c_void_p, c_void_p, POINTER(GUID), POINTER(c_void_p)),
                            None, None, byref(IID_IBaseFilter), byref(raw),
                            what=f"Could not open “{self.device_name}” for property access",
                        )
                        self._filter = _ComPtr(raw, "IBaseFilter")
                        self.device_path = info.path
                        break
                else:
                    raise DShowError(f"Video device “{self.device_name}” not found")
            finally:
                for _, moniker in pairs:
                    moniker.release()
            self._procamp = self._optional(IID_IAMVideoProcAmp, "IAMVideoProcAmp")
            self._decoder = self._optional(IID_IAMAnalogVideoDecoder, "IAMAnalogVideoDecoder")
        except BaseException:
            self.close()
            raise

    def _optional(self, iid: GUID, name: str) -> _ComPtr | None:
        assert self._filter is not None
        try:
            return self._filter.query(iid, name)
        except DShowError as exc:
            log.info("%s: %s", self.device_name, exc)
            return None

    def close(self) -> None:
        for ptr in (self._procamp, self._decoder, self._filter):
            if ptr is not None:
                try:
                    ptr.release()
                except OSError:  # an unplugged device can fault on release; nothing to do
                    pass
        self._procamp = self._decoder = self._filter = None
        if self._com_owed:
            _ole32.CoUninitialize()
            self._com_owed = False

    def __enter__(self) -> VideoDeviceControls:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._filter is not None

    @property
    def has_proc_amp(self) -> bool:
        return self._procamp is not None

    @property
    def has_decoder(self) -> bool:
        return self._decoder is not None

    def _need(self, ptr: _ComPtr | None, what: str) -> _ComPtr:
        if self._thread is not None and threading.get_ident() != self._thread:
            raise DShowError("VideoDeviceControls used from a different thread than it was opened on")
        if ptr is None:
            raise DShowError(f"“{self.device_name}” has no {what}")
        return ptr

    # -- proc amp (IAMVideoProcAmp: GetRange=3, Set=4, Get=5) -----------------

    def proc_amp_range(self, prop: ProcAmp) -> ProcAmpRange | None:
        """Range and neutral default of one control, or None if the driver lacks it."""
        amp = self._need(self._procamp, "proc amp")
        lo, hi, step, default, caps = (c_long() for _ in range(5))
        try:
            amp.call(
                3, (c_long,) + (POINTER(c_long),) * 5,
                int(prop), byref(lo), byref(hi), byref(step), byref(default), byref(caps),
                what=f"{prop.label}: GetRange failed",
            )
        except DShowError as exc:
            if exc.unsupported:
                return None
            raise
        return ProcAmpRange(lo.value, hi.value, step.value, default.value, bool(caps.value & _VIDEOPROCAMP_FLAGS_AUTO))

    def get_proc_amp(self, prop: ProcAmp) -> int:
        amp = self._need(self._procamp, "proc amp")
        value, flags = c_long(), c_long()
        amp.call(5, (c_long, POINTER(c_long), POINTER(c_long)), int(prop), byref(value), byref(flags),
                 what=f"{prop.label}: read failed")
        return value.value

    def set_proc_amp(self, prop: ProcAmp, value: int) -> int:
        """Set a control (manual mode) and return the value the driver actually kept."""
        amp = self._need(self._procamp, "proc amp")
        amp.call(4, (c_long, c_long, c_long), int(prop), int(value), _VIDEOPROCAMP_FLAGS_MANUAL,
                 what=f"{prop.label}: could not set {value}")
        return self.get_proc_amp(prop)

    # -- decoder (IAMAnalogVideoDecoder) ----------------------------------------
    # slots: get_AvailableTVFormats=3, put_TVFormat=4, get_TVFormat=5,
    #        get_HorizontalLocked=6, get_NumberOfLines=9

    def _get_long(self, slot: int, what: str) -> int:
        dec = self._need(self._decoder, "analog video decoder")
        out = c_long()
        dec.call(slot, (POINTER(c_long),), byref(out), what=what)
        return out.value

    def available_tv_formats(self) -> int:
        return self._get_long(3, "Could not read the supported TV standards")

    def tv_format(self) -> int:
        return self._get_long(5, "Could not read the decoder's TV standard")

    def set_tv_format(self, flag: int) -> None:
        dec = self._need(self._decoder, "analog video decoder")
        dec.call(4, (c_long,), int(flag), what=f"Could not switch the decoder to TV standard 0x{flag:X}")

    def horizontal_locked(self) -> bool:
        """True when the decoder has locked onto horizontal sync — i.e. a picture
        signal is arriving.  False means no signal (camera off, cable loose)."""
        return bool(self._get_long(6, "Could not read the signal-lock status"))

    def number_of_lines(self) -> int:
        return self._get_long(9, "Could not read the number of lines")
