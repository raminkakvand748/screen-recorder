"""
Screen Recorder
===============
A lightweight, high-performance desktop screen recorder for Windows.

Architecture overview
---------------------
The application is split across several threads that are coordinated with
`threading.Event` flags:

    main thread (Tk)  – runs the GUI, dispatches queued UI callbacks
    session thread    – owns a recording lifecycle (start → stop → save)
    capture thread    – grabs screen frames with `mss` at ~30 fps
    encode thread     – pipes raw BGR frames into a live FFmpeg process
    audio thread      – records microphone/system audio with `sounddevice`
    preview thread    – reads a DXGI frame stream via `dxcam` for the preview

Frame flow:
    capture → Frames queue → encode → FFmpeg stdin → temp file
    audio   → audio_frames list → temp WAV
    stop    → FFmpeg muxes video + audio into the final container

Thread safety:
    Tkinter is not thread-safe, so every Tk call from a worker thread is
    marshalled through `ui_post()` onto the main-thread queue.
"""

import sys, traceback

# Surface uncaught exceptions in the main thread before the process exits.
sys.excepthook = lambda t, v, tb: (traceback.print_exception(t, v, tb), input("crash — Enter to close"))

import cv2
print("-> cv2", flush=True)
import mss
print("-> mss", flush=True) 
import numpy as np
print("-> numpy", flush=True) 
import threading
print("-> threading", flush=True)
import queue
print("-> queue", flush=True)
import time
print("-> time", flush=True)
import sounddevice
print("-> sounddevice", flush=True)
import wavio
print("-> wavio", flush=True)
import subprocess
print("-> subprocess", flush=True)
import os
import re
import shutil
import tempfile
import atexit

# Opt into per-monitor DPI awareness so screen coordinates match physical pixels.
if os.name == "nt":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        pass

import colorama

# Ensure ANSI colour codes work in the Windows console.
colorama.just_fix_windows_console()
RED = "\033[31m"
RESET = "\033[0m"

# --------------------------------------------------------------------------
# Video settings
# --------------------------------------------------------------------------
capture_width, capture_height = 1920, 1080   # fallback; replaced by real monitor size in session()
output_width,  output_height  = 1920, 1080
fps = 30.5                                    # capture rate (slightly > encoder rate to avoid starvation)
fps_output = 30                               # constant output framerate
monitor = {"top": 0, "left": 0, "width": capture_width, "height": capture_height}
format = ""                                   # ".mp4" or ".avi", set by the UI dropdown

# --------------------------------------------------------------------------
# Audio settings
# --------------------------------------------------------------------------
SAMPLE_RATE = 44100
CHANNELS    = 2
DTYPE       = "int16"
audio_frames = []                             # list of NumPy chunks accumulated during a session
audio_lock   = threading.Lock()

# --------------------------------------------------------------------------
# Shared state between threads
# --------------------------------------------------------------------------
filename        = ""
Frames          = queue.Queue(maxsize=4)      # bounded to apply back-pressure on capture
stop_event      = threading.Event()           # set to terminate the current session
pause_event     = threading.Event()           # set while the user is paused
pause_accum     = 0.0                         # total paused seconds accumulated so far
pause_mark      = None                        # perf_counter() at the start of the current pause
pause_lock      = threading.Lock()
audio_started   = threading.Event()           # signalled by audio_callback on first chunk
capture_ready   = threading.Event()           # signalled once the capture loop is running
session_thread  = None
recording       = False
recording_finishing = False                   # True while the session thread drains ffmpeg
preview_was_on  = False                       # remembers preview state across a recording
save_requested  = False
dropped_frames  = 0
encoded_frames  = 0

# --------------------------------------------------------------------------
# Preview (DXGI-based, independent of the recording pipeline)
# --------------------------------------------------------------------------
import dxcam
from PIL import Image, ImageTk

preview_enabled     = threading.Event()
preview_thread      = None
preview_frame       = None                    # latest BGR NumPy frame, shared with the Tk thread
preview_frame_lock  = threading.Lock()

preview_camera      = None                    # one DXGI camera reused for the process lifetime
preview_camera_lock = threading.Lock()

PREVIEW_W, PREVIEW_H = 740, 360
WINDOW_W             = 750
WINDOW_H_BASE        = 175
WINDOW_H_PREVIEW     = WINDOW_H_BASE + PREVIEW_H + 20


def preview_loop():
    """Continuously pull the latest desktop frame and store a downscaled copy."""
    global preview_frame

    camera = _get_preview_camera()
    if camera is None:
        print("dxcam: no output device")
        preview_enabled.clear()
        return

    try:
        camera.start(target_fps=30, video_mode=True)
        while preview_enabled.is_set():
            frame = camera.get_latest_frame()
            if frame is None:
                time.sleep(0.005)
                continue

            small = cv2.resize(frame, (PREVIEW_W, PREVIEW_H),
                               interpolation=cv2.INTER_AREA)
            with preview_frame_lock:
                preview_frame = small

    finally:
        try:
            camera.stop()
        except Exception:
            pass


def toggle_preview():
    """Start or stop the preview thread and resize the window accordingly.

    Called from the UI thread. It is intentionally a no-op while a recording
    is active: the preview window itself would otherwise appear in the
    captured frames since `mss` grabs the whole screen.
    """
    global preview_thread, preview_frame

    if recording:
        return

    # ---- stop ----
    if preview_enabled.is_set():
        preview_enabled.clear()
        if preview_thread is not None and preview_thread.is_alive():
            preview_thread.join(timeout=2.0)
        with preview_frame_lock:
            preview_frame = None
        preview_container.pack_forget()
        window.geometry(f"{WINDOW_W}x{WINDOW_H_BASE}")
        return

    # Guard against a stale thread that has not finished joining yet.
    if preview_thread is not None and preview_thread.is_alive():
        return

    # ---- start ----
    preview_container.pack(side=tkinter.TOP, pady=(0, 10))
    window.geometry(f"{WINDOW_W}x{WINDOW_H_PREVIEW}")
    preview_enabled.set()
    preview_thread = threading.Thread(target=preview_loop, daemon=True)
    preview_thread.start()


def _get_preview_camera():
    """Return a single lazily-created dxcam camera reused across toggles."""
    global preview_camera
    with preview_camera_lock:
        if preview_camera is None:
            preview_camera = dxcam.create(output_idx=0, output_color="BGR")
        return preview_camera


# --------------------------------------------------------------------------
# Temp directory management
# --------------------------------------------------------------------------
_tempdirs = []


def _cleanup_temps():
    """Remove any leftover session directories on interpreter exit."""
    for d in _tempdirs:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_temps)


# --------------------------------------------------------------------------
# Capture thread
# --------------------------------------------------------------------------
def capture():
    """Grab the screen at a steady cadence and push BGR frames into `Frames`.

    A drifting wall-clock schedule is used instead of `time.sleep(interval)`
    so that any one slow grab does not permanently shift the frame rate. If
    the encoder falls more than one frame behind, the schedule resets rather
    than bursting to catch up.
    """
    global dropped_frames
    frame_interval = 1 / fps
    need_resize = (output_width, output_height) != (capture_width, capture_height)

    with mss.MSS() as screen:
        next_frame_time = time.perf_counter()
        capture_ready.set()

        while not stop_event.is_set():
            if pause_event.is_set():
                next_frame_time = time.perf_counter()
                time.sleep(0.02)
                continue

            screenshot = screen.grab(monitor)
            frame = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR)

            if need_resize:
                frame = cv2.resize(frame, (output_width, output_height),
                                   interpolation=cv2.INTER_LINEAR)

            try:
                Frames.put_nowait(frame)
            except queue.Full:
                dropped_frames += 1

            next_frame_time += frame_interval
            sleep_time = next_frame_time - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -frame_interval:
                next_frame_time = time.perf_counter()


# --------------------------------------------------------------------------
# Encoder thread
# --------------------------------------------------------------------------
def encode(proc):
    """Drain the frame queue into the FFmpeg process's stdin pipe.

    FFmpeg runs in its own process. Writing to its stdin without buffering
    (`bufsize=0` at Popen time) makes the pipe a true back-pressure channel:
    if FFmpeg cannot keep up, our writes block, the capture queue fills up,
    and frames are dropped at the source rather than silently piling up.
    """
    global encoded_frames
    while not stop_event.is_set() or not Frames.empty():
        try:
            frame = Frames.get(timeout=0.1)
        except queue.Empty:
            continue

        try:
            proc.stdin.write(frame.tobytes())
            encoded_frames += 1
        except (BrokenPipeError, OSError) as e:
            # FFmpeg died mid-session — abort cleanly and inform the user.
            print(RED + "ffmpeg pipe broke; stopping" + RESET)
            stop_event.set()
            ui_post(lambda err=e: messagebox.showerror(
                "Recording failed",
                f"Encoder stopped unexpectedly:\n{err}\n\n"
                f"The current recording was not saved."))
            break

    try:
        proc.stdin.close()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Audio thread
# --------------------------------------------------------------------------
def audio_callback(indata, frame_count, time_info, status):
    """PortAudio callback — invoked on a dedicated audio thread.

    Frames that arrive before capture is ready, or while the user is paused,
    are silently discarded so that audio and video stay in sync.
    """
    audio_started.set()

    if status:
        print(status)
    if not capture_ready.is_set():
        return
    if pause_event.is_set():
        return

    with audio_lock:
        audio_frames.append(indata.copy())


def record_audio(path):
    """Record audio into `audio_frames` until `stop_event`, then write a WAV."""
    print(RED + "recording..." + RESET)

    with sounddevice.InputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
        callback=audio_callback,
    ):
        stop_event.wait()

    with audio_lock:
        if audio_frames:
            audio = np.concatenate(audio_frames, axis=0)
        else:
            audio = np.zeros((0, CHANNELS), dtype=DTYPE)

    if len(audio) == 0:
        print(RED + "No audio captured — skipping WAV write." + RESET)
        return

    wavio.write(path, audio, SAMPLE_RATE, sampwidth=2)
    print(RED + f"Saved {len(audio) / SAMPLE_RATE:.2f}s audio" + RESET)


# --------------------------------------------------------------------------
# FFmpeg helpers
# --------------------------------------------------------------------------
def find_ffmpeg():
    """Locate ffmpeg.exe, preferring the copy bundled next to a frozen exe.

    In a Nuitka standalone build, `sys.frozen` is True and the bundled
    `ffmpeg.exe` sits in the same directory as the main executable. During
    development the function falls back to the system PATH.
    """
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
        bundled_ffmpeg = os.path.join(base_path, "ffmpeg.exe")
        if os.path.exists(bundled_ffmpeg):
            return bundled_ffmpeg

    path = shutil.which("ffmpeg")
    if not path:
        raise FileNotFoundError(
            "ffmpeg not found. Please ensure 'ffmpeg.exe' is in the same folder as this program."
        )
    return path


def detect_monitor():
    """Return the geometry of the primary monitor as a plain dict."""
    with mss.MSS() as sct:
        mon = sct.monitors[1]
    return dict(mon)


def build_record_cmd(ffmpeg_path, out_path):
    """Build the FFmpeg argument list for raw BGR frames on stdin.

    The video stream is treated as constant-framerate (`-fps_mode cfr`) so
    the output duration matches `encoded_frames / fps_output`, regardless of
    small timing jitter in the capture thread.
    """
    if format == ".mp4":
        vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                  "-pix_fmt", "yuv420p"]
    elif format == ".avi":
        vcodec = ["-c:v", "mpeg4", "-qscale:v", "3", "-pix_fmt", "yuv420p"]
    else:
        raise ValueError(f"Unsupported format: {format}")

    return [
        ffmpeg_path, "-y", "-loglevel", "info",

        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{output_width}x{output_height}",
        "-framerate", str(fps_output),
        "-i", "pipe:0",

        "-r", str(fps_output),
        "-fps_mode", "cfr",
        *vcodec,
        out_path,
    ]


# --------------------------------------------------------------------------
# Session (one recording lifecycle)
# --------------------------------------------------------------------------
def session():
    """Run a full recording: capture, encode, audio, mux, and save.

    This function runs on the `session_thread` and orchestrates all worker
    threads. It exits when `stop_event` is set (either by the Stop button
    or by the encoder detecting a broken FFmpeg pipe).
    """
    global filename, save_requested, dropped_frames, encoded_frames
    global monitor, capture_width, capture_height
    global recording

    try:
        monitor = detect_monitor()
        capture_width  = monitor["width"]
        capture_height = monitor["height"]

        tmpdir     = tempfile.mkdtemp(prefix="screenrec_")
        _tempdirs.append(tmpdir)
        video_temp = os.path.join(tmpdir, "video" + format)
        audio_temp = os.path.join(tmpdir, "audio.wav")

        # Reset any state left over from a previous session.
        with audio_lock:
            audio_frames.clear()
        while not Frames.empty():
            try:
                Frames.get_nowait()
            except queue.Empty:
                break
        dropped_frames = 0
        encoded_frames = 0

        try:
            ffmpeg_path = find_ffmpeg()
        except FileNotFoundError as e:
            print(RED + str(e) + RESET)
            return

        cmd = build_record_cmd(ffmpeg_path, video_temp)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=0)

        start_time = time.perf_counter()

        capture_thread = threading.Thread(target=capture)
        encode_thread  = threading.Thread(target=encode, args=(proc,))
        audio_thread   = threading.Thread(target=record_audio, args=(audio_temp,))

        audio_thread.start()
        if not audio_started.wait(timeout=3.0):
            print("no audio device! video only!")

        capture_thread.start()
        encode_thread.start()

        stop_event.wait()

        capture_thread.join()
        encode_thread.join()
        audio_thread.join()

        rc = proc.wait()
        elapsed = time.perf_counter() - start_time

        if rc != 0:
            print(RED + f"ffmpeg exited with code {rc}" + RESET)

        # Compute the effective (unpaused) duration for the FPS report.
        with pause_lock:
            paused_total = pause_accum
            if pause_mark is not None:
                paused_total += time.perf_counter() - pause_mark

        active = elapsed - paused_total
        if active > 0:
            print(RED + f"Average FPS: {encoded_frames / active:.2f} "
                        f"(drops: {dropped_frames}, paused {paused_total:.1f}s)" + RESET)

        if not save_requested:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return

        print(RED + "Saved:" + RESET, video_temp)

        out_path = filename + format
        have_audio = os.path.exists(audio_temp) and os.path.getsize(audio_temp) > 44

        with audio_lock:
            audio_sec = sum(a.shape[0] for a in audio_frames) / SAMPLE_RATE
        print(f"audio: {audio_sec:.2f}s  video: {encoded_frames/fps_output:.2f}s")

        try:
            if have_audio:
                # Remux video, transcode audio into the target container.
                # `aresample=async=1` keeps audio aligned to the video timeline
                # even when the two streams started a few tens of ms apart.
                merge_cmd = [
                    ffmpeg_path, "-y", "-loglevel", "error",
                    "-i", video_temp,
                    "-i", audio_temp,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-af", "aresample=async=1:min_hard_comp=0.100:first_pts=0",
                    "-c:a", "aac" if format == ".mp4" else "pcm_s16le",
                    "-shortest",
                    out_path,
                ]
                subprocess.run(merge_cmd, check=True)
            else:
                # No audio track — the encoded video is already in the target format.
                shutil.copy(video_temp, out_path)
        except Exception as e:
            print(RED + f"Save failed: {e}" + RESET)
            ui_post(lambda err=e: messagebox.showerror(
                "Save failed", f"Could not write {out_path}:\n{err}"))

        shutil.rmtree(tmpdir, ignore_errors=True)

    finally:
        recording = False
        ui_post(_ui_sync_after_session)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
import tkinter
print("-> tkinter", flush=True)
print("ALL IMPORTS OK", flush=True)
from tkinter import messagebox, filedialog

BG, PANEL, FG        = "#1e1e1e", "#2a2a2a", "#f0f0f0"
ENTRY_BG, BUTTON_BG  = "#333333", "#3a3a3a"

window = tkinter.Tk()
window.title("Screen Recorder")
window.geometry(f"{WINDOW_W}x{WINDOW_H_BASE}")
window.configure(bg=BG)


# --------------------------------------------------------------------------
# Thread-safe Tk dispatch
# --------------------------------------------------------------------------
# Tkinter is not thread-safe. Worker threads push callables onto this queue
# and the main thread drains it on a 50 ms tick, which is the officially
# supported pattern for updating Tk from background threads.
_ui_queue = queue.Queue()


def ui_post(fn):
    """Schedule `fn()` to run on the Tk main thread."""
    _ui_queue.put(fn)


def _pump_ui_queue():
    """Drain pending UI callbacks; reschedule itself on the Tk event loop."""
    while True:
        try:
            fn = _ui_queue.get_nowait()
        except queue.Empty:
            break
        try:
            fn()
        except Exception as e:
            print(RED + f"UI callback error: {e}" + RESET)
    try:
        window.after(50, _pump_ui_queue)
    except tkinter.TclError:
        pass  # window already destroyed


# --------------------------------------------------------------------------
# Worker-thread crash handler
# --------------------------------------------------------------------------
# `sys.excepthook` only fires on the main thread. Worker-thread crashes
# would otherwise be swallowed by pythonw / windowless builds, so we install
# a `threading.excepthook` that surfaces the error via a modal dialog.
def _thread_crash(args):
    try:
        print(RED + f"{args.thread.name} crashed: {args.exc_value}" + RESET)
    except Exception:
        pass
    try:
        window.after(0, lambda: messagebox.showerror(
            "Background thread crashed",
            f"{args.thread.name} crashed:\n{args.exc_value}"))
    except Exception:
        pass  # window already torn down


threading.excepthook = _thread_crash


# --------------------------------------------------------------------------
# Widget helpers
# --------------------------------------------------------------------------
def make_dropdown(parent, var, options, command):
    """Create a themed OptionMenu bound to `var` and dispatching on select."""
    om = tkinter.OptionMenu(parent, var, *options, command=command)
    om.config(bg=BUTTON_BG, fg=FG, activebackground="#505050",
              activeforeground=FG, relief="flat", highlightthickness=0,
              width=8, anchor="w")
    om["menu"].config(bg=PANEL, fg=FG, activebackground="#505050",
                      activeforeground=FG)
    return om


def make_tool_button(parent, text, fg, command, **kw):
    """Create a themed toolbar button; all buttons share the same width."""
    return tkinter.Button(
        parent, text=text, font=("Arial", 14), fg=fg,
        bg=BUTTON_BG, activebackground="#505050", activeforeground=FG,
        relief="flat", width=3, padx=3, pady=3,
        command=command, **kw)


# --------------------------------------------------------------------------
# Toolbar
# --------------------------------------------------------------------------
row = tkinter.Frame(window, bg=BG)
row.pack(side=tkinter.TOP, pady=20)

tkinter.Label(row, text="filename: ", bg=BG, fg=FG).pack(side=tkinter.LEFT)
entry = tkinter.Entry(row, width=40, bg=ENTRY_BG, fg=FG,
                      insertbackground=FG, selectbackground="#555555",
                      relief="flat")
entry.pack(side="left", padx=5)


# --------------------------------------------------------------------------
# Record / Stop / Pause state machine
# --------------------------------------------------------------------------
def start_record():
    """Validate inputs, initialise session state, and launch `session()`."""
    global session_thread, recording, filename, save_requested, pause_mark, pause_accum
    global preview_was_on

    if recording or recording_finishing:
        return

    if not format:
        messagebox.showwarning("Format not set", "Pick a format from the dropdown.")
        return

    name = entry.get().strip()
    if not name:
        messagebox.showwarning("Missing filename", "Please enter a filename.")
        return

    try:
        find_ffmpeg()
    except FileNotFoundError as e:
        messagebox.showerror("ffmpeg missing", str(e))
        return

    # Sanitise the filename so Windows can always write it.
    filename = re.sub(r'[<>:"/\\|?*]', "_", name)
    save_requested = False
    stop_event.clear()
    pause_event.clear()
    audio_started.clear()
    capture_ready.clear()
    pause_accum = 0.0
    pause_mark = None
    pause_btn.config(state="normal", text="⏸", font=("Arial", 14), fg="#ff5555",
                     bg=BUTTON_BG, activebackground="#505050", activeforeground=FG,
                     relief="flat")

    # Hide the preview while recording so it doesn't appear in the capture.
    preview_was_on = preview_enabled.is_set()
    if preview_was_on:
        toggle_preview()

    recording = True
    session_thread = threading.Thread(target=session, daemon=True)
    session_thread.start()


def stop_recording():
    """Prompt for an output path, then signal the session to terminate."""
    global recording, filename, save_requested, recording_finishing

    if not recording or recording_finishing:
        return

    # Pause so that the *current* frame is what gets held while the dialog
    # is open; this prevents a burst of extra frames during the save prompt.
    if not pause_event.is_set():
        toggle_pause()

    save_path = filedialog.asksaveasfilename(
        parent=window, title="Save recording",
        initialfile=entry.get().strip() or "recording",
        defaultextension=format,
        filetypes=[(f"{format[1:].upper()} video", f"*{format}"),
                   ("All files", "*.*")],
    )

    if save_path:
        filename = os.path.splitext(save_path)[0]
        save_requested = True
    else:
        save_requested = False

    stop_event.set()
    pause_event.clear()
    pause_btn.config(state="disabled", text="⏸", font=("Arial", 14), fg="#ff5555",
                     bg=BUTTON_BG, activebackground="#505050", activeforeground=FG,
                     relief="flat")
    recording = False
    recording_finishing = True
    _poll_session_done()


def _ui_sync_after_session():
    """Restore the toolbar to its idle state after a session completes.

    Safe to call more than once — every write below is idempotent, so the
    poller and `session()`'s `finally` block can both invoke it.
    """
    global recording_finishing, preview_was_on

    recording_finishing = False

    if pause_btn["state"] == "normal":
        pause_btn.config(state="disabled", text="⏸",
                         fg="#ff5555", bg=BUTTON_BG,
                         activebackground="#505050",
                         activeforeground=FG, relief="flat")

    if preview_was_on and not preview_enabled.is_set():
        toggle_preview()
    preview_was_on = False


def _poll_session_done():
    """Poll until the session thread exits, then sync the UI.

    Keeps Tk responsive while FFmpeg finishes the mux (which can take a few
    seconds for long recordings).
    """
    if session_thread and session_thread.is_alive():
        window.after(50, _poll_session_done)
        return
    _ui_sync_after_session()


def close_app():
    """Terminate any running session and close the window."""
    global recording, recording_finishing

    stop_event.set()
    recording = False
    recording_finishing = False
    pause_event.clear()

    preview_enabled.clear()
    if preview_thread is not None and preview_thread.is_alive():
        preview_thread.join(timeout=2.0)

    if session_thread and session_thread.is_alive():
        session_thread.join(timeout=5.0)

    window.destroy()


def toggle_pause():
    """Flip the pause state and update the pause button glyph."""
    global pause_mark, pause_accum

    if not recording:
        return

    with pause_lock:
        if pause_event.is_set():
            # ---- resume ----
            pause_event.clear()
            if pause_mark is not None:
                pause_accum += time.perf_counter() - pause_mark
                pause_mark = None
            pause_btn.config(text="⏸", font=("Arial", 14), fg="#ff5555",
                             bg=BUTTON_BG, activebackground="#505050",
                             activeforeground=FG, relief="flat")
            print("resumed")
        else:
            # ---- pause ----
            pause_mark = time.perf_counter()
            pause_event.set()
            pause_btn.config(text="▶", font=("Arial", 14), fg="#ff5555",
                             bg=BUTTON_BG, activebackground="#505050",
                             activeforeground=FG, relief="flat")
            print("paused")


# ---- Toolbar buttons ----
make_tool_button(row, "●", "#ff5555", start_record).pack(side="left", padx=5)

pause_btn = make_tool_button(row, "⏸", "#ff5555", toggle_pause, state="disabled")
pause_btn.pack(side="left", padx=5)

make_tool_button(row, "■", "#ff5555", stop_recording).pack(side="left", padx=5)

make_tool_button(row, "👁", "#ff5555", toggle_preview).pack(side="left", padx=5)

make_tool_button(row, "x", "#ff5555", close_app).pack(side="left", padx=5)


# --------------------------------------------------------------------------
# Format / quality controls
# --------------------------------------------------------------------------
input_frame = tkinter.Frame(window, bg=BG)
input_frame.pack(side=tkinter.TOP, pady=20)

FORMAT_MAP  = {"MP4": ".mp4", "AVI": ".avi"}
QUALITY_MAP = {"1080p": (1920, 1080), "720p": (1280, 720), "480p": (854, 480)}

format_var  = tkinter.StringVar(master=window, value="MP4")
quality_var = tkinter.StringVar(master=window, value="1080p")


def on_format_selected(value):
    """Update the module-level `format` when the dropdown changes."""
    global format
    format = FORMAT_MAP[value]


def on_quality_selected(value):
    """Update the output resolution when the dropdown changes."""
    global output_width, output_height
    output_width, output_height = QUALITY_MAP[value]


tkinter.Label(input_frame, text="format: ", bg=BG, fg=FG).pack(side=tkinter.LEFT)
make_dropdown(input_frame, format_var, list(FORMAT_MAP),
              on_format_selected).pack(side=tkinter.LEFT, padx=(0, 20))

tkinter.Label(input_frame, text="quality: ", bg=BG, fg=FG).pack(side=tkinter.LEFT)
make_dropdown(input_frame, quality_var, list(QUALITY_MAP),
              on_quality_selected).pack(side=tkinter.LEFT)

# Seed the globals from the dropdown defaults so the first session starts
# with a valid format and resolution even if the user never touches them.
on_format_selected(format_var.get())
on_quality_selected(quality_var.get())


# --------------------------------------------------------------------------
# Embedded preview widget
# --------------------------------------------------------------------------
preview_container = tkinter.Frame(window, bg="#000000",
                                  width=PREVIEW_W, height=PREVIEW_H)
preview_container.pack_propagate(False)     # pin to PREVIEW_W × PREVIEW_H

preview_label = tkinter.Label(preview_container, bg="#000000")
preview_label.pack(fill="both", expand=True)

preview_photo = None                        # keep a reference so Tk doesn't GC the image


def update_preview():
    """Push the latest preview frame to the label, ~33 times per second.

    Runs on the Tk main thread (scheduled with `window.after`) and reads the
    shared frame under `preview_frame_lock` so it is safe with `preview_loop`.
    """
    global preview_photo

    if not preview_enabled.is_set():
        if preview_photo is not None:
            preview_photo = None
            preview_label.config(image="")
    else:
        with preview_frame_lock:
            frame = preview_frame
        if frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            preview_photo = ImageTk.PhotoImage(img)
            preview_label.config(image=preview_photo)

    window.after(30, update_preview)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
update_preview()
_pump_ui_queue()

try:
    window.mainloop()
except Exception:
    import traceback
    traceback.print_exc()
    input("Press Enter to exit...")