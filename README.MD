## Features

- **Full-screen capture** using `mss` (and a low-latency `dxcam` preview).
- **Flexible audio recording**: Capture Microphone, System Audio via WASAPI (no Stereo Mix required), or mix both together seamlessly.
- **Multi-monitor support**: Choose which display to record and preview from a dropdown menu.
- **Pause / Resume** without breaking the output file or audio sync.
- **Real-time preview** toggle (hidden automatically during recording).
- **Quality presets**: 1080p, 720p, 480p.
- **Eight output formats**: MP4 (H.264), MKV, MOV, AVI (MPEG-4), TS, FLV, WEBM (VP9), and WMV.
- **Optional extra compression**: A one-click checkbox to re-encode the final file with a slower preset for maximum file size reduction.
- **Crash-safe**: temporary files are cleaned up automatically; broken pipes and background thread errors are caught and shown via UI dialogs.

## Requirements

- **Windows 10/11**
- **FFmpeg** on `PATH`, or `ffmpeg.exe` next to the script.

Install the Python dependencies via pip:
```bash
pip install -r requirements.txt

## Installation & Usage

```bash
pip install -r requirements.txt
python screen_recorder.py
```

Enter a filename, pick **format** and **quality**, press **●** to record, **■** to stop and save.

| Button | Action |
|--------|--------|
| ● | Start recording |
| ⏸ / ▶ | Pause / Resume |
| ■ | Stop and save |
| 👁 | Toggle preview |
| x | Close app |

## Building a Standalone Executable

```bash
pip install nuitka
python -m nuitka --standalone --enable-plugin=tk-inter ^
  --windows-console-mode=disable ^
  --include-data-files="C:\path\to\ffmpeg.exe=ffmpeg.exe" ^
  screen_recorder.py
```

## AI Assistance

This project is primarily human-written. AI coding assistants (Claude) were used for specific portions — including [refactoring / documentation / debugging / boilerplate], and every AI-assisted change was reviewed, tested, and integrated by the author.
