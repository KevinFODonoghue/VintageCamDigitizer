# Setup — Windows 11

Paste each block into **PowerShell**. Blocks marked ADMIN need an elevated window
(Win+X → Terminal (Admin)).

---

## 1. Core tools

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
winget install --id Anthropic.ClaudeCode -e
```

Close and reopen PowerShell afterward — PATH only updates for new sessions.

Verify:

```powershell
git --version; python --version; ffmpeg -version | Select-Object -First 1; claude --version
```

If `winget` is blocked by policy, download manually:

- Git — https://git-scm.com/download/win
- Python 3.12 — https://www.python.org/downloads/windows/ (tick **Add python.exe to PATH**)
- FFmpeg — https://www.gyan.dev/ffmpeg/builds/ (grab `ffmpeg-release-essentials.zip`, extract to `C:\ffmpeg`, add `C:\ffmpeg\bin` to PATH)
- Claude Code — https://claude.com/product/claude-code

---

## 2. Editor (pick one)

```powershell
winget install --id Microsoft.VisualStudioCode -e
```

VS Code — https://code.visualstudio.com/ — has a Claude Code extension.

---

## 3. Playback for lossless files

Windows Media Player cannot open FFV1. VLC can.

```powershell
winget install --id VideoLAN.VLC -e
```

---

## 4. Elgato driver — ADMIN

Only the **Driver**, not the application.

https://www.elgato.com/us/en/s/downloads → Capture → Video Capture → **Driver**

Unplug the dongle before installing, plug in when prompted. Decline "Always trust
software from Elgato Systems" — you only need this one driver.

**The driver will not load unless Memory Integrity is off.** It is a 2014 driver
and is not HVCI-compatible; Windows rejects it with Code 39 / `0xC0000220`.

Windows Security → Device security → Core isolation details → **Memory integrity: Off** → reboot.

This lowers a real kernel-level protection. Turn it back on between capture
sessions. If the toggle is greyed out and says your organization manages it, that
is a policy block and the driver cannot load on that machine.

Verify all three entries read `Status: OK`:

```powershell
Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -like "*Elgato*" } | Format-List FriendlyName, Status, Problem
```

Verify DirectShow sees it:

```powershell
ffmpeg -list_devices true -f dshow -i dummy
```

You want `"Elgato Video Capture" (audio, video)` in that list.

---

## 5. Repo and Python environment

```powershell
cd $HOME\Documents
git clone https://github.com/YOURNAME/YOURREPO.git
cd YOURREPO
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install av PySide6 numpy pyqtgraph
pip freeze > requirements.txt
```

If `Activate.ps1` is blocked by execution policy:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Later, for packaging:

```powershell
pip install pyinstaller
```

---

## 6. .gitignore

```powershell
@"
.venv/
__pycache__/
*.pyc
captures/
*.mkv
*.mp4
*.png
settings.json
build/
dist/
*.spec
"@ | Out-File -Encoding utf8 .gitignore
```

Capture files are tens of GB per hour. Keep them out of git.

---

## 7. Start Claude Code

Put `CLAUDE_CODE_PROMPT.md` in the repo root, then:

```powershell
claude
```

Open with:

```
Read CLAUDE_CODE_PROMPT.md and build Phase 1. Ask me before making architecture
decisions that differ from the brief.
```

---

## Known-good reference commands

If the app misbehaves, these are verified working. Use them to tell app bugs from
hardware problems.

Preview:

```powershell
ffplay -f dshow -i video="Elgato Video Capture"
```

Record 10 seconds lossless:

```powershell
ffmpeg -f dshow -rtbufsize 512M -video_size 720x480 -pixel_format uyvy422 -i video="Elgato Video Capture" -c:v ffv1 -level 3 -g 1 -slices 16 -slicecrc 1 -t 10 "$HOME\Videos\test.mkv"
```

List device capabilities:

```powershell
ffmpeg -f dshow -list_options true -i video="Elgato Video Capture"
```

**Do not add `-framerate 30000/1001`.** The driver rejects it. Use `29.97` or omit it.

---

## Reference links

- PyAV docs — https://pyav.org/docs/stable/
- PySide6 docs — https://doc.qt.io/qtforpython-6/
- pyqtgraph docs — https://pyqtgraph.readthedocs.io/
- numpy docs — https://numpy.org/doc/stable/
- FFmpeg dshow input — https://ffmpeg.org/ffmpeg-devices.html#dshow
- FFV1 spec — https://datatracker.ietf.org/doc/rfc9043/
- Claude Code docs — https://docs.claude.com/en/docs/claude-code/overview
