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
import json
import curses
import termios
import tty
import select
import pygame
from PIL import Image
import numpy as np
import sounddevice as sd

# ═══════════════════════════════════════════════════════════════
# ██  C O N F I G U R A T I O N  ██
# ═══════════════════════════════════════════════════════════════
YOUTUBE_URL = "https://youtu.be/pAgnJDJN4VA?si=zAr_El1xtW_ugq8K"
CLAP_THRESHOLD = 0.10  # lower = more sensitive to claps
DOUBLE_CLAP_WINDOW = 1.0  # Max seconds between two claps
MIN_CLAP_GAP = 0.08  # Min seconds between claps (echo reject)
COOLDOWN_AFTER_TRIGGER = 2.0  # Seconds to ignore mic after trigger
FREQ_RATIO_THRESHOLD = 0.15  # less aggressive frequency filtering
MAX_SUSTAINED_CHUNKS = 8  # claps can span a few more blocks
MIN_CREST_FACTOR = 2.0  # peak/RMS ratio, lowered for easier detection
SAMPLE_RATE = 44100
BLOCK_SIZE = 512

CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "misu_config.json"
)
DEFAULT_APP = "Codex"
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
  ║     DOUBLE CLAP DETECTED — ACTIVATING   ║
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
APP_TO_OPEN = DEFAULT_APP
_trigger_event = threading.Event()
_audio_started_event = threading.Event()


def discover_apps():
    """Scan macOS app directories and return sorted list of installed apps."""
    search_dirs = [
        "/Applications",
        "/Applications/Utilities",
        "/System/Applications",
        "/System/Applications/Utilities",
        os.path.expanduser("~/Applications"),
    ]
    apps = set()
    for d in search_dirs:
        if os.path.isdir(d):
            for entry in os.listdir(d):
                if entry.endswith(".app"):
                    apps.add(entry[:-4])  # strip ".app" extension
    return sorted(apps, key=str.lower)


def load_config():
    """Load the saved app preference from config file."""
    global APP_TO_OPEN
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                saved_app = config.get("app_to_open", DEFAULT_APP)
                APP_TO_OPEN = saved_app
                log(f"Loaded app preference: {APP_TO_OPEN}", DIM)
        except Exception as e:
            log(f"Could not load config: {e}", YELLOW)
            APP_TO_OPEN = DEFAULT_APP
    else:
        APP_TO_OPEN = DEFAULT_APP


def save_config():
    """Save the current app preference to config file."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"app_to_open": APP_TO_OPEN}, f)
    except Exception as e:
        log(f"Could not save config: {e}", YELLOW)


def _run_selector(apps, start_idx):
    """Curses inner loop. Returns the selected app name, or None if cancelled."""
    result = [None]  # use list so inner function can write to it

    def _curses_main(stdscr):
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)  # highlighted row
        curses.init_pair(2, curses.COLOR_YELLOW, -1)  # label text
        curses.init_pair(3, curses.COLOR_GREEN, -1)  # selected marker

        current = start_idx
        scroll_offset = 0
        search = ""
        filtered = apps[:]

        def refresh_filter():
            nonlocal filtered, current, scroll_offset
            if search:
                filtered = [a for a in apps if search.lower() in a.lower()]
            else:
                filtered = apps[:]
            current = 0
            scroll_offset = 0

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            visible_rows = height - 5  # reserve rows for header + search bar

            # ── Header ──
            header = " SELECT APP  (↑↓ navigate · Enter select · Esc cancel · type to filter)"
            stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
            stdscr.addstr(0, 0, header[: width - 1])
            stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

            # ── Search bar ──
            stdscr.attron(curses.color_pair(2))
            stdscr.addstr(1, 0, f" Filter: {search}_"[: width - 1])
            stdscr.attroff(curses.color_pair(2))

            stdscr.addstr(2, 0, "─" * min(width - 1, 60))

            # ── App list ──
            if not filtered:
                stdscr.addstr(3, 2, "(no matches)")
            else:
                # Keep cursor in view
                if current < scroll_offset:
                    scroll_offset = current
                elif current >= scroll_offset + visible_rows:
                    scroll_offset = current - visible_rows + 1

                for i, app in enumerate(
                    filtered[scroll_offset : scroll_offset + visible_rows]
                ):
                    row = 3 + i
                    abs_idx = i + scroll_offset
                    prefix = " ▸ " if abs_idx == current else "   "

                    if abs_idx == current:
                        stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
                        stdscr.addstr(row, 0, f"{prefix}{app}"[: width - 1])
                        stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)
                    else:
                        stdscr.addstr(row, 0, f"{prefix}{app}"[: width - 1])

            stdscr.refresh()

            key = stdscr.getch()

            if key == curses.KEY_UP and filtered:
                current = (current - 1) % len(filtered)
            elif key == curses.KEY_DOWN and filtered:
                current = (current + 1) % len(filtered)
            elif key in (curses.KEY_ENTER, 10, 13):  # Enter
                if filtered:
                    result[0] = filtered[current]
                return
            elif key == 27:  # Escape
                return
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                search = search[:-1]
                refresh_filter()
            elif 32 <= key <= 126:  # printable character
                search += chr(key)
                refresh_filter()

    curses.wrapper(_curses_main)
    return result[0]


def show_app_menu():
    """Arrow-key dropdown to pick the default app."""
    global APP_TO_OPEN

    apps = discover_apps()
    if not apps:
        log("No apps found — keeping current default.", YELLOW)
        return

    # Start cursor on the currently saved app if it exists
    try:
        start_idx = apps.index(APP_TO_OPEN)
    except ValueError:
        start_idx = 0

    selected = _run_selector(apps, start_idx)

    if selected and selected != APP_TO_OPEN:
        APP_TO_OPEN = selected
        save_config()
        print(f"\n  {GREEN}✓ Default app set to: {YELLOW}{APP_TO_OPEN}{RESET}\n")
    else:
        print(f"\n  Keeping {YELLOW}{APP_TO_OPEN}{RESET} as default.\n")


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
        # Last resort: open -Ra which resolves without launching
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
                _audio_started_event.set()  # unblock waiters even on failure
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
            _audio_started_event.set()  # signal that audio has started
            _active_audio_proc.wait()
            _active_audio_proc = None
            try:
                os.remove(fifo_path)
            except OSError:
                pass

        elif audio_player_cmd == "mpv":
            _active_audio_proc = subprocess.Popen(
                ["mpv", "--no-video", "--really-quiet", direct_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _audio_started_event.set()  # signal that audio has started
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
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _audio_started_event.set()  # signal that audio has started
            _active_audio_proc.wait()
            _active_audio_proc = None

        log("Audio playback finished.", DIM)

    except Exception as e:
        log(f"Audio stream error: {e}", RED)
        _audio_started_event.set()  # unblock waiters even on error
        _active_audio_proc = None


def show_image_with_ctrl_p(close_event=None):
    """Show Jarvis image with drag support and A to change app.
    If close_event is provided, closes automatically when it's set.
    """
    img_path = "/Users/thirumarandeepak/Documents/misu/Jarvis.jpeg"
    try:
        import ctypes

        log("Opening Jarvis image...", CYAN)

        pil_img = Image.open(img_path).convert("RGB")
        orig_w, orig_h = pil_img.size
        new_w, new_h = int(orig_w * 1.5), int(orig_h * 1.5)
        pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)

        os.environ["SDL_VIDEO_WINDOW_POS"] = "40,40"

        img = pygame.image.fromstring(pil_img.tobytes(), (new_w, new_h), "RGB")
        screen = pygame.display.set_mode((new_w, new_h), pygame.NOFRAME)
        pygame.display.set_caption("M.I.S.U.")
        screen.blit(img, (0, 0))
        pygame.display.flip()

        # Force keyboard focus onto the pygame window
        pygame.event.set_grab(False)
        sdl_focused = False
        try:
            sdl_video = pygame.display.get_wm_info()
            # On macOS, clicking the window is needed; raise it to front
        except Exception:
            pass

        subprocess.Popen(
            [
                "osascript",
                "-e",
                'tell application "System Events" to set frontmost of every process '
                f"whose unix id is {os.getpid()} to true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Small delay to let OS process the frontmost request
        time.sleep(0.2)

        # Click the center of our window to ensure keyboard focus
        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'tell application "System Events"\n'
                    f'  set frontProcess to first process whose unix id is {os.getpid()}\n'
                    f'  set frontmost of frontProcess to true\n'
                    f'end tell',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

        libsdl2 = None
        window_ptr = None
        can_drag = False

        for lib_name in [
            "libSDL2-2.0.0.dylib",
            "libSDL2.dylib",
            "/usr/local/lib/libSDL2.dylib",
            "/opt/homebrew/lib/libSDL2.dylib",
        ]:
            try:
                libsdl2 = ctypes.CDLL(lib_name)
                break
            except OSError:
                continue

        if libsdl2:
            try:
                libsdl2.SDL_GetWindowPosition.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                ]
                libsdl2.SDL_SetWindowPosition.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_int,
                ]

                try:
                    libsdl2.SDL_GL_GetCurrentWindow.restype = ctypes.c_void_p
                    libsdl2.SDL_GL_GetCurrentWindow.argtypes = []
                    window_ptr = libsdl2.SDL_GL_GetCurrentWindow()
                    if window_ptr:
                        can_drag = True
                        log("SDL2 window via SDL_GL_GetCurrentWindow.", GREEN)
                except (AttributeError, OSError):
                    pass

                if not can_drag:
                    try:
                        libsdl2.SDL_GetWindowFromID.restype = ctypes.c_void_p
                        libsdl2.SDL_GetWindowFromID.argtypes = [ctypes.c_uint32]
                        window_ptr = libsdl2.SDL_GetWindowFromID(1)
                        if window_ptr:
                            can_drag = True
                            log("SDL2 window via SDL_GetWindowFromID(1).", GREEN)
                    except (AttributeError, OSError):
                        pass

                if not can_drag:
                    wm_info = pygame.display.get_wm_info()
                    raw_capsule = wm_info.get("window")
                    if raw_capsule is not None:
                        if isinstance(raw_capsule, int):
                            window_ptr = raw_capsule
                            can_drag = True
                            log("SDL2 window pointer (raw int).", GREEN)
                        else:
                            ctypes.pythonapi.PyCapsule_GetName.restype = ctypes.c_char_p
                            ctypes.pythonapi.PyCapsule_GetName.argtypes = [
                                ctypes.py_object
                            ]
                            try:
                                actual_name = ctypes.pythonapi.PyCapsule_GetName(
                                    raw_capsule
                                )
                                log(f"PyCapsule name = {actual_name!r}", DIM)
                            except Exception:
                                actual_name = None

                            ctypes.pythonapi.PyCapsule_GetPointer.restype = (
                                ctypes.c_void_p
                            )
                            ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [
                                ctypes.py_object,
                                ctypes.c_char_p,
                            ]
                            try:
                                window_ptr = ctypes.pythonapi.PyCapsule_GetPointer(
                                    raw_capsule, actual_name
                                )
                                if window_ptr:
                                    try:
                                        libsdl2.SDL_GetWindowFromID.restype = (
                                            ctypes.c_void_p
                                        )
                                        libsdl2.SDL_GetWindowFromID.argtypes = [
                                            ctypes.c_uint32
                                        ]
                                        sdl_win = libsdl2.SDL_GetWindowFromID(1)
                                        if sdl_win:
                                            window_ptr = sdl_win
                                    except Exception:
                                        pass
                                    can_drag = True
                                    log("SDL2 window via PyCapsule unwrap.", GREEN)
                            except (ValueError, OSError) as e:
                                log(f"PyCapsule unwrap failed: {e}", YELLOW)

                if not can_drag:
                    log("Could not get SDL2 window — dragging disabled.", YELLOW)

            except Exception as e:
                log(f"SDL2 drag setup failed: {e}", YELLOW)
                can_drag = False
        else:
            log("SDL2 library not found — dragging disabled.", YELLOW)

        if can_drag:
            log(
                "Drag: left-click + move | Close: right-click / Esc / Q | A: change app",
                DIM,
            )
        else:
            log("Close: right-click / Esc / Q | A: change app", DIM)
        log("(You can also type 'a' in the terminal to change app)", DIM)

        dragging = False
        start = time.time()
        running = True
        clock = pygame.time.Clock()

        if can_drag:
            wx_val, wy_val = ctypes.c_int(0), ctypes.c_int(0)
            libsdl2.SDL_GetWindowPosition(
                window_ptr, ctypes.byref(wx_val), ctypes.byref(wy_val)
            )
            win_x, win_y = wx_val.value, wy_val.value
        else:
            win_x, win_y = 40, 40

        while running:
            total_dx, total_dy = 0, 0

            # Auto-close if close_event is set
            if close_event and close_event.is_set():
                running = False

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_a:
                        pygame.quit()
                        print(f"\n  {CYAN}Changing default app...{RESET}")
                        show_app_menu()
                        print(f"\n  {GREEN}App changed to {YELLOW}{APP_TO_OPEN}{RESET}")
                        print(f"  {CYAN}Restarting image display...{RESET}\n")
                        pygame.init()
                        pil_img = Image.open(img_path).convert("RGB")
                        orig_w, orig_h = pil_img.size
                        new_w, new_h = int(orig_w * 1.5), int(orig_h * 1.5)
                        pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
                        os.environ["SDL_VIDEO_WINDOW_POS"] = "40,40"
                        img = pygame.image.fromstring(
                            pil_img.tobytes(), (new_w, new_h), "RGB"
                        )
                        screen = pygame.display.set_mode((new_w, new_h), pygame.NOFRAME)
                        pygame.display.set_caption("M.I.S.U.")
                        screen.blit(img, (0, 0))
                        pygame.display.flip()
                        if libsdl2:
                            try:
                                libsdl2.SDL_GL_GetCurrentWindow.restype = (
                                    ctypes.c_void_p
                                )
                                window_ptr = libsdl2.SDL_GL_GetCurrentWindow()
                                if window_ptr:
                                    can_drag = True
                            except:
                                pass
                        start = time.time()
                        continue
                    elif event.key in (pygame.K_ESCAPE, pygame.K_q, pygame.K_RETURN):
                        running = False
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1:
                        dragging = True
                    elif event.button == 3:
                        running = False
                elif event.type == pygame.MOUSEBUTTONUP:
                    if event.button == 1:
                        dragging = False
                elif event.type == pygame.MOUSEMOTION and dragging and can_drag:
                    total_dx += event.rel[0]
                    total_dy += event.rel[1]

            if total_dx != 0 or total_dy != 0:
                win_x += total_dx
                win_y += total_dy
                libsdl2.SDL_SetWindowPosition(window_ptr, win_x, win_y)

            if time.time() - start > 1.5:
                running = False

            if dragging:
                clock.tick(120)
            else:
                clock.tick(60)

        pygame.quit()
        log("Image closed.", DIM)

    except Exception as e:
        log(f"Image error: {e}", RED)


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


def clear_cache():
    """Clear cached audio URL and re-fetch it."""
    global _cached_audio_url
    _cached_audio_url = None
    log("Cache cleared.", YELLOW)
    log("Re-fetching audio URL...", DIM)
    prefetch_audio_url()


def show_gif(gif_path):
    """Display a GIF using pygame with animation."""
    if not os.path.isfile(gif_path):
        log(f"GIF not found: {gif_path}", YELLOW)
        return
    try:
        log("Loading GIF...", CYAN)
        pygame.init()

        frames = []
        durations = []
        import PIL.ImageSequence

        gif = Image.open(gif_path)
        for frame in PIL.ImageSequence.Iterator(gif):
            frame = frame.convert("RGBA")
            pil_frame = frame.resize(
                (int(frame.width * 0.76), int(frame.height * 0.76)), Image.LANCZOS
            )
            pygame_frame = pygame.image.fromstring(
                pil_frame.tobytes(), pil_frame.size, "RGBA"
            )
            frames.append(pygame_frame)
            durations.append(frame.info.get("duration", 100) / 1000.0)

        if not frames:
            log("No frames in GIF.", YELLOW)
            return

        os.environ["SDL_VIDEO_WINDOW_POS"] = "40,40"
        w, h = frames[0].get_size()
        screen = pygame.display.set_mode((w, h), pygame.NOFRAME)
        pygame.display.set_caption("M.I.S.U.")

        subprocess.Popen(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to set frontmost of every process '
                f"whose unix id is {os.getpid()} to true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        log(f"GIF loaded: {len(frames)} frames.", GREEN)

        running = True
        clock = pygame.time.Clock()
        frame_idx = 0
        frame_timer = 0.0
        start = time.time()

        while running:
            dt = clock.tick(60) / 1000.0
            frame_timer += dt

            if frame_timer >= durations[frame_idx]:
                frame_timer = 0
                frame_idx = (frame_idx + 1) % len(frames)

            screen.fill((0, 0, 0))
            screen.blit(frames[frame_idx], (0, 0))
            pygame.display.flip()

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q, pygame.K_RETURN):
                        running = False
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 3:
                        running = False

            if time.time() - start > 1.5:
                running = False

        pygame.quit()
        log("GIF closed.", DIM)

    except Exception as e:
        log(f"GIF error: {e}", RED)


def toggle_pause():
    """Pause or resume currently playing audio via SIGSTOP/SIGCONT."""
    global _audio_paused
    if not _active_audio_proc or _active_audio_proc.poll() is not None:
        log("No audio playing.", DIM)
        return

    try:
        if _audio_paused:
            os.kill(_active_audio_proc.pid, signal.SIGCONT)
            _audio_paused = False
            log("Audio resumed.", GREEN)
        else:
            os.kill(_active_audio_proc.pid, signal.SIGSTOP)
            _audio_paused = True
            log("Audio paused.", YELLOW)
    except OSError as e:
        log(f"Pause toggle error: {e}", RED)


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
    """Open the configured app immediately and bring it to front."""
    try:
        log(f"Launching {APP_TO_OPEN}...", CYAN)

        # Step 1: Find the actual .app path on disk (handles name mismatches)
        app_path = None
        search_dirs = [
            "/Applications",
            "/Applications/Utilities",
            "/System/Applications",
            os.path.expanduser("~/Applications"),
        ]
        for d in search_dirs:
            candidate = os.path.join(d, f"{APP_TO_OPEN}.app")
            if os.path.isdir(candidate):
                app_path = candidate
                break

        # Also search for partial matches (e.g., "ChatGPT" might be in a subfolder)
        if not app_path:
            try:
                result = subprocess.run(
                    ["mdfind", f"kMDItemKind == 'Application' && kMDItemDisplayName == '{APP_TO_OPEN}'"],
                    capture_output=True, text=True, timeout=5,
                )
                paths = result.stdout.strip().split("\n")
                if paths and paths[0]:
                    app_path = paths[0]
            except Exception:
                pass

        # Step 2: Launch using the resolved path, or fall back to name
        if app_path:
            log(f"Resolved app path: {app_path}", DIM)
            subprocess.run(["open", app_path], check=False, capture_output=True, timeout=10)
        else:
            subprocess.run(["open", "-a", APP_TO_OPEN], check=False, capture_output=True, timeout=10)

        # Step 3: Wait for it to actually start
        time.sleep(2.0)

        # Step 4: Try multiple ways to bring it to front
        # 4a: AppleScript activate
        subprocess.run(
            ["osascript", "-e", f'tell application "{APP_TO_OPEN}" to activate'],
            check=False, capture_output=True, timeout=10,
        )
        time.sleep(0.3)

        # 4b: System Events — match by name (handles apps whose process name differs)
        subprocess.run(
            [
                "osascript", "-e",
                (
                    'tell application "System Events"\n'
                    '  repeat with p in (every process whose background only is false)\n'
                    f'    if name of p contains "{APP_TO_OPEN}" then\n'
                    '      set frontmost of p to true\n'
                    '      exit repeat\n'
                    '    end if\n'
                    '  end repeat\n'
                    'end tell'
                ),
            ],
            check=False, capture_output=True, timeout=10,
        )

        # 4c: If it's still not in front, try open again (brings existing window forward)
        if app_path:
            subprocess.run(["open", app_path], check=False, capture_output=True, timeout=10)
        else:
            subprocess.run(["open", "-a", APP_TO_OPEN], check=False, capture_output=True, timeout=10)

        log(f"{APP_TO_OPEN} launched.", GREEN)
    except Exception as e:
        log(f"Failed to open {APP_TO_OPEN}: {e}", RED)


def on_trigger():
    """Fired on double-clap detection. Signals the main thread to handle activation."""
    global last_trigger_time
    now = time.time()

    if not trigger_lock.acquire(blocking=False):
        return
    try:
        if now - last_trigger_time < COOLDOWN_AFTER_TRIGGER:
            return
        last_trigger_time = now

        print(ACTIVATION_MSG, flush=True)
        _trigger_event.set()

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

    # Load saved config first
    load_config()

    print_config()

    if not run_dependency_checks():
        log("One or more critical dependencies missing. Exiting.", RED)
        sys.exit(1)

    print(f"  {GREEN}{BOLD}All checks passed.{RESET}")
    print(f"  {CYAN}Listening for double clap... (Ctrl+C to stop){RESET}")
    print(
        f"  {DIM}Controls: {YELLOW}s{RESET}{DIM} = pause/resume  |  {YELLOW}a{RESET}{DIM} = change app  |  {YELLOW}c{RESET}{DIM} = clear cache{RESET}\n"
    )

    # Pre-fetch audio URL for instant playback
    prefetch_audio_url()

    # Run sounddevice + terminal input in a background thread
    def listener_loop():
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
                    if cmd == "s":
                        toggle_pause()
                    elif cmd == "a":
                        show_app_menu()
                    elif cmd == "c":
                        clear_cache()
        except Exception as e:
            log(f"Listener error: {e}", RED)

    threading.Thread(target=listener_loop, daemon=True).start()

    # Main thread waits for triggers, then handles everything
    try:
        while True:
            _trigger_event.wait()
            _trigger_event.clear()

            home_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "home.mp3"
            )

            # Event to signal when home.mp3 finishes
            home_done = threading.Event()

            def play_home():
                if os.path.isfile(home_path):
                    log("Playing home.mp3...", CYAN)
                    subprocess.call(["afplay", home_path])
                home_done.set()

            # Start home.mp3 in background
            t_home = threading.Thread(target=play_home, daemon=True)
            t_home.start()

            # Show image simultaneously with home.mp3 (auto-closes when home.mp3 ends)
            pygame.init()
            show_image_with_ctrl_p(close_event=home_done)

            # home.mp3 done → start music
            _audio_started_event.clear()
            t_audio = threading.Thread(target=stream_youtube_audio, daemon=True)
            t_audio.start()

            # Wait until the audio player process has actually spawned
            _audio_started_event.wait(timeout=30)

            # Wait 3.5 seconds so the song is audibly playing
            time.sleep(3.5)

            # Now open the app
            t_app = threading.Thread(target=open_app, daemon=True)
            t_app.start()

            # Wait for app to finish launching
            t_app.join(timeout=15)

            # Short delay before GIF appears
            time.sleep(0.75)

            # After app launched, show freaky.gif
            show_gif("/Users/thirumarandeepak/Documents/misu/freaky.gif")

            log("Trigger sequence complete.", GREEN)

    except KeyboardInterrupt:
        print(f"\n  {YELLOW}M.I.S.U. shutting down. Goodbye.{RESET}\n")


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
