# VintageCam Digitizer

A Windows desktop app for capturing analog composite video and calibrating the
camera that produces it. It was built to restore a 1984 **RCA CKC021** tube colour
camera feeding an **Elgato Video Capture** USB dongle.

While you turn a trimmer pot inside the camera, you need a live, low-latency
picture and something that tells you how far off the signal is. This app shows
the picture with calibration overlays, records lossless FFV1 files, and shows
and resets the capture card's own picture controls so they can't quietly
"correct" the camera behind your back.

> **Status: Phase 1 is complete.** That covers the live preview, overlays,
> lossless recording and the device panel.
> **Phase 2 is next:** the measuring instruments (vectorscope, waveform
> monitor, the live numeric readout, and a histogram). See [Roadmap](#roadmap).

---

## Quick start

```powershell
cd C:\dev\VintageCamDigitizer
python main.py
```

`main.py` finds the project's virtual environment (`.venv`) by itself, so you
don't need to activate it first. **Close anything else using the Elgato first**
(an ffplay window, OBS, Elgato Game Capture, the Windows Camera app). A capture
device can only stream to one program at a time. If you forget, the app says so
and connects as soon as the other program lets go.

## Setup from scratch (Windows 11)

The full notes are in [docs/SETUP.md](docs/SETUP.md). In short:

1. **Tools:** Git, Python 3.12, FFmpeg and VLC. For example:
   `winget install --id Python.Python.3.12 -e`. FFmpeg isn't needed by the app
   itself (PyAV bundles its own copy); it's for the reference commands in SETUP.md.
2. **Elgato driver:** install the *driver only*.
   - It's a 2014 driver, and Windows won't load it while **Memory Integrity** is on
     (Device Manager shows Code 39).
   - Turn Memory Integrity off to capture: Windows Security → Device security →
     Core isolation.
   - Turn it back on between sessions; it's a real security feature.
3. **Python environment:**

   ```powershell
   git clone https://github.com/KevinFODonoghue/VintageCamDigitizer.git
   cd VintageCamDigitizer
   python -m venv .venv
   .\.venv\Scripts\python -m pip install -r requirements.txt
   ```

4. **Check the hardware.** It should end with `ALL CHECKS PASSED`:

   ```powershell
   .\.venv\Scripts\python tools\hardware_check.py
   ```

5. **Run:** `python main.py`

---

## The window

| Area | What it's for |
|---|---|
| **Picture** (centre) | The live feed with overlays. Top left: status (live, frames per second, standard, input). Top right: `● REC` while recording. Warnings appear top centre. |
| **Device** (right) | The capture device, input (Composite / S-Video), TV standard, signal lock, and the **proc amp** (the card's own brightness, contrast, saturation and hue). |
| **Recording** (right) | The big record button, elapsed time, file size, data rate, free disk space and time left, dropped frames, and the audio level. |
| **Log** (bottom) | Everything that happens, with a timestamp. Warnings are amber and errors red. Nothing fails silently. A full debug log goes to `logs/vintagecam.log`. |
| **Status bar** | Capture state, measured fps, the app's display lag, and any frames dropped by the device. |

Panels can be hidden (Ctrl+1/2/3), dragged or floated. The layout is remembered.

## Keyboard shortcuts

These work anywhere in the window, including when a floating panel has focus,
because you'll have one hand inside the camera.

| Key | Action |
|---|---|
| **R** | Start / stop recording |
| **Space** | Freeze / unfreeze the picture (recording carries on) |
| **G** | Centering grid |
| **C** | Center crosshair |
| **S** | Safe areas |
| **L** | Luma staircase reference |
| **D** | Deinterlace the preview: off → bob → blend |
| **F** | Fit to window ↔ 1:1 pixels (or double-click the picture) |
| **A** | Correct pixel aspect (true 4:3) |
| **I** | On-screen info |
| **F11** / **Esc** | Full screen / leave full screen |
| **Ctrl+R** | Reconnect to the device now |
| **Ctrl+O** | Open the recordings folder |
| **F1** | Show this list |

Phase 2 will add **V** (vectorscope), **W** (waveform) and **H** (hold reading).

---

## The instruments, and how to read them

### The live picture

- **Latency.** Each frame goes straight from the card to the screen, and a
  frame the screen had no time for is skipped, never queued. So the picture is
  never "behind". The status bar's *display lag* is this app's share, typically
  a few milliseconds. The card itself adds more; measured on this machine, a
  change at the card reaches the software in about 60 ms.
- **Fit vs 1:1.**
  - *Fit* scales the picture to the window.
  - *1:1* maps each video pixel to one physical screen pixel, whatever your
    Windows display scaling is. Use 1:1 when judging fine detail and focus.
- **Pixel aspect.** An NTSC pixel is 10/11 as wide as it is tall. With **A** on
  (the default), circles on your chart look round, as they would on a TV. With
  it off, the frame is shown with square pixels, about 10% too wide.
- **Deinterlace (preview only).** The camera scans the picture as two
  *fields*: odd lines, then even lines, 1/60 s apart. Motion therefore shows
  "combing".
  - *Blend* averages the two fields: a stable picture with a little softness.
  - *Bob* shows each field on its own at 59.94 per second: smooth motion, half
    the vertical detail. If motion judders back and forth in bob, flip
    View → Bob field order. (The field order couldn't be verified on a static
    chart.)
  - Recordings are never deinterlaced.

### Centering grid (G)

- 10 × 10 cells, which is 72 × 48 pixels on a 720 × 480 frame, drawn as faint
  white lines.
- Use it to check **linearity**: equal squares on your chart should fill equal
  grid cells from the middle to the edges. Tube cameras stretch or squash near
  the edges when the scan linearity controls are off.

### Center crosshair (C)

- A thin line runs full width and full height, with a bold red cross at the
  exact centre of the frame.
- With an even number of pixels there's no single middle pixel. The centre is
  the corner shared by pixels 359/360 and rows 239/240, the same place the old
  ffplay overlay marked.
- Align your chart's centre mark with it to set **horizontal and vertical
  centering** (the camera's H/V centering or shift adjustments).

### Safe areas (S)

- **Action safe (green, 90%)** and **title safe (orange, 80%)**.
- CRT TVs hid the edges of the picture under the bezel, by varying amounts.
  Anything important had to sit inside action safe, and text inside title safe.
- A chart framed for full scan should have its border just outside action safe,
  evenly on all four sides. Uneven margins mean a centering or size error.

### Luma staircase (L)

- A strip of **16 equal grey steps** from black (Y′ = 16) to white (Y′ = 235),
  drawn in exactly the grey this preview uses for those luma values.
- Point the camera at a grey-scale chart and compare step by step:
  - if the camera's darkest bars are all as black as the first step, it's
    **crushing the blacks** (setup or pedestal too low);
  - if its brightest bars merge into the last step, it's **clipping the
    whites** (gain or white clip too low).
- *Phase 2's waveform turns this visual check into numbers.*

### Signal indicator (Device panel)

- **Locked** means the card's decoder has locked onto the camera's sync pulses.
- **NO SIGNAL** means it hasn't. Check the camera is on and connected to the
  selected input. The picture area also says so in large type.

### Proc amp (Device panel)

- The card's own brightness, contrast, saturation and hue controls.
- **During calibration they must stay at neutral**, or you'd be adjusting the
  camera to cancel out the card. Neutral is marked by an amber notch on each
  slider.
- If anything is off neutral:
  - an amber warning appears in the panel **and** over the picture;
  - **Reset all to neutral** turns amber.
- Recordings note in their metadata whether the proc amp was neutral.
- Neutral is whatever the driver reports as default. On the Elgato every
  control runs **0–10000 with 5000 as neutral**. (The "128/64/64/0" figures
  quoted for this chip are its internal register units; DirectShow rescales
  them.)

### Recording readouts

| Readout | Meaning |
|---|---|
| Elapsed | Length of video in the file |
| Size / Data rate | FFV1 is lossless, so size depends on the picture. About 20 GB/h on a mostly-white test chart, typically 30–45 GB/h on real footage. Noise costs bits. |
| Dropped | "lost" = frames the disk couldn't keep up with (should always be 0). "skipped by device" = frames the card never delivered (kept as timing gaps, so the rest stays in sync). |
| Audio | Peak level of the sound over the last half second. Keep loud passages below about −6 dB; 0 dB means clipping. "silence" means nothing is coming in. |
| Free space / Time left | At the current data rate. An amber warning appears below 20 GB free. Below 2 GB the recording stops cleanly, so the file isn't damaged by a full disk. |

---

## A calibration session, step by step

1. Turn **Memory Integrity** off (and back on afterwards), then plug in the
   Elgato and start the app.
2. Check the Device panel: **Signal: locked**, and the proc amp shows no amber
   warning. If it does, click **Reset all to neutral**.
3. Frame the chart. Use **C** (crosshair) and **S** (safe areas) for centering
   and size, **G** (grid) for linearity, and **L** (staircase) for black and
   white levels.
4. Press **Space** to freeze a "before" picture when you want to compare; press
   it again for live.
5. Press **R** to record a before/after clip for your records. Files are named
   `capture_YYYYMMDD_HHMMSS.mkv` in `Videos\VintageCam` (change it in the
   Recording panel).

---

## Video concepts used in this app

- **Interlacing and fields.** NTSC draws 525 lines per frame as two fields of
  262½ lines each, 59.94 fields per second. The card weaves each pair of fields
  into one 720 × 480 frame. More in `vintagecam/video_format.py`.
- **29.97 fps.** When colour was added in 1953, NTSC's frame rate dropped 0.1%
  to 30000/1001, so the colour subcarrier wouldn't beat against the sound
  carrier. Your camera measured 29.968 fps over a minute.
- **Y′CbCr.** Video stores brightness (luma, Y′) separately from two
  colour-difference signals (Cb ≈ blue − luma, Cr ≈ red − luma). Grey means
  Cb = Cr = 128. The Phase 2 vectorscope plots Cb against Cr, so a colour cast
  shows up as the trace moving off centre.
- **Limited range.** 8-bit video puts black at 16 and white at 235 (not 0 and
  255), leaving headroom for analog overshoot. Values stuck at 16 or 235 mean
  something clipped.
- **IRE.** The analog unit of video level: 0 IRE is blanking, 100 IRE is peak
  white, and sync pulses sit at −40. US NTSC (NTSC-M) puts black slightly above
  blanking, at **7.5 IRE**; that offset is the *setup* or *pedestal*. The Phase
  2 waveform will label 0, 7.5 and 100 IRE.
- **4:2:2 chroma.** There's one colour sample for every two luma samples along
  a line, because the eye sees colour detail less sharply. Composite video's
  colour is even coarser, so nothing the camera sent is lost.
- **Cross-colour.** Fine stripes near 3.58 MHz (the wedges and star on your
  chart) fool the decoder into seeing false colour: the rainbow shimmer. Always
  measure colour on flat grey patches.

`vintagecam/color.py` and `vintagecam/video_format.py` explain each of these in
more depth, right next to the code that uses them.

---

## Recordings

- **Format:** FFV1 version 3, lossless, in Matroska (`.mkv`), with 4:2:2 planar
  (yuv422p) video at 720 × 480 and 30000/1001 fps. Tagged BT.601 / limited range.
- **Settings:** identical to the proven command,
  `ffmpeg … -c:v ffv1 -level 3 -g 1 -slices 16 -slicecrc 1`. Our file's FFV1
  configuration record is byte-identical to that command's. Every frame is a
  keyframe, and every slice carries a CRC.
- **Lossless means bit-exact:** decoding the file returns exactly the bytes the
  card delivered. `tools/hardware_check.py` verifies this on live frames.
- **Play:** in VLC. Windows Media Player can't play FFV1. Turn on VLC's
  Video → Deinterlace for smooth motion.
- **Verify integrity at any time:** `ffmpeg -v error -i file.mkv -f null -`
  prints nothing for an intact file.
- **Audio (optional):** uncompressed 16-bit PCM, 48 kHz stereo, next to the
  video. See [Audio](#audio).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "…is in use by another program" | Close ffplay, OBS, Elgato Game Capture or the Camera app. The app retries every few seconds, or press Ctrl+R. |
| "…is not connected" | Plug the Elgato in; capture starts by itself. If it never appears, check Device Manager. **Code 39** means Memory Integrity is on. |
| NO SIGNAL | The camera is off, the cable is loose, or the wrong input (Composite / S-Video) is selected. |
| "refused the requested video format" | The wrong standard is selected (NTSC for this camera), or a different device is selected. |
| Picture freezes and the app says "stopped responding… reconnecting" | The device dropped off USB. It reconnects automatically. If you were recording, the file up to that point is saved and closed properly. |
| Amber "PROC AMP NOT NEUTRAL" | Click **Reset all to neutral** in the Device panel. |
| "The Elgato's driver stopped responding…" | The driver can hang when a stream in the wrong TV standard is closed (seen: switching to PAL with this NTSC camera, then back). Unplug the Elgato for 5 s and plug it back in; capture restarts by itself. The app asks before you switch away from a locked picture. If the app was closed while the driver was stuck, Windows can't end it until you unplug the card. |

**Don't "fix" the frame rate to 30000/1001.** The app asks for `29.97`
deliberately. FFmpeg converts the rate into 100-ns ticks with integer division:
30000/1001 becomes 333 666 ticks, one short of the 333 667 the Elgato
advertises, so it fails with "Could not set video options". The details are in
`video_format.py`, and a unit test guards it.

Known-good reference commands (preview, 10-second recording, device listing)
are in [docs/SETUP.md](docs/SETUP.md). They tell a hardware problem from an app
bug.

## Audio

Audio recording works, through a different Windows route from the one the
brief planned. What's been verified, and how, is at the end of this section.

- The camera's sound goes into the Elgato's **RCA audio jacks** (red = right,
  white = left), next to the yellow video plug. With the camera's lead in the
  red jack, the Elgato delivers its sound on both channels.
- **Record audio with the video** is on by default (Device panel). Recordings then
  carry uncompressed 16-bit, 48 kHz PCM next to the FFV1 video, and the
  Recording panel shows a level meter. Keep loud passages below about −6 dB.
- **Plug** (Device panel → Audio) picks what's recorded: **red + white as
  stereo** (the default), or one plug's channel on its own as a mono track.
  With this camera both channels carry the same sound, so mono only saves
  space (about 0.3 GB an hour, under 1% of the video).
- There's only sound **while video is live**: the card switches its audio path
  on together with the video decoder. A recording always runs with video, so in
  practice this doesn't matter.

**Why a different route.** The brief planned to use the DirectShow audio device
"Analog Audio In (Elgato Video Capture)". On this PC, every route through
Windows' audio engine refuses it:

- FFmpeg/DirectShow: "Could not find output pin from audio only capture device";
- waveIn: `WAVERR_BADFORMAT`;
- DirectSound: fails to open;
- WASAPI, shared and exclusive: `AUDCLNT_E_UNSUPPORTED_FORMAT`.

The engine has no format stored for this endpoint, and the driver rejects every
stream it's asked for. **Kernel streaming (WDM-KS)** reaches the driver's audio
filter directly, underneath the engine, and works. The app uses it through the
`sounddevice` package (PortAudio). `vintagecam/audio.py` explains the details.

**Two measured quirks:**

- The chip always delivers **48 000 samples per second**, whatever rate is asked
  for, so the app always asks for 48 kHz. Asking for 44.1 kHz would label
  48 kHz sound as 44.1 kHz, and it would play 9% slow.
- With video closed, the input still runs, but every sample is zero.

**Staying in sync.** Each block of sound is placed by when it was captured,
relative to the latest video frame. The camera's frame rate and the card's
audio clock both differ from nominal by tens of parts per million, so the
recorder now and then drops or repeats a single sample (inaudible) to keep the
sound within 10 ms of the picture. Sound reaches the app up to ~0.4 s after it
was captured, so when you press Stop the recording keeps taking the sound still
in the buffers, then cuts it to end exactly with the last frame. If the sound
seems early or late overall, set `av_sync_offset_ms` in settings.json
(positive = sound later).

**What's verified.**

- **Sound reaching the camera is recorded correctly** (2026-09-11, with
  `tools/camera_sound_check.py`). The tool plays a 1 kHz and a 440 Hz tone
  through the laptop's speakers; the camera's microphone picks them up, and the
  app's own capture and recording code records them. In the file they measured
  999.96 Hz and 440.01 Hz (so the 48 kHz label is right), 57 and 41 dB above
  the surrounding sound, with peaks at −14 dBFS (no clipping).
- The tones came back **at the same level on both channels**, with the camera's
  lead in the red jack. Without a test signal, the two channels were 0.998
  correlated between 150 Hz and 4 kHz. So a stereo recording of this camera
  holds the same sound twice.
- Kernel streaming delivers 48,000 samples a second. With video closed, every
  sample is exactly zero.
- With the camera on, the input also carries interference. Its strongest lines,
  the NTSC line frequency (15,734 Hz) and 60 Hz hum, measured −54 and −58 dBFS.
  With the camera off, there's a −73 dBFS noise floor with faint USB tones
  (8 and 16 kHz).
- Recordings keep sound and picture together, including through a deliberate
  freeze.
- *On Linux* (cx231xx driver; the chip identifies as a CX23102), the same input
  offered 48 kHz stereo S16. It was only shown to open there, not to contain
  sound.

## Viewing copies (MP4)

Recordings are FFV1, a lossless archival format. VLC plays them, but Windows'
Media Player, phones and browsers don't ("encoded in Unknown format"). So the
app also makes a **viewing copy**: H.264 video and AAC sound in an `.mp4`, next
to the recording and with the same name. The `.mkv` is only read, never
changed.

- **Make one after each recording** (Recording panel) is on by default. The
  copy is made in the background while you carry on.
- **Export recordings…** (or Capture → Export MP4 viewing copies, Ctrl+E) makes
  copies of the recordings you pick, one after another. **Cancel** stops and
  deletes the unfinished copy.
- From a terminal, for one file or many:
  `.\.venv\Scripts\python main.py --export capture_20260911_001409.mkv`

What the copy is:

- **Deinterlaced** to 59.94 frames a second (FFmpeg's bwdif): each field
  becomes a frame, so motion is as smooth as on a TV and there are no comb
  lines. Which field comes first is measured from motion in the recording.
  When the picture is too still to tell, the standard's usual order is used;
  then it makes no visible difference. An order chosen in View → Bob field
  order overrides the measurement.
- **Square pixels:** 654 × 480, the picture the preview shows with "Correct
  pixel aspect".
- **Quality:** H.264 High profile at CRF 18, hard to tell from the original;
  sound is AAC at 192 kb/s (stereo) or 128 kb/s (mono).
- It's made in a separate, low-priority process (`main.py --export`), so a
  capture or recording running at the same time gets the computer first.

## Verified on the target machine

Measured with `tools/hardware_check.py` and the tests, on 2026-09-10:

| Check | Result |
|---|---|
| Device opens | 0.4 s |
| Frame rate | 29.968 fps over 60 s (camera within 60 ppm of NTSC) |
| Dropped frames | 0 |
| Timestamp jitter | σ 4.7 ms (DirectShow quantises to ~10 ms; handled) |
| Card → software latency | median 41–59 ms over three runs (a brightness change timed until it appeared in frames) |
| App display lag | 2 ms from a frame arriving off the card to being drawn |
| App start → live picture | about 1 s |
| Proc amp on the live stream | works, and is restored afterwards |
| Recording | bit-exact vs. live frames; all keyframes; slice CRCs clean; plays in VLC |
| FFV1 configuration | byte-identical to the proven ffmpeg command |
| Device in use by another program | detected and explained; resumes automatically |
| Unplug / replug | unplugged twice, once mid-recording: the recording was stopped and finalised (193 frames, decodes cleanly), and live video came back by itself after each replug, including a flaky re-plug that dropped out once |
| Camera signal lost | "NO SIGNAL" shown within a second; cleared by itself when the signal returned |
| Sound (kernel streaming) | ~48,000 samples/s arriving (48 kHz). Recordings have no gaps or overflows, even through a deliberate 0.3 s freeze of the program, and the sound ends within 1 ms of the picture |
| Sound from the camera (2026-09-11, `tools/camera_sound_check.py`) | test tones picked up by the camera's microphone come back at the right pitch (999.96 and 440.01 Hz), 41–57 dB above the surrounding sound, equally on both channels, without clipping |
| MP4 viewing copy (2026-09-11) | a 6.6 s recording (33 MB) became a 2.7 MB .mp4 in 1.4 s: H.264 High, 654 × 480, 59.94 fps (all 382 fields), AAC stereo, BT.601 colour tags |
| Other Windows audio routes | DirectShow, waveIn, DirectSound and WASAPI (shared and exclusive) all refuse this card's audio |

## Tests

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -t .    # no hardware needed, ~3 s
.\.venv\Scripts\python tools\hardware_check.py                # needs the Elgato, ~50 s
.\.venv\Scripts\python tools\camera_sound_check.py            # needs the camera on; plays tones, ~15 s
```

The unit tests cover:

- BT.601 colour maths against SMPTE 75% bars;
- that the preview's fast converter matches the formula (so the preview shows
  true colours);
- that the UYVY → planar repack is lossless;
- a synthetic recording decoded back bit-for-bit, with timing gaps preserved;
- the newest-frame slot;
- settings handling;
- error classification;
- the 29.97 guard;
- audio stored bit-for-bit and starting in step with the video, and sync holding
  when the audio clock runs fast;
- choosing the audio input (with a stand-in for PortAudio);
- the real main window, run off-screen: every overlay shortcut toggles,
  deinterlace cycles, freeze works, a frame is drawn in true colour, a stuck
  driver is waited out, and switching away from a locked picture asks first.

## Project layout

```
main.py                   start here (re-launches itself inside .venv if needed)
vintagecam/
  video_format.py         NTSC/PAL facts: sizes, 29.97, pixel aspect, the framerate gotcha
  color.py                Y'CbCr maths, UYVY layout, deinterlacers, staircase (pure numpy)
  frames.py               CapturedFrame and LatestSlot (drop-on-late hand-off between threads)
  capture.py              CaptureThread: owns the device, fans frames out, reconnects
  recorder.py             RecordThread: FFV1 (+ PCM sound) MKV writer that never blocks capture
  audio.py                the Elgato's line input via Windows kernel streaming (sounddevice / PortAudio)
  render.py               preview colour conversion (BT.601, limited -> full range)
  dshow.py                DirectShow COM via ctypes: device list, proc amp, TV standard, signal lock
  errors.py               FFmpeg/DirectShow failures -> messages you can act on
  config.py               settings.json load/save, hardware constants
  ui/                     main window, preview + overlays, device/record/log panels, theme
tools/hardware_check.py   end-to-end check against the real card
tests/                    unit tests (no hardware needed)
docs/SETUP.md             machine setup and known-good ffmpeg commands
CLAUDE_CODE_PROMPT.md     the project brief
```

**Threads**, as the brief specified:

- `CaptureThread` owns the single device handle and hands every frame to
  `RecordThread` through a bounded queue.
- It hands only the **newest** frame to the screen (and, in Phase 2, to the
  analysis thread) through single-slot buffers.
- The GUI thread never decodes; it converts and draws the one frame it's given.

## Settings

`settings.json`, next to `main.py`, is created on exit. It holds:

- device names, input and standard;
- output folder and file prefix;
- overlay and view toggles;
- low-disk thresholds;
- the window layout.

Delete it to get the defaults back. A damaged file is set aside as
`settings.json.bad` and reported in the Log.

## Roadmap

**Phase 2 — the instruments (next):**
- a vectorscope with 75% colour-bar targets, gain and persistence;
- a waveform monitor with IRE scale and clip indicators;
- **the live numeric readout** (Cb/Cr error, Y peak/mean, % clipped) over a
  region you drag on the picture, with hold/compare;
- Y/Cb/Cr histograms.

**Phase 3:**
- session log;
- snapshots with measurements;
- frame averaging;
- A/B against a reference capture;
- presets;
- a single-file .exe via PyInstaller.
