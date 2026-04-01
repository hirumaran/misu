# M.I.S.U.

Background mic listener. Double-clap = opens an app + streams a YouTube audio clip. No GUI, no frontend, just a terminal process.

**Doesn't auto-start.** You run it, you kill it.

---

## Setup

```bash
chmod +x setup.sh && ./setup.sh

# test it
python3 misu.py

# run in background
nohup python3 /Users/thirumarandeepak/Documents/misu/misu.py &> /tmp/misu.log &

# kill it
pkill -f misu.py
```

---

## Config

Edit the block at the top of `misu.py`:

| variable | default | description |
|---|---|---|
| `APP_TO_OPEN` | `"Codex"` | app to launch |
| `YOUTUBE_URL` | `https://youtu.be/pAgnJDJN4VA...` | audio to stream |
| `CLAP_THRESHOLD` | `0.25` | lower = more sensitive |
| `DOUBLE_CLAP_WINDOW` | `0.7` | max seconds between claps |
| `COOLDOWN_AFTER_TRIGGER` | `3.0` | stops audio from re-triggering it |

---

## How it works

Watches mic RMS. Two spikes above threshold within 0.7s = double clap. Fires `open -a` and `yt-dlp | ffplay` simultaneously, then goes quiet for 3 seconds.

**Needs:** `sounddevice`, `numpy`, `yt-dlp`, `ffmpeg`, and mic access for Terminal (System Settings > Privacy > Microphone).

---

## Troubleshooting

- **Mic not found:** grant Terminal mic access in System Settings
- **Wrong sensitivity:** adjust `CLAP_THRESHOLD`, run interactively to watch it
- **Stream fails:** test with `yt-dlp -f bestaudio --get-url "YOUR_URL"` directly
