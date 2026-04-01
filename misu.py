#!/usr/bin/env python3
"""
M.I.S.U. — Machine-Initiated Startup Unit

A terminal-only macOS background mic listener.
Detects a double-clap and simultaneously opens an app + streams YouTube audio.

THIS DOES NOT AUTO-START. You run it manually, once:
    nohup python3 /Users/thirumarandeepak/Documents/misu/misu.py &> /tmp/misu.log &
To stop it:
    pkill -f misu.py
"""

import os
import sys
import time
import shutil
import subprocess
import threading
import signal
import atexit
import numpy as np
import sounddevice as sd

# ═══════════════════════════════════════════════════════════════
# ██  C O N F I G U R A T I O N  ██
# ═══════════════════════════════════════════════════════════════
APP_TO_OPEN = "Codex"
YOUTUBE_URL = "https://youtu.be/pAgnJDJN4VA?si=zAr_El1xtW_ugq8K"
CLAP_THRESHOLD = 0.15  # was 0.25, lower so it actually catches claps
DOUBLE_CLAP_WINDOW = 0.7  # Max seconds between two claps
MIN_CLAP_GAP = 0.08  # Min seconds between claps (echo reject)
COOLDOWN_AFTER_TRIGGER = 3.0  # Seconds to ignore mic after trigger
FREQ_RATIO_THRESHOLD = 0.25  # was 0.35, less aggressive filtering
MAX_SUSTAINED_CHUNKS = 6  # was 4, claps can span a few more blocks
MIN_CREST_FACTOR = 2.5  # NEW: peak/RMS ratio, claps are spiky, voice isn't
SAMPLE_RATE = 44100
BLOCK_SIZE = 1024
# ═══════════════════════════════════════════════════════════════

# ANSI color helpers
CYAN = "\033[96m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

BANNER = rf"""
{CYAN}{BOLD}
 ███╗   ███╗   ██╗   ███████╗   ██╗   ██╗
 ████╗ ████║   ██║   ██╔════╝   ██║   ██║
 ██╔████╔██║   ██║   ███████╗   ██║   ██║
 ██║╚██╔╝██║   ██║   ╚════██║   ██║   ██║
 ██║ ╚═╝ ██║ ██║██║  ███████║ ██║╚█████╔╝
 ╚═╝     ╚═╝ ╚═╝╚═╝ ╚══════╝ ╚═╝ ╚════╝
{RESET}{DIM}{CYAN}  Machine-Initiated Startup Unit  v1.0{RESET}
{DIM}{CYAN}  ─────────────────────────────────────{RESET}
"""

ACTIVATION_MSG = f"""
{YELLOW}{BOLD}
  ╔══════════════════════════════════════════╗
  ║  ⚡  DOUBLE CLAP DETECTED — ACTIVATING    ║
  ║      S Y S T E M   E N G A G E D         ║
  ╚══════════════════════════════════════════╝
{RESET}"""

# ── Global state ──
audio_player_cmd = None
trigger_lock = threading.Lock()
last_trigger_time = 0.0
first_clap_time = None
_sustained_chunk_count = 0
_cached_audio_url = None
_active_audio_proc = None
_audio_paused = False


def log(msg, color=RESET):
    timestamp = time.strftime("%H:%M:%S")
    print(f"  {DIM}[{timestamp}]{RESET} {color}{msg}{RESET}", flush=True)


def check_binary(name):
    return shutil.which(name) is not None


def resolve_audio_player():
    """Pick the best available audio player: ffplay > mpv > afplay."""
    for player in ["ffplay", "mpv", "afplay"]:
        if check_binary(player):
            return player
    return None


def check_app_exists(app_name):
    """Use osascript to check if an app bundle can be found."""
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "Finder" to get application file id (id of application "{app_name}")',
            ],
            capture_output=True,
            timeout=5,
        )
        # Fallback: just check if `open -a` would resolve
        result2 = subprocess.run(
            [
                "mdfind",
                f"kMDItemKind == 'Application' && kMDItemDisplayName == '{app_name}'",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 or result2.stdout.strip():
            return True
        # Last resort: try open -Ra which resolves without launching
        result3 = subprocess.run(
            ["open", "-Ra", app_name], capture_output=True, timeout=5
        )
        return result3.returncode == 0
    except Exception:
        # Simplest check
        try:
            r = subprocess.run(
                ["open", "-Ra", app_name], capture_output=True, timeout=5
            )
            return r.returncode == 0
        except Exception:
            return False


def run_dependency_checks():
    """Run all startup checks. Returns True if all critical deps pass."""
    global audio_player_cmd
    print(f"\n  {BOLD}{CYAN}── Dependency Checks ──{RESET}\n")
    all_ok = True

    # yt-dlp
    if check_binary("yt-dlp"):
        log("yt-dlp ................................. FOUND", GREEN)
    else:
        log("yt-dlp ................................. MISSING", RED)
        log("  Fix: brew install yt-dlp", YELLOW)
        all_ok = False

    # audio player
    audio_player_cmd = resolve_audio_player()
    if audio_player_cmd:
        log(f"audio player ({audio_player_cmd}) ..................... FOUND", GREEN)
    else:
        log("audio player (ffplay/mpv/afplay) ....... MISSING", RED)
        log("  Fix: brew install ffmpeg", YELLOW)
        all_ok = False

    # app
    if check_app_exists(APP_TO_OPEN):
        log(f"app '{APP_TO_OPEN}' .............................. FOUND", GREEN)
    else:
        log(f"app '{APP_TO_OPEN}' .............................. NOT FOUND", RED)
        log(f"  Make sure '{APP_TO_OPEN}' is installed in /Applications", YELLOW)
        all_ok = False

    # sounddevice mic access
    try:
        sd.query_devices(kind="input")
        log("mic input device ....................... FOUND", GREEN)
    except Exception as e:
        log(f"mic input device ....................... ERROR: {e}", RED)
        all_ok = False

    print()
    return all_ok


def print_config():
    print(f"  {BOLD}{CYAN}── Configuration ──{RESET}\n")
    print(f"  {DIM}APP_TO_OPEN          ={RESET} {YELLOW}{APP_TO_OPEN}{RESET}")
    print(f"  {DIM}YOUTUBE_URL          ={RESET} {YELLOW}{YOUTUBE_URL}{RESET}")
    print(f"  {DIM}CLAP_THRESHOLD       ={RESET} {YELLOW}{CLAP_THRESHOLD}{RESET}")
    print(f"  {DIM}DOUBLE_CLAP_WINDOW   ={RESET} {YELLOW}{DOUBLE_CLAP_WINDOW}{RESET}")
    print(
        f"  {DIM}COOLDOWN_AFTER_TRIGGER={RESET} {YELLOW}{COOLDOWN_AFTER_TRIGGER}s{RESET}"
    )
    print(f"  {DIM}AUDIO_PLAYER         ={RESET} {YELLOW}{audio_player_cmd}{RESET}")
    print()


def stream_youtube_audio():
    """Stream YouTube audio via yt-dlp piped into the chosen player. No disk download."""
    global _active_audio_proc
    try:
        log("Streaming YouTube audio...", CYAN)

        # Use cached URL if available for instant start
        if _cached_audio_url:
            direct_url = _cached_audio_url
        else:
            url_result = subprocess.run(
                ["yt-dlp", "-f", "bestaudio", "--get-url", YOUTUBE_URL],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if url_result.returncode != 0:
                log(f"yt-dlp URL extract failed: {url_result.stderr.strip()}", RED)
                return
            direct_url = url_result.stdout.strip()

        if audio_player_cmd == "afplay":
            import tempfile

            fifo_path = os.path.join(tempfile.gettempdir(), "misu_audio_fifo")
            try:
                os.mkfifo(fifo_path)
            except FileExistsError:
                os.remove(fifo_path)
                os.mkfifo(fifo_path)

            def curl_writer():
                subprocess.run(
                    ["curl", "-sL", direct_url, "-o", fifo_path], capture_output=True
                )

            t = threading.Thread(target=curl_writer, daemon=True)
            t.start()
            _active_audio_proc = subprocess.Popen(["afplay", fifo_path])
            _active_audio_proc.wait()
            _active_audio_proc = None
            try:
                os.remove(fifo_path)
            except OSError:
                pass

        elif audio_player_cmd == "mpv":
            _active_audio_proc = subprocess.Popen(
                ["mpv", "--no-video", "--really-quiet", direct_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _active_audio_proc.wait()
            _active_audio_proc = None

        else:
            # ffplay (default / best)
            _active_audio_proc = subprocess.Popen(
                [
                    "ffplay",
                    "-nodisp",
                    "-autoexit",
                    "-loglevel",
                    "quiet",
                    "-i",
                    direct_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _active_audio_proc.wait()
            _active_audio_proc = None

        log("Audio playback finished.", DIM)

    except Exception as e:
        log(f"Audio stream error: {e}", RED)
        _active_audio_proc = None


def show_image():
    img_path = "/Users/thirumarandeepak/Documents/misu/jarvis.jpeg"
    if os.path.isfile(img_path):
        log("Opening jarvis image...", CYAN)
        subprocess.call(["open", img_path])
        log("Image opened.", GREEN)
    else:
        log(f"Image not found: {img_path}", YELLOW)


def prefetch_audio_url():
    """Pre-fetch the direct audio URL at startup so playback is instant."""
    global _cached_audio_url
    try:
        log("Pre-fetching audio URL...", DIM)
        result = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "--get-url", YOUTUBE_URL],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            _cached_audio_url = result.stdout.strip()
            log("Audio URL cached.", GREEN)
        else:
            log(f"Pre-fetch failed: {result.stderr.strip()}", YELLOW)
    except Exception as e:
        log(f"Pre-fetch error: {e}", YELLOW)


def toggle_pause():
    """Pause or resume currently playing audio."""
    global _audio_paused
    if not _active_audio_proc or _active_audio_proc.poll() is not None:
        log("No audio playing.", DIM)
        return
    if _audio_paused:
        os.kill(_active_audio_proc.pid, signal.SIGCONT)
        _audio_paused = False
        log("Audio resumed.", GREEN)
    else:
        os.kill(_active_audio_proc.pid, signal.SIGSTOP)
        _audio_paused = True
        log("Audio paused.", YELLOW)


def cleanup():
    """Kill audio subprocess on exit — including crashes."""
    global _active_audio_proc
    if _active_audio_proc and _active_audio_proc.poll() is None:
        _active_audio_proc.terminate()
        try:
            _active_audio_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _active_audio_proc.kill()


def open_app():
    """Open the configured app via macOS open command."""
    try:
        log(f"Opening {APP_TO_OPEN}...", CYAN)
        subprocess.run(["open", "-a", APP_TO_OPEN], check=True, capture_output=True)
        log(f"{APP_TO_OPEN} launched.", GREEN)
    except Exception as e:
        log(f"Failed to open {APP_TO_OPEN}: {e}", RED)


def on_trigger():
    """Fired on double-clap detection. Launches app + audio simultaneously."""
    global last_trigger_time
    now = time.time()

    if not trigger_lock.acquire(blocking=False):
        return
    try:
        if now - last_trigger_time < COOLDOWN_AFTER_TRIGGER:
            return
        last_trigger_time = now

        print(ACTIVATION_MSG, flush=True)

        # Launch all in parallel threads so none blocks the listener
        t_audio = threading.Thread(target=stream_youtube_audio, daemon=True)
        t_app = threading.Thread(target=open_app, daemon=True)
        t_image = threading.Thread(target=show_image, daemon=True)
        t_audio.start()
        t_app.start()
        t_image.start()
        # Don't join — let them run, we return to listening after cooldown

    finally:
        trigger_lock.release()


def audio_callback(indata, frames, time_info, status):
    global first_clap_time, last_trigger_time, _sustained_chunk_count

    if time.time() - last_trigger_time < COOLDOWN_AFTER_TRIGGER:
        return

    rms = np.sqrt(np.mean(indata**2))

    if rms >= CLAP_THRESHOLD:
        _sustained_chunk_count += 1
        if _sustained_chunk_count > MAX_SUSTAINED_CHUNKS:
            return

        # Crest factor check: claps are spiky, voice is not
        peak = np.max(np.abs(indata))
        crest_factor = peak / (rms + 1e-9)
        if crest_factor < MIN_CREST_FACTOR:
            return

        # Frequency check: clap energy lives in 1.5-6kHz
        spectrum = np.fft.rfft(indata[:, 0])
        freqs = np.fft.rfftfreq(frames, d=1.0 / SAMPLE_RATE)
        energy = np.abs(spectrum) ** 2
        total_energy = np.sum(energy)
        if total_energy == 0:
            return
        band_mask = (freqs >= 1500) & (freqs <= 6000)
        freq_ratio = np.sum(energy[band_mask]) / total_energy
        if freq_ratio < FREQ_RATIO_THRESHOLD:
            return

        now = time.time()

        if first_clap_time is None:
            first_clap_time = now
            _sustained_chunk_count = 0  # reset so second clap can register
        else:
            gap = now - first_clap_time
            if gap < MIN_CLAP_GAP:
                return
            elif gap <= DOUBLE_CLAP_WINDOW:
                first_clap_time = None
                _sustained_chunk_count = 0
                on_trigger()
            else:
                first_clap_time = now
                _sustained_chunk_count = 0
    else:
        _sustained_chunk_count = 0
        if (
            first_clap_time is not None
            and (time.time() - first_clap_time) > DOUBLE_CLAP_WINDOW
        ):
            first_clap_time = None


def main():
    script_path = os.path.abspath(__file__)

    print(BANNER)

    print(f"  {BOLD}{CYAN}── Run Commands ──{RESET}\n")
    print(f"  {DIM}Start (background):{RESET}")
    print(f"  {YELLOW}nohup python3 {script_path} &> /tmp/misu.log &{RESET}\n")
    print(f"  {DIM}Stop:{RESET}")
    print(f"  {YELLOW}pkill -f misu.py{RESET}\n")

    print_config()

    if not run_dependency_checks():
        log("One or more critical dependencies missing. Exiting.", RED)
        sys.exit(1)

    print(f"  {GREEN}{BOLD}All checks passed.{RESET}")
    print(f"  {CYAN}Listening for double clap... (Ctrl+C to stop){RESET}")
    print(
        f"  {DIM}Controls: {YELLOW}p{RESET}{DIM} = play music, {YELLOW}s{RESET}{DIM} = pause/resume{RESET}\n"
    )

    # Pre-fetch audio URL for instant playback
    prefetch_audio_url()

    # Auto-launch everything on startup
    threading.Thread(target=stream_youtube_audio, daemon=True).start()
    threading.Thread(target=open_app, daemon=True).start()
    threading.Thread(target=show_image, daemon=True).start()

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=1,
            dtype="float32",
            callback=audio_callback,
        ):
            while True:
                cmd = input().strip().lower()
                if cmd == "p":
                    threading.Thread(target=stream_youtube_audio, daemon=True).start()
                elif cmd == "s":
                    toggle_pause()
    except KeyboardInterrupt:
        print(f"\n  {YELLOW}M.I.S.U. shutting down. Goodbye.{RESET}\n")
    except Exception as e:
        log(f"Fatal error: {e}", RED)
        sys.exit(1)


if __name__ == "__main__":
    # Ignore SIGHUP so nohup works cleanly
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    # Kill audio subprocess on exit
    atexit.register(cleanup)

    def handle_signal(sig, frame):
        cleanup()
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    main()
