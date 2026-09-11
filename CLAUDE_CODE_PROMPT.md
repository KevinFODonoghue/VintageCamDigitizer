# Project Brief: Analog Video Capture & Calibration Tool

Build a Windows desktop application for capturing analog composite video from a
vintage camera and providing the live measurement instruments needed to calibrate
that camera's color and luma.

This is a real, working setup — not hypothetical. Every hardware fact below has
been verified on the target machine.

---

## Background

The user is restoring a 1984 RCA CKC021 tube-based color video camera. Its
composite output goes into an Elgato Video Capture dongle (Conexant CX231xx
chipset) on a Windows 11 laptop.

Existing consumer software is inadequate:

- Elgato's own app records 640x480 lossy MPEG-4 and offers no measurement tools.
- OBS is heavyweight, has no vectorscope, and its preview subsamples chroma.
- A previous ffmpeg-command-line workflow worked but was unusable for calibration —
  you cannot turn a trimmer pot while reading a terminal.

**The core need:** while physically adjusting trimmer pots inside the camera, the
user must see (a) a live low-latency picture and (b) live numeric measurement of
how far the signal is from correct. No existing tool does both.

---

## Verified Hardware Facts

Do not re-derive these. They were established by direct measurement.

**DirectShow device names (exact strings):**

```
"Elgato Video Capture"                    (audio, video)
"Analog Audio In (Elgato Video Capture)"  (audio)
```

**Video pin capabilities:**

```
Pin "Capture" (alt name "2")
  pixel_format=uyvy422  min 80x60   fps=29.97  max 720x480 fps=29.97   <- NTSC
  pixel_format=uyvy422  min 88x72   fps=25     max 720x576 fps=25      <- PAL
  pixel_format=gray     min 88x72   fps=25     max 720x576 fps=25
Pin "Audio Out" (alt name "3")
```

**Crossbar routing (already correct, but expose it in the UI):**

```
Output pin 0 "Video Decoder" -> current input pin 0, compatible: 0, 1
Output pin 1 "Audio Decoder" -> current input pin 2, compatible: 2
Input pin 0 - "Video Composite"   <- the one in use
Input pin 1 - "S-Video"
Input pin 2 - "Audio Line"
```

**Working capture format:** 720x480, uyvy422, 29.97 fps, interlaced (480i, NTSC-M).

**Known gotchas, learned the hard way:**

- The driver **rejects** `-framerate 30000/1001`. Use `29.97` or omit it entirely.
  Passing the rational form produces `Could not set video options` and an I/O error.
- The device **cannot be opened by two processes at once**. Enforce this in the app —
  one capture handle, fan out internally.
- Pairing `video=` and `audio=` in one DirectShow input string **fails** with
  `Could not find output pin from audio only capture device`. Audio must be opened
  as a separate input.
- The card's video **proc amp** (brightness/contrast/hue/saturation) must stay at
  neutral defaults (128/64/64/0) during calibration, or you measure the card's
  correction instead of the camera. Surface these in the UI with a prominent
  "reset to neutral" control and a warning when they are off-default.

---

## Required Stack

Use exactly this. Do not substitute.

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Fast iteration; user is a beginner programmer |
| Capture/decode | **PyAV** (`av`) | libav bindings; one device handle, frames as numpy |
| GUI | **PySide6** (Qt6) | Official Qt binding, LGPL, solid video widgets |
| Arrays/math | **numpy** | Chroma math on every frame |
| Plot rendering | **pyqtgraph** | Fast enough for 30fps scope redraws; matplotlib is not |
| Packaging | **PyInstaller** | Single .exe, later phase |

**Do not use:** OpenCV for capture (bad DirectShow support), matplotlib for live
scopes (too slow), tkinter, web frameworks, or Electron.

Target: Windows 11 x64. Keep the code portable enough that the capture backend
could later accept V4L2 on Linux, but do not build that now.

---

## Architecture

Three threads. Do not put decode or analysis on the GUI thread.

```
CaptureThread  -- owns the single PyAV device handle
                  emits raw uyvy422 frames
       |
       +--> RecordThread   -- FFV1 lossless muxing to .mkv, never blocks capture
       |
       +--> AnalysisThread -- downsampled chroma/luma stats, scope point clouds
       |
       +--> GUI (main)     -- direct blit of the newest frame; drops frames freely
```

Frame delivery must be **drop-on-late**, never queue-and-lag. A stale preview is
useless when the user's hand is on a trimmer. Prefer a single-slot buffer holding
the newest frame over any FIFO.

Recording must be independent: if the preview or scopes stall, the file must keep
receiving every frame.

---

## Features — Phase 1 (build this first)

**Live preview**
- 720x480, minimal latency, drop-on-late
- Fit-to-window and 1:1 pixel modes
- Deinterlace toggle for viewing only (bob or blend) — the recorded file is
  untouched by this

**Overlays**, independently toggleable:
- Centering grid: 10x10 cells, faint white
- Center crosshair: full-width/height lines plus a bold short cross at the exact
  center pixel (360, 240)
- Safe-area boxes: 90% action safe, 80% title safe
- 16-step luma staircase reference strip along one edge

**Recording**
- FFV1 lossless in Matroska, matching the proven ffmpeg settings:
  `-c:v ffv1 -level 3 -g 1 -slices 16 -slicecrc 1`
- Optional PCM audio from the separate DirectShow audio input
- Timestamped filenames, configurable output directory
- Live elapsed time, file size, and free-disk-space readout
- Warn at low disk space; FFV1 at this resolution runs 30–45 GB/hour

**Device panel**
- Enumerate DirectShow devices, let the user pick
- Crossbar input selector (Composite / S-Video)
- Video standard selector (NTSC / PAL)
- Proc amp sliders with neutral defaults marked and a reset button
- Connection status; graceful handling of device disappearing mid-session

---

## Features — Phase 2 (the actual reason this exists)

These are the instruments. Get them right.

**Vectorscope**
- Plot Cb/Cr as a 2D point cloud, standard orientation
- Graticule with NTSC 75% colorbar target boxes (R, G, B, Y, Cy, Mg) at correct
  angles and amplitudes
- Center reticle marking true neutral
- Adjustable gain (1x, 2x, 5x) — near-neutral errors are small and need magnification
- Persistence/decay so the trace is readable rather than flickering
- Sample a decimated grid of pixels, not all 345,600, to hold 30fps

**Waveform monitor**
- Luma level per horizontal position, IRE scale
- Reference lines at 0 IRE (blank), 7.5 IRE (NTSC setup/pedestal), 100 IRE (peak white)
- Overlay/parade mode toggle
- Clipping indicator when values hit the 16 or 235 rails

**Live numeric readout — highest value feature**

A large, always-visible panel showing:

```
Cb error   -1.4      (target 0)
Cr error   +0.7      (target 0)
Y peak      205      (target ~235)
Y mean      180
Clipped   0.0%
```

Computed over a user-defined region of interest, drawn by dragging on the preview.
This turns calibration into watching a number approach zero, which is the entire
point of the application.

Include a "hold/compare" function: freeze the current readings alongside live ones
so the user can see whether an adjustment helped.

**Histogram**
- Y, Cb, Cr distributions
- Log scale toggle

---

## Features — Phase 3 (later)

- Session log: timestamped record of measurements, so a calibration session
  produces a written trail
- Snapshot to PNG with measurements burned into a sidecar text file
- Frame-average mode: average N frames to cut analog noise for a more stable reading
  (very useful — single-frame chroma readings are noisy)
- A/B comparison against a previously saved reference capture
- Preset save/load for device configuration
- PyInstaller packaging to a single .exe

---

## UI Guidance

Dark theme — this is a video monitoring tool and a bright UI wrecks visual judgment
of the picture.

Layout: preview occupies the majority of the window. Instruments dock to the right
and/or bottom, each independently show/hide. The numeric readout must remain visible
regardless of which scopes are open.

Everything frequently used gets a keyboard shortcut. The user will have one hand
inside a camera. Suggested: `R` record toggle, `Space` freeze, `G` grid, `C`
crosshair, `V` vectorscope, `W` waveform, `H` hold reading.

Show measurement units and targets inline. Do not make the user remember that 7.5
IRE is the NTSC pedestal — label it.

---

## Code Quality

- Type hints throughout
- Docstrings explaining *why*, especially for the video-format and DirectShow
  workarounds listed above — those look arbitrary and will otherwise get
  "cleaned up" and break
- A `config.py` or JSON settings file for device names, paths, defaults
- Real error handling for: device in use, device disconnected mid-capture,
  disk full, unsupported format requested
- No silent failures — surface errors in the UI, not just the console
- README with setup instructions and a short explanation of what each instrument
  measures and how to read it

The user is a first-year mechanical engineering student with limited programming
experience. Comment generously. Explain the video-domain concepts (chroma
subsampling, IRE, interlacing, cross-color) in comments where they appear, because
learning the domain is part of the point.

---

## Definition of Done — Phase 1

- Opens the Elgato device and shows live 720x480 video with under ~100ms latency
- Records FFV1 lossless files that play correctly in VLC
- Grid and crosshair overlays toggle cleanly
- Survives the device being unplugged and reconnected without crashing
- Runs from `python main.py` with no manual configuration

Build Phase 1 completely and verify it works before starting Phase 2.
