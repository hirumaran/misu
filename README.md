# M.I.S.U. — Machine-Initiated Startup Unit

A **pure terminal** macOS background mic listener. Detects a double-clap and simultaneously opens an app + streams a YouTube audio clip. No GUI. No frontend. No web interface.

## ⚠️ Important

**M.I.S.U. does NOT auto-start on login.** There are no Launch Agents, no launchd plists, no login items. You run it manually when you want it, and kill it when you're done.

## Quick Start

```bash
# 1. Install dependencies
chmod +x setup.sh
./setup.sh

# 2. Run interactively (to test)
python3 misu.py

# 3. Run in background (intended workflow)
nohup python3 /Users/thirumarandeepak/Documents/misu/misu.py &> /tmp/misu.log &

# 4. Stop it
pkill -f misu.py
```

## Configuration

Edit the config block at the top of `misu.py`:

| Variable | Default | Description |
|---|---|---|
| `APP_TO_OPEN` | `"Codex"` | macOS app to launch on double clap |
| `YOUTUBE_URL` | `https://youtu.be/pAgnJDJN4VA...` | YouTube URL to stream audio from |
| `CLAP_THRESHOLD` | `0.25` | RMS amplitude threshold for clap detection |
| `DOUBLE_CLAP_WINDOW` | `0.7` | Max seconds between two claps |
| `COOLDOWN_AFTER_TRIGGER` | `3.0` | Seconds to ignore mic after activation |

## How It Works

1. Listens to the default mic input via `sounddevice`
2. Computes RMS per audio block
3. Two RMS spikes above threshold within 0.7s (with ≥80ms gap) = double clap
4. On detection, **simultaneously** launches the app (`open -a`) and streams YouTube audio (`yt-dlp` piped into `ffplay`)
5. 3-second cooldown prevents re-triggers from the audio playback itself

## Dependencies

- Python 3
- `sounddevice` + `numpy` (pip)
- `yt-dlp` + `ffmpeg` (brew)
- macOS mic access permission (System Settings → Privacy → Microphone)

## Audio Player Priority

ffplay (from ffmpeg) → mpv → afplay

## Troubleshooting

- **"Mic not found"**: Grant Terminal/iTerm mic access in System Settings → Privacy & Security → Microphone
- **Threshold too sensitive / not sensitive enough**: Adjust `CLAP_THRESHOLD`. Lower = more sensitive. Run interactively and watch for false triggers.
- **YouTube stream fails**: Run `yt-dlp -f bestaudio --get-url "YOUR_URL"` manually to verify the URL works.
