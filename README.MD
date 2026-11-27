# Screen Recorder

A lightweight screen recorder for Windows, written in Python. It
provides full-screen capture, timestamp-based audio/video
synchronization, pause/resume without splitting the recording,
multi-monitor support, live preview, and FFmpeg-encoded output.

**No watermarks. No time limits.**

## Features

-   **Timestamp-based audio/video synchronization** to reduce drift
    during longer recordings.
-   **Flexible audio recording:** microphone, system audio via WASAPI
    (no Stereo Mix required), or both.
-   **Multi-monitor support:** choose which display to record and
    preview.
-   **Pause / Resume** without splitting the output file.
-   **Recording status indicator:** see when the app is Ready,
    Recording, Paused, Finalizing, Saving, Compressing, Saved, or when
    saving fails.
-   **Live preview** that can be toggled and is hidden automatically
    during recording.
-   **Quality presets:** 1080p, 720p, and 480p.
-   **Multiple output formats:** MP4 (H.264), MKV, MOV, AVI (MPEG-4),
    TS, FLV, WEBM (VP9), and WMV.
-   **Optional extra compression** to reduce the final file size.
-   **Crash-aware cleanup and error handling** for temporary files and
    background recording tasks.

## Download (Recommended)

You can use the prepared Windows standalone package without installing
Python or manually installing the project's Python dependencies.

1.  Open the [latest GitHub
    Release](https://github.com/raminkakvand748/screen-recorder/releases/latest).
2.  Download the standalone package from the release **Assets**. It is a
    folder-based distribution packaged for download, not a single-file
    executable.
3.  Extract the downloaded archive.
4.  Open the extracted application folder (the `.dist` folder/package).
5.  Run the `.exe` file inside that folder.

**Important:** Run the executable from inside the extracted folder. Keep
the other files and folders that came with the package in place; the
executable may need them to start correctly. Do not move only the `.exe`
to another location.

You do **not** need to install Python or run
`pip install -r requirements.txt` when using the standalone package.

> If you do not see a standalone package under the latest release's
> **Assets**, it has not been attached to that release yet.

## Alternative: Run from Python Source

Choose this option if you want to inspect, modify, or run the Python
source code instead of using the prepared standalone package.

### Requirements

-   Windows 10 or Windows 11
-   A compatible Python version
-   FFmpeg on `PATH`, or `ffmpeg.exe` placed next to the script
-   The Python packages listed in `requirements.txt`

### Installation

Clone or download this repository, open a terminal in the project
folder, and run:

``` bash
pip install -r requirements.txt
python screen_recorder.py
```

## Building a Standalone Executable

```bash
pip install nuitka
python -m nuitka --standalone --enable-plugin=tk-inter ^
  --windows-console-mode=disable ^
  --include-data-files="C:\path\to\ffmpeg.exe=ffmpeg.exe" ^
  screen_recorder.py
```

## Building the Standalone Package

The standalone package is built with Nuitka. This section is for
developers preparing a release; regular users should follow the
**Download** instructions above.

Install Nuitka:

``` bash
pip install nuitka
```

Build command (adjust the FFmpeg path if needed):

``` bash
python -m nuitka --standalone --enable-plugin=tk-inter --windows-console-mode=disable --include-package=comtypes --include-package=soundcard --include-package=dxcam --include-package=sounddevice --include-package=cffi --include-module=_cffi_backend --include-package-data=soundcard --include-data-files="C:\path\to\ffmpeg.exe=ffmpeg.exe" screen_recorder.py
```

Nuitka creates a `.dist` directory containing the executable and its
supporting files. For distribution, package the entire directory (for
example, as a ZIP archive) and upload that archive to the GitHub
Release. Users should extract the archive and run the executable from
inside the extracted `.dist` folder. Do not distribute only the `.exe`.

Before publishing a release, test the extracted package on a Windows
machine that does not have the project's Python dependencies installed.

## AI Assistance
This project is primarily human-written. AI coding assistants (Claude) were used for specific portions — including [refactoring / documentation / debugging / boilerplate], and every AI-assisted change was reviewed, tested, and integrated by the author.

## Third-Party Software
This application bundles FFmpeg. For licensing information, please see [LICENSES.md](LICENSES.md).

## License
See [LICENSE](LICENSE).
