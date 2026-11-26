import sys, traceback
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
import soundcard as sc
print("-> soundcard", flush=True)
import wavio
print("-> wavio", flush=True)
import subprocess
print("-> subprocess", flush=True)
import ctypes
import os
import re
import shutil
import tempfile
import atexit

if os.name == "nt":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        pass

import colorama

colorama.just_fix_windows_console()
RED = "\033[31m"
RESET = "\033[0m"

# ---- video settings ----
capture_width, capture_height = 1920, 1080 #fallback, overridden in session()
output_width,  output_height  = 1920, 1080
fps = 30.5
fps_output = 30
monitor = {"top": 0, "left": 0, "width": capture_width, "height": capture_height}
format = ""          # ".mp4" or ".avi", set by UI

# ---- audio settings ----
SAMPLE_RATE = 44100
CHANNELS    = 2
DTYPE       = "int16"
mic_frames  = []
loop_frames = []
audio_lock   = threading.Lock()

audio_source_devices = []        # [(device_index, label), ...] populated at startup
selected_mic_idx     = None      # device index for the mic (None = default)
selected_loopback_idx = None     # device index for system-audio loopback (None = unavailable)
audio_mode           = "mic"     # "mic" | "system" | "mic+system"

# ---- shared state ----
filename        = ""
Frames          = queue.Queue(maxsize=4)
stop_event      = threading.Event()
pause_event = threading.Event()
pause_accum = 0.0
pause_mark = None
pause_lock = threading.Lock()
audio_started = threading.Event()
capture_ready = threading.Event()
session_thread  = None
recording       = False
recording_finishing = False     
preview_was_on      = False 
save_requested  = False
dropped_frames  = 0
encoded_frames  = 0
compress_final = False

# ---- preview setting ----
import dxcam
from PIL import Image, ImageTk

preview_enabled     = threading.Event()
preview_thread      = None
preview_frame       = None            # latest BGR numpy frame
preview_frame_lock  = threading.Lock()

preview_cameras      = {}             # dict: dxcam output_idx -> camera (one per monitor)
preview_camera_lock  = threading.Lock()
_preview_switching   = threading.Event()

selected_monitor_idx = 1              # 1-based, matches mss.monitors indexing

PREVIEW_W, PREVIEW_H = 740, 360
WINDOW_W             = 850
WINDOW_H_BASE        = 220
WINDOW_H_PREVIEW     = WINDOW_H_BASE + PREVIEW_H + 20

def preview_loop():
    global preview_frame

    # mss uses 1-based indices; dxcam uses 0-based
    output_idx = max(0, selected_monitor_idx - 1)
    camera = _get_preview_camera(output_idx)
    if camera is None:
        print("dxcam: no output device")
        preview_enabled.clear()
        ui_post(lambda: (preview_container.pack_forget(),
                         window.geometry(f"{WINDOW_W}x{WINDOW_H_BASE}")))
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
    global preview_thread, preview_frame

    # Called internally by start_record() (before recording=True) to hide
    # the preview, and by _ui_sync_after_session() (after recording=False)
    # to restore it. In both cases recording is False, so the guard below
    # only blocks the user clicking 👁 while a session is active — which
    # is exactly what we want: the preview would otherwise land in the
    # recording because capture() grabs the whole screen.
    if recording:
        return

    if preview_enabled.is_set():
        preview_enabled.clear()
        if preview_thread is not None and preview_thread.is_alive():
            preview_thread.join(timeout=2.0)
        with preview_frame_lock:
            preview_frame = None
        preview_container.pack_forget()
        window.geometry(f"{WINDOW_W}x{WINDOW_H_BASE}")
        return

    if preview_thread is not None and preview_thread.is_alive():
        return
    
    preview_container.pack(side=tkinter.TOP, pady=(0, 10))
    window.geometry(f"{WINDOW_W}x{WINDOW_H_PREVIEW}")
    preview_enabled.set()
    preview_thread = threading.Thread(target=preview_loop, daemon=True)
    preview_thread.start()

def _switch_preview_monitor():
    """Restart the preview thread for the newly selected monitor.

    Runs the stop/start in a background thread so the Tk main thread is not
    blocked while dxcam releases the old D3D device and the new one starts.
    """
    global preview_thread

    if not preview_enabled.is_set():
        return
    if _preview_switching.is_set():
        return                         # a switch is already in flight

    _preview_switching.set()

    def _do_switch():
        global preview_thread
        try:
            preview_enabled.clear()
            if preview_thread is not None and preview_thread.is_alive():
                preview_thread.join(timeout=2.0)
            with preview_frame_lock:
                preview_frame = None
            preview_enabled.set()
            preview_thread = threading.Thread(target=preview_loop, daemon=True)
            preview_thread.start()
        finally:
            _preview_switching.clear()

    threading.Thread(target=_do_switch, daemon=True).start()

def _get_preview_camera(output_idx):
    """Return the cached dxcam camera for the given 0-based output index.

    One camera is created per monitor and reused for the lifetime of the
    process. Returns None if dxcam cannot create a camera for that output
    (e.g. a headless or disconnected display).
    """
    global preview_cameras
    with preview_camera_lock:
        if output_idx not in preview_cameras:
            try:
                preview_cameras[output_idx] = dxcam.create(
                    output_idx=output_idx, output_color="BGR")
            except Exception as e:
                print(f"dxcam: failed to create camera for output {output_idx}: {e}")
                return None
        return preview_cameras[output_idx]
   

# ---- temp dir cleanup ----
_tempdirs = []
def _cleanup_temps():
    for d in _tempdirs:
        shutil.rmtree(d, ignore_errors=True)
atexit.register(_cleanup_temps)

# ---- capture thread ----
def capture():
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
                # fell behind -> reset schedule instead of burst-catchup
                next_frame_time = time.perf_counter()

# ---- encoder thread: pipe raw frames to ffmpeg ----
def encode(proc):
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

# ---- audio thread ----
def _audio_callback(indata, frame_count, time_info, status, target):
    audio_started.set()
    if status:
        print(status)
    if not capture_ready.is_set() or pause_event.is_set():
        return
    with audio_lock:
        target.append(indata.copy())

def enumerate_audio_devices():
    global audio_source_devices, selected_mic_idx

    try:
        devices = sounddevice.query_devices()
    except Exception:
        devices = []

    mics = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            # Exclude Stereo Mix from the mic list just in case it exists
            if "stereo mix" not in d["name"].lower():
                mics.append(i)

    selected_mic_idx = mics[0] if mics else None

    audio_source_devices = []
    if selected_mic_idx is not None:
        audio_source_devices.append(("Microphone (default)", "mic"))
    
    # System audio is ALWAYS available now, thanks to WASAPI
    audio_source_devices.append(("System audio (WASAPI)", "system"))
    audio_source_devices.append(("Microphone + System audio", "mic+system"))
        
    for i in mics[1:]:
        audio_source_devices.append((f"Mic: {devices[i]['name'][:40]}", i))

def _record_system_audio_thread():
    """Continuously record system audio using WASAPI loopback."""
    # Initialize COM for this thread
    ctypes.windll.ole32.CoInitialize(None)
    
    try:
        # Get the default speaker
        speaker = sc.default_speaker()
        
        # Fetch the loopback microphone for this speaker
        loopback_mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        
        if not loopback_mic:
            print(RED + "Could not find loopback microphone for default speaker." + RESET)
            return

        # Record from the loopback microphone
        with loopback_mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS) as recorder:
            while not stop_event.is_set():
                # Record 1024 frames
                data = recorder.record(numframes=1024)
                
                if pause_event.is_set():
                    continue

                # soundcard returns float32 (from -1.0 to 1.0), convert to int16
                chunk_int16 = (data * 32767).astype(np.int16)
                
                with audio_lock:
                    loop_frames.append(chunk_int16)
                    
    except Exception as e:
        print(RED + f"WASAPI Loopback failed: {e}" + RESET)
    finally:
        # Clean up COM
        ctypes.windll.ole32.CoUninitialize()

def record_audio(path):
    """Record from the selected source (mic, system loopback, or both)."""
    print(RED + "recording..." + RESET)

    streams = []
    system_thread = None

    # 1. Setup Microphone (still uses sounddevice)
    if audio_mode in ("mic", "mic+system") and selected_mic_idx is not None:
        streams.append(sounddevice.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
            device=selected_mic_idx,
            callback=lambda *a: _audio_callback(*a, target=mic_frames),
        ))

    # 2. Setup System Audio (using soundcard)
    if audio_mode in ("system", "mic+system"):
        system_thread = threading.Thread(target=_record_system_audio_thread, daemon=True)
        system_thread.start()

    if not streams and system_thread is None:
        print(RED + "No usable audio device for the selected source." + RESET)
        audio_started.set()
        return

    # 3. Start Recording
    for s in streams:
        s.start()
    
    audio_started.set() # Signal that audio is running

    try:
        stop_event.wait()
    finally:
        # 4. Stop everything
        for s in streams:
            try: s.stop()
            except: pass
            try: s.close()
            except: pass
            
        # The system_thread will exit automatically because stop_event is set
        
    # 5. Mix the buffers (unchanged from your current code)
    with audio_lock:
        mic  = np.concatenate(mic_frames,  axis=0) if mic_frames  else np.zeros((0, CHANNELS), np.int16)
        loop = np.concatenate(loop_frames, axis=0) if loop_frames else np.zeros((0, CHANNELS), np.int16)

    if len(mic) and len(loop):
        n = min(len(mic), len(loop))
        mixed = mic[:n].astype(np.int32) + (loop[:n] * 0.7).astype(np.int32)
        audio = np.clip(mixed, -32768, 32767).astype(np.int16)
    elif len(mic):
        audio = mic
    elif len(loop):
        audio = loop
    else:
        audio = np.zeros((0, CHANNELS), np.int16)

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

    # Dev mode: also check next to the script
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
    if os.path.exists(local):
        return local

    path = shutil.which("ffmpeg")
    if not path:
        raise FileNotFoundError(
            "ffmpeg not found. Please ensure 'ffmpeg.exe' is in the same folder as this program."
        )
    return path

def detect_monitor():
    """Return the currently selected monitor's geometry as a plain dict."""
    with mss.MSS() as sct:
        idx = selected_monitor_idx
        if not (1 <= idx < len(sct.monitors)):
            idx = 1                    # fall back to primary
        mon = sct.monitors[idx]
    return dict(mon)

def build_record_cmd(ffmpeg_path, out_path):
    if format == ".mp4":
        vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                  "-pix_fmt", "yuv420p"]
    elif format == ".mkv":
        vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                  "-pix_fmt", "yuv420p"]
    elif format == ".mov":
        vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                  "-pix_fmt", "yuv420p"]
    elif format == ".avi":
        vcodec = ["-c:v", "mpeg4", "-qscale:v", "7", "-pix_fmt", "yuv420p"]
    elif format == ".ts":
        vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                  "-pix_fmt", "yuv420p"]
    elif format == ".flv":
        vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                  "-pix_fmt", "yuv420p", "-f", "flv"]
    elif format == ".webm":
        vcodec = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
                  "-b:v", "0", "-crf", "32", "-pix_fmt", "yuv420p"]
    elif format == ".wmv":
        vcodec = ["-c:v", "wmv2", "-qscale:v", "4", "-pix_fmt", "yuv420p", "-f", "asf"]
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

def build_compress_cmd(ffmpeg_path, in_video, in_audio, out_path, has_audio):
    """Re-encode the saved file for maximum compression.

    Returns None if the current format has no defined compression target.
    Uses slow-preset H.264 for MP4-family containers, aggressive MPEG-4 for
    AVI, etc. — always traded against save time, never against capture FPS.
    """
    if format in (".mp4", ".mkv", ".mov", ".ts", ".flv"):
        vcodec = ["-c:v", "libx264", "-preset", "slow", "-crf", "30",
                  "-pix_fmt", "yuv420p"]
        acodec = ["-c:a", "aac", "-b:a", "96k"]
    elif format == ".avi":
        vcodec = ["-c:v", "mpeg4", "-qscale:v", "12", "-pix_fmt", "yuv420p"]
        acodec = ["-c:a", "pcm_s16le"]
    elif format == ".webm":
        vcodec = ["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "2",
                  "-b:v", "0", "-crf", "42", "-pix_fmt", "yuv420p"]
        acodec = ["-c:a", "libopus", "-b:a", "64k"]
    elif format == ".wmv":
        vcodec = ["-c:v", "wmv2", "-qscale:v", "8", "-pix_fmt", "yuv420p"]
        acodec = ["-c:a", "wmav2", "-b:a", "96k"]
    else:
        return None

    cmd = [ffmpeg_path, "-y", "-loglevel", "error", "-i", in_video]

    if has_audio:
        cmd += [
            "-i", in_audio,
            "-map", "0:v:0", "-map", "1:a:0",
            *vcodec,
            "-af", "aresample=async=1:min_hard_comp=0.100:first_pts=0",
            *acodec,
            "-shortest",
        ]
    else:
        cmd += vcodec

    cmd.append(out_path)
    return cmd

# ---- main function ----
def session():
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

        with audio_lock:
            mic_frames.clear()
            loop_frames.clear()
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
        # bufsize=0 -> unbuffered stdin, so capture back-pressure is real
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
        with pause_lock:
            paused_total = pause_accum
            if pause_mark is not None:            # stopped while still paused
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
        if have_audio:
            audio_sec = (os.path.getsize(audio_temp) - 44) / (SAMPLE_RATE * CHANNELS * 2)
        else:
            audio_sec = 0.0
        print(f"audio: {audio_sec:.2f}s  video: {encoded_frames/fps_output:.2f}s")
        
        try:
            compress_cmd = (build_compress_cmd(ffmpeg_path, video_temp, audio_temp,
                                               out_path, have_audio)
                            if compress_final else None)

            if compress_cmd:
                print(RED + "Compressing (this may take a while)..." + RESET)
                try:
                    in_mb = os.path.getsize(video_temp) / (1024 * 1024)
                except OSError:
                    in_mb = None
                ui_post(lambda: window.title("Screen Recorder — Compressing..."))
                subprocess.run(compress_cmd, check=True)
                ui_post(lambda: window.title("Screen Recorder"))
                try:
                    out_mb = os.path.getsize(out_path) / (1024 * 1024)
                    if in_mb is not None and in_mb > 0:
                        pct = (1 - out_mb / in_mb) * 100
                        print(RED + f"Compression finished: {out_mb:.1f} MB "
                                    f"({pct:.0f}% smaller)" + RESET)
                    else:
                        print(RED + f"Compression finished: {out_mb:.1f} MB" + RESET)
                except OSError:
                    print(RED + "Compression finished" + RESET)
            elif have_audio:
                if format == ".webm":
                    acodec = ["-c:a", "libopus", "-b:a", "128k"]
                elif format == ".wmv":
                    acodec = ["-c:a", "wmav2", "-b:a", "128k"]
                elif format == ".avi":
                    acodec = ["-c:a", "pcm_s16le"]
                else:
                    acodec = ["-c:a", "aac", "-b:a", "128k"]

                merge_cmd = [
                    ffmpeg_path, "-y", "-loglevel", "error",
                    "-i", video_temp,
                    "-i", audio_temp,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-af", "aresample=async=1:min_hard_comp=0.100:first_pts=0",
                    *acodec,
                    "-shortest",
                    out_path,
                ]
                subprocess.run(merge_cmd, check=True)
            else:
                shutil.copy(video_temp, out_path)
        except Exception as e:
            ui_post(lambda: window.title("Screen Recorder"))
            print(RED + f"Save failed: {e}" + RESET)
            ui_post(lambda err=e: messagebox.showerror(
                "Save failed", f"Could not write {out_path}:\n{err}"))

        shutil.rmtree(tmpdir, ignore_errors=True)

    finally:
        recording = False
        ui_post(_ui_sync_after_session)

enumerate_audio_devices()

# ---- UI ----
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

# ---- thread-safe UI dispatch ----
# Tkinter is not thread-safe. Worker threads (session, encode, etc.) that
# need to pop a dialog or run Tk calls push a callable onto this queue, and
# the main thread drains it on a 50 ms tick. This is the officially safe
# pattern — no direct Tk calls from non-main threads.
_ui_queue = queue.Queue()


def ui_post(fn):
    """Thread-safe: arrange for `fn()` to run on the Tk main thread."""
    _ui_queue.put(fn)


def _pump_ui_queue():
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
        pass   # window destroyed — stop pumping

# ---- surface worker-thread crashes ----
# sys.excepthook only fires for the main thread. session(), capture(),
# encode(), audio_callback(), record_audio() and preview_loop() run on
# worker threads — an uncaught exception there would otherwise only hit
# stderr (invisible if launched via pythonw).
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
        pass   # window already torn down

threading.excepthook = _thread_crash

# ---- dropdown helper (styled for the dark theme) ----
def make_dropdown(parent, var, options, command, width=8):
    """Create a themed OptionMenu bound to `var` and dispatching on select."""
    om = tkinter.OptionMenu(parent, var, *options, command=command)
    om.config(bg=BUTTON_BG, fg=FG, activebackground="#505050",
              activeforeground=FG, relief="flat", highlightthickness=0,
              width=width, anchor="w")
    om["menu"].config(bg=PANEL, fg=FG, activebackground="#505050",
                      activeforeground=FG)
    return om

def make_tool_button(parent, text, fg, command, **kw):
    """All toolbar buttons share the same explicit width so they match."""
    return tkinter.Button(
        parent, text=text, font=("Arial", 14), fg=fg,
        bg=BUTTON_BG, activebackground="#505050", activeforeground=FG,
        relief="flat", width=3, padx=3, pady=3,
        command=command, **kw)

row = tkinter.Frame(window, bg=BG)
row.pack(side=tkinter.TOP, pady=20)

tkinter.Label(row, text="filename: ", bg=BG, fg=FG).pack(side=tkinter.LEFT)
entry = tkinter.Entry(row, width=40, bg=ENTRY_BG, fg=FG,
                      insertbackground=FG, selectbackground="#555555",
                      relief="flat")
entry.pack(side="left", padx=5)

def start_record():
    global session_thread, recording, filename, save_requested, pause_mark, pause_accum
    global preview_was_on, compress_final

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

    filename = re.sub(r'[<>:"/\\|?*]', "_", name)
    save_requested = False
    stop_event.clear()
    pause_event.clear()
    audio_started.clear()
    capture_ready.clear()
    pause_accum = 0.0
    pause_mark = None
    pause_btn.config(state="normal", text="⏸",font=("Arial", 14), fg="#ff5555",
               bg=BUTTON_BG, activebackground="#505050", activeforeground=FG,
               relief="flat")

    preview_was_on = preview_enabled.is_set()
    if preview_was_on:
        toggle_preview()

    compress_final = compress_var.get()
    recording = True
    session_thread = threading.Thread(target=session, daemon=True)
    session_thread.start()

def stop_recording():
    global recording, filename, save_requested, recording_finishing

    if not recording or recording_finishing:
        return

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
    """Restore the toolbar to idle. Safe to call more than once — every
    write below is idempotent, so double-invocation (e.g., both the poller
    and session()'s finally firing) is harmless."""
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
    """Non-blocking wait for session_thread to drain — keeps Tk responsive
    while ffmpeg finishes the merge."""
    if session_thread and session_thread.is_alive():
        window.after(50, _poll_session_done)
        return
    _ui_sync_after_session()

def close_app():
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
    global pause_mark, pause_accum
    if not recording:
        return
    with pause_lock:
        if pause_event.is_set():
            pause_event.clear()
            if pause_mark is not None:
                pause_accum += time.perf_counter() - pause_mark
                pause_mark = None
            pause_btn.config(text="⏸",font=("Arial", 14), fg="#ff5555",
               bg=BUTTON_BG, activebackground="#505050", activeforeground=FG,
               relief="flat")
            print("resumed")
        else:
            pause_mark = time.perf_counter()
            pause_event.set()
            pause_btn.config(text="▶", font=("Arial", 14), fg="#ff5555",
               bg=BUTTON_BG, activebackground="#505050", activeforeground=FG,
               relief="flat")
            print("paused")


make_tool_button(row, "●", "#ff5555", start_record).pack(side="left", padx=5)

pause_btn = make_tool_button(row, "⏸", "#ff5555", toggle_pause, state="disabled")
pause_btn.pack(side="left", padx=5)

make_tool_button(row, "■", "#ff5555", stop_recording).pack(side="left", padx=5)

make_tool_button(row, "👁", "#ff5555", toggle_preview).pack(side="left", padx=5)

make_tool_button(row, "x", "#ff5555", close_app).pack(side="left", padx=5)


input_frame = tkinter.Frame(window, bg=BG)
input_frame.pack(side=tkinter.TOP, pady=20)

FORMAT_MAP = {
    "MP4":  ".mp4",
    "MKV":  ".mkv",
    "MOV":  ".mov",
    "AVI":  ".avi",
    "TS":   ".ts",
    "FLV":  ".flv",
    "WEBM": ".webm",
    "WMV":  ".wmv",
}
QUALITY_MAP = {"1080p": (1920, 1080), "720p": (1280, 720), "480p": (854, 480)}

format_var  = tkinter.StringVar(master=window, value="MP4")
quality_var = tkinter.StringVar(master=window, value="1080p")

def on_format_selected(value):
    global format
    format = FORMAT_MAP[value]

def on_quality_selected(value):
    global output_width, output_height
    output_width, output_height = QUALITY_MAP[value]

tkinter.Label(input_frame, text="format: ", bg=BG, fg=FG).pack(side=tkinter.LEFT)
make_dropdown(input_frame, format_var, list(FORMAT_MAP),
              on_format_selected).pack(side=tkinter.LEFT, padx=(0, 20))

tkinter.Label(input_frame, text="quality: ", bg=BG, fg=FG).pack(side=tkinter.LEFT)
make_dropdown(input_frame, quality_var, list(QUALITY_MAP),
              on_quality_selected).pack(side=tkinter.LEFT)

# ---- extra compression toggle ----
compress_var = tkinter.BooleanVar(master=window, value=False)
tkinter.Checkbutton(
    input_frame, text="Compress more (takes a while to compress!)",
    variable=compress_var, bg=BG, fg=FG, selectcolor=BUTTON_BG,
    activebackground=BG, activeforeground=FG, font=("Arial", 10),
).pack(side=tkinter.LEFT, padx=(20, 0))

# apply the defaults immediately so `format` and `quality_set` are initialized
on_format_selected(format_var.get())
on_quality_selected(quality_var.get())

# ---- monitor selection ----
def _list_monitors():
    with mss.MSS() as sct:
        return [(i, m) for i, m in enumerate(sct.monitors) if i > 0]

_monitor_entries = _list_monitors()
if _monitor_entries:
    MONITOR_LABELS = [f"Monitor {i} ({m['width']}x{m['height']})"
                      for i, m in _monitor_entries]
else:
    MONITOR_LABELS = ["Monitor 1 (unknown)"]

monitor_var = tkinter.StringVar(master=window,
                                value=MONITOR_LABELS[0])

def on_monitor_selected(value):
    global selected_monitor_idx
    try:
        n = int(value.split()[1])
    except (IndexError, ValueError):
        return
    if n == selected_monitor_idx:
        return
    selected_monitor_idx = n
    _switch_preview_monitor()

monitor_frame = tkinter.Frame(window, bg=BG)
monitor_frame.pack(side=tkinter.TOP, pady=10, before=input_frame, anchor="w")

tkinter.Label(monitor_frame, text="monitor: ", bg=BG, fg=FG).pack(side=tkinter.LEFT)
make_dropdown(monitor_frame, monitor_var, MONITOR_LABELS,
              on_monitor_selected, width=20).pack(side=tkinter.LEFT)

AUDIO_LABELS = [label for label, _ in audio_source_devices] or ["Microphone (default)"]
audio_var = tkinter.StringVar(master=window, value=AUDIO_LABELS[0])

def on_audio_source_selected(value):
    global audio_mode, selected_mic_idx
    for label, key in audio_source_devices:
        if label == value:
            if isinstance(key, int):
                audio_mode = "mic"
                selected_mic_idx = key
            elif key == "mic":
                # reset to the first enumerated mic
                first_mic = next((k for _, k in audio_source_devices
                                  if isinstance(k, int)), selected_mic_idx)
                audio_mode = "mic"
                selected_mic_idx = first_mic
            else:
                audio_mode = key
            return

tkinter.Label(monitor_frame, text="  audio: ", bg=BG, fg=FG).pack(side=tkinter.LEFT)
make_dropdown(monitor_frame, audio_var, AUDIO_LABELS,
              on_audio_source_selected, width=24).pack(side=tkinter.LEFT)

# ---- embedded preview widget ----
preview_container = tkinter.Frame(window, bg="#000000",
                                   width=PREVIEW_W, height=PREVIEW_H)
preview_container.pack_propagate(False)   # lock to 640x360
preview_label = tkinter.Label(preview_container, bg="#000000")
preview_label.pack(fill="both", expand=True)

preview_photo = None                       # keep reference so PhotoImage isn't GC'd


def update_preview():
    """Runs on the Tk main thread ~33 times/sec; pushes frames to the label."""
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


# kick off the update loop before mainloop
update_preview()
_pump_ui_queue()

try:
    window.mainloop()
except Exception:
    import traceback; traceback.print_exc()
    input("Press Enter to exit...")