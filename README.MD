## Requirements

Windows
opencv-python>=4.8
mss>=9.0
numpy>=1.24
sounddevice>=0.4.6
wavio>=0.4.6
colorama>=0.4.6
dxcam>=0.0.5
Pillow>=10.0
**FFmpeg** on `PATH`, or `ffmpeg.exe` next to the script

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
