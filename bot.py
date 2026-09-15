#!/usr/bin/env python3
"""
VidCore Discord Downloader — Raspberry Pi edition
- In-channel dropdown/button prompts (restricted to the requester)
- One editable status message per episode; live progress via !status
- Auto-upscale to 4K; deletes the non-4K source afterwards
- Cancel deletes partial downloads completely
- DMs the user ONLY when the job finishes
- Auto-resumes interrupted downloads ONCE per process
- Manual override: !dlurl <http(s)_url> [title]  (m3u8, YouTube, direct links)
- Subtitles: fetched from OpenSubtitles after download (never via yt-dlp)
- MP4 output (seekable) for maximum player compatibility
"""

import subprocess
import time
import os
import sys
import re
import io
import json
import pickle
import asyncio
import itertools
import shutil
import requests
import threading
import queue
import pyperclip
from collections import deque
from datetime import datetime

import discord
from discord.ext import commands

# =============================================================
# CONFIG — Raspberry Pi
# =============================================================
DISCORD_TOKEN   = ""          # loaded from CREDENTIALS_FILE at startup
ALLOWED_USER_ID = 0

PYTHON_PATH      = sys.executable
MOVIES_FOLDER    = "/mnt/minecraft-backups/videos"
CHROME_PATH      = "/usr/bin/chromium"
CHROME_USER_DATA = "/home/grap/.config/chromium"

PROGRESS_FILE      = "download_progress.pkl"
STATE_FILE         = "bot_state.json"
MOVIE_STATE_FILE   = "movie_downloads.pkl"   # tracks in-flight movie downloads
TEMP_FOLDER        = "tmp"                     # yt-dlp temp dir for fragments

YTDLP_MODULE = "yt_dlp"

# OpenSubtitles API (free account at opensubtitles.com) — loaded from CREDENTIALS_FILE
OS_API_KEY   = ""
OS_USERNAME  = ""
OS_PASSWORD  = ""
OS_API_BASE  = "https://api.opensubtitles.com/api/v1"

# Secrets live in a separate text file (not hard-coded in this script).
# Format, one per line:  KEY = value   (# lines and blanks are ignored)
CREDENTIALS_FILE = "credentials.txt"

# Tunables
CONCURRENT_FRAGMENTS = 5
STATUS_EDIT_INTERVAL = 8.0
SUBTITLE_LANGS = "en.*,nl.*"   # subtitle languages to fetch (en = English, nl = Dutch)

# =============================================================
# Setup
# =============================================================
def load_credentials(path=CREDENTIALS_FILE):
    """
    Read secrets from a KEY = value text file and update the module-level
    DISCORD_TOKEN / OS_* variables. Returns True if a Discord token was found.
    """
    global DISCORD_TOKEN, OS_API_KEY, OS_USERNAME, OS_PASSWORD
    found = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                found[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as e:
        print(f"[credentials] could not read {path}: {e}")
        return False

    DISCORD_TOKEN  = found.get("DISCORD_TOKEN", DISCORD_TOKEN)
    OS_API_KEY     = found.get("OS_API_KEY", OS_API_KEY)
    OS_USERNAME    = found.get("OS_USERNAME", OS_USERNAME)
    OS_PASSWORD    = found.get("OS_PASSWORD", OS_PASSWORD)
    return bool(DISCORD_TOKEN)


os.makedirs(MOVIES_FOLDER, exist_ok=True)
os.makedirs(os.path.join(MOVIES_FOLDER, TEMP_FOLDER), exist_ok=True)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)


def kill_chrome():
    try:
        subprocess.run(["pkill", "-f", "chromium"], capture_output=True)
    except Exception:
        pass


def safe_delete(path):
    """Delete a file or folder if it exists, ignoring errors."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        print(f"[safe_delete] {path}: {e}")


def cleanup_download_artifacts(output_prefix):
    """
    Remove every file matching a download prefix (partials, parts,
    yt-dlp metadata, and the temp fragment directory), recursively so
    series files inside show/season subfolders are covered too.
    """
    try:
        touched_dirs = set()
        for root, dirs, files in os.walk(MOVIES_FOLDER):
            # Skip the yt-dlp temp fragment dir; handled below
            dirs[:] = [d for d in dirs if d != TEMP_FOLDER]
            removed = False
            for f in files:
                if f.startswith(output_prefix):
                    safe_delete(os.path.join(root, f))
                    removed = True
            if removed:
                touched_dirs.add(root)
        # Also nuke any yt-dlp fragment dir for this download
        for entry in os.listdir(os.path.join(MOVIES_FOLDER, TEMP_FOLDER)):
            if entry.startswith(output_prefix):
                safe_delete(os.path.join(MOVIES_FOLDER, TEMP_FOLDER, entry))
        # Prune show/season folders that ended up empty
        for d in sorted(touched_dirs, key=lambda x: x.count(os.sep), reverse=True):
            cur = d
            while cur != MOVIES_FOLDER and os.path.isdir(cur):
                try:
                    if os.listdir(cur):
                        break
                    os.rmdir(cur)
                    cur = os.path.dirname(cur)
                except Exception:
                    break
    except Exception as e:
        print(f"[cleanup] {e}")


# =============================================================
# Global live status
# =============================================================
LIVE_STATUS = {}
LIVE_STATUS_LOCK = threading.Lock()


def set_live_status(job_id, text):
    with LIVE_STATUS_LOCK:
        LIVE_STATUS[job_id] = {"text": text, "ts": time.time()}


def clear_live_status(job_id):
    with LIVE_STATUS_LOCK:
        LIVE_STATUS.pop(job_id, None)


def get_live_status(job_id):
    with LIVE_STATUS_LOCK:
        entry = LIVE_STATUS.get(job_id)
        return entry["text"] if entry else None


# =============================================================
# Background upscale worker
# =============================================================
# Downloads push files onto UPSCALE_QUEUE and continue immediately, so the
# next download can run while ffmpeg is busy encoding a previous file.
UPSCALE_QUEUE = queue.Queue()
UPSCALE_ACTIVITY = {}
UPSCALE_ACTIVITY_LOCK = threading.Lock()
upscale_id_counter = itertools.count(1)
_upscale_thread = None


def register_upscale_activity(up_id, job_id, label, text):
    with UPSCALE_ACTIVITY_LOCK:
        UPSCALE_ACTIVITY[up_id] = {
            "job_id": job_id, "label": label, "text": text, "ts": time.time()
        }


def set_upscale_activity(up_id, text):
    with UPSCALE_ACTIVITY_LOCK:
        entry = UPSCALE_ACTIVITY.get(up_id)
        if entry:
            entry["text"] = text
            entry["ts"] = time.time()


def clear_upscale_activity(up_id):
    with UPSCALE_ACTIVITY_LOCK:
        UPSCALE_ACTIVITY.pop(up_id, None)


def get_upscale_activities():
    with UPSCALE_ACTIVITY_LOCK:
        return [dict(e) for e in UPSCALE_ACTIVITY.values()]


def ensure_upscale_worker():
    global _upscale_thread
    if _upscale_thread is None or not _upscale_thread.is_alive():
        _upscale_thread = threading.Thread(target=_run_upscale_worker, daemon=True)
        _upscale_thread.start()
        print("[upscale] background upscale worker started")


def _upscale_post(loop, channel, text):
    async def _send():
        try:
            await channel.send(text)
        except Exception as e:
            print(f"[upscale post] {e}")
    try:
        asyncio.run_coroutine_threadsafe(_send(), loop).result()
    except Exception as e:
        print(f"[upscale post] {e}")


def _run_upscale_worker():
    while True:
        item = UPSCALE_QUEUE.get()
        if item is None:
            break
        up_id = item["up_id"]
        try:
            job_id = item["job_id"]
            label = item["label"]
            input_file = item["input"]
            cancel_event = item.get("cancel_event")
            loop = item.get("loop")
            channel = item.get("channel")
            set_status = item.get("set_status")

            def post(text):
                if loop is not None and channel is not None:
                    _upscale_post(loop, channel, text)

            if cancel_event and cancel_event.is_set():
                clear_upscale_activity(up_id)
                continue

            if set_status:
                set_status(f"🔼 Upscaling {os.path.basename(input_file)}…", force=True)
            register_upscale_activity(
                up_id, job_id, label,
                f"⏳ queued — {os.path.basename(input_file)}"
            )

            def status_cb(m):
                set_upscale_activity(up_id, m)
                if set_status:
                    set_status(m, force=True)

            result = upscale_to_4k(input_file,
                                   status_cb=status_cb,
                                   delete_source=True,
                                   cancel_event=cancel_event)

            if cancel_event and cancel_event.is_set():
                post(f"🛑 Upscale cancelled: {os.path.basename(input_file)}")
            elif result:
                post(f"✅ Upgraded to 4K: {os.path.basename(result)}")
            else:
                post(f"❌ Upscale failed: {os.path.basename(input_file)} "
                     f"— source kept, will retry later")
            clear_upscale_activity(up_id)
        except Exception as e:
            print(f"[upscale worker] {e}")
            import traceback; traceback.print_exc()
            try:
                clear_upscale_activity(up_id)
            except Exception:
                pass


def enqueue_upscale(input_file, job_id, label, cancel_event=None,
                    loop=None, channel=None, set_status=None):
    up_id = next(upscale_id_counter)
    UPSCALE_QUEUE.put({
        "up_id": up_id,
        "job_id": job_id,
        "label": label,
        "input": input_file,
        "cancel_event": cancel_event,
        "loop": loop,
        "channel": channel,
        "set_status": set_status,
    })
    ensure_upscale_worker()
    return up_id


# =============================================================
# Movie downloads state
# =============================================================
class MovieDownloadsState:
    """
    Persisted record of in-flight movie downloads so we can resume
    after a reboot / restart. Keyed by output prefix.
    """
    def __init__(self):
        # {prefix: {"url": ..., "title": ..., "quality": ..., "additional": ...}}
        self.entries = {}

    @staticmethod
    def _path():
        return os.path.join(MOVIES_FOLDER, MOVIE_STATE_FILE)

    @staticmethod
    def load():
        s = MovieDownloadsState()
        try:
            with open(MovieDownloadsState._path(), 'rb') as f:
                s.entries = pickle.load(f)
        except Exception:
            s.entries = {}
        return s

    def save(self):
        try:
            with open(self._path(), 'wb') as f:
                pickle.dump(self.entries, f)
        except Exception as e:
            print(f"[movie state save] {e}")

    def add(self, prefix, url, title, quality, additional):
        self.entries[prefix] = {
            "url": url, "title": title,
            "quality": quality, "additional": additional,
        }
        self.save()

    def remove(self, prefix):
        self.entries.pop(prefix, None)
        self.save()

    def get(self, prefix):
        return self.entries.get(prefix)


# =============================================================
# Progress
# =============================================================
class DownloadProgress:
    def __init__(self):
        self.title = ""
        self.imdb_id = ""
        self.kind = "series"
        self.total_episodes = 0
        self.downloaded_episodes = 0
        self.completed = False
        self.failed_episodes = []
        self.completed_episodes = []
        self.season_episode_counts = {}
        self.selected_episodes = []
        self.start_time = None
        self.download_folder = MOVIES_FOLDER
        self.owner_id = 0
        self.channel_id = 0
        self.quality_choice = "1"
        self.additional_choice = "3"
        self.subtitles = False
        self.upscale = False

    def save(self):
        try:
            with open(os.path.join(self.download_folder, PROGRESS_FILE), 'wb') as f:
                pickle.dump(self, f)
        except Exception as e:
            print(f"[progress save] {e}")

    @staticmethod
    def load():
        try:
            with open(os.path.join(MOVIES_FOLDER, PROGRESS_FILE), 'rb') as f:
                obj = pickle.load(f)
        except Exception:
            return None

        defaults = {
            "title": "", "imdb_id": "", "kind": "series",
            "total_episodes": 0, "downloaded_episodes": 0, "completed": False,
            "failed_episodes": [], "completed_episodes": [],
            "season_episode_counts": {}, "selected_episodes": [],
            "start_time": None, "download_folder": MOVIES_FOLDER,
            "owner_id": 0, "channel_id": 0,
            "quality_choice": "1", "additional_choice": "3",
            "subtitles": False, "upscale": False,
        }
        for name, default in defaults.items():
            if not hasattr(obj, name):
                setattr(obj, name, default)
        return obj

    def percent(self):
        if self.total_episodes == 0:
            return 0
        return (self.downloaded_episodes / self.total_episodes) * 100


def save_state(**kwargs):
    try:
        state = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
            if not isinstance(state, dict):
                state = {}
        state.update(kwargs)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[save_state] {e}")


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# =============================================================
# IMDb helpers
# =============================================================
def classify(q_value: str, qid: str = "") -> str:
    qid = (qid or "").strip()
    if qid:
        low = qid.lower()
        if "series" in low:
            return "series"
        if low in ("movie", "tv movie", "video", "short", "tv special", "tvshort"):
            return "movie"

    q = (q_value or "").strip().lower()
    if "series" in q:
        return "series"
    if q in ("tv movie", "movie", "video", "short"):
        return "movie"
    if "tv" in q:
        return "movie"
    return "movie"


def search_imdb(query, limit=5):
    if not query:
        return []
    c = query[0].lower() if query[0].isalnum() else "a"
    url = f"https://v2.sg.media-imdb.com/suggestion/{c}/{query}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[imdb search] {e}")
        return []
    return [x for x in data.get('d', []) if x.get('id', '').startswith('tt')][:limit]


def get_series_info(imdb_id):
    """Episode counts per season from TVMaze (key-free JSON API)."""
    seasons = {}
    try:
        r = requests.get(f"https://api.tvmaze.com/lookup/shows?imdb={imdb_id}",
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        show_id = r.json()["id"]
    except Exception as e:
        print(f"[series info] lookup failed: {e}")
        return {}

    try:
        sr = requests.get(f"https://api.tvmaze.com/shows/{show_id}/seasons",
                          headers=HEADERS, timeout=15)
        sr.raise_for_status()
        for season in sr.json():
            number = season.get("number")
            order = season.get("episodeOrder")
            if number is None:
                continue
            if order:
                seasons[number] = order
            else:
                seasons[number] = 0   # resolved via /episodes below
    except Exception as e:
        print(f"[series info] seasons failed: {e}")
        return {}

    unresolved = [n for n, c in seasons.items() if c == 0]
    if unresolved:
        try:
            er = requests.get(f"https://api.tvmaze.com/shows/{show_id}/episodes",
                              headers=HEADERS, timeout=20)
            er.raise_for_status()
            max_ep = {}
            for ep in er.json():
                sn = ep.get("season")
                num = ep.get("number")
                if sn is not None and num is not None:
                    max_ep[sn] = max(max_ep.get(sn, 0), num)
            for n in unresolved:
                if max_ep.get(n):
                    seasons[n] = max_ep[n]
                else:
                    seasons.pop(n, None)
        except Exception as e:
            print(f"[series info] episodes fallback failed: {e}")

    seasons = {n: c for n, c in sorted(seasons.items()) if c}
    return seasons


# =============================================================
# Download helpers
# =============================================================
def construct_vidcore_url(imdb_id, is_series=False, season=1, episode=1):
    if is_series:
        return f"https://vidcore.io/tv/{imdb_id}/{season}/{episode}"
    return f"https://vidcore.io/movie/{imdb_id}"


def check_internet():
    try:
        requests.get("https://www.google.com", timeout=5)
        return True
    except Exception:
        return False


def is_4k_source(path):
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=height', '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=30)
        if probe.returncode == 0 and probe.stdout.strip().isdigit():
            return int(probe.stdout.strip()) >= 2160
    except Exception:
        pass
    return False


def upscale_to_4k(input_file, status_cb=None, delete_source=True,
                  cancel_event=None):
    if not os.path.exists(input_file):
        if status_cb: status_cb(f"❌ File not found: {input_file}")
        return None

    if is_4k_source(input_file):
        if status_cb: status_cb(f"⏭️ Already 4K: {os.path.basename(input_file)}")
        return input_file

    base, ext = os.path.splitext(input_file)
    output_file = f"{base}_4k{ext}"

    if status_cb: status_cb(f"🔼 Upscaling {os.path.basename(input_file)} to 4K…")

    cmd = [
        'ffmpeg',
        '-err_detect', 'explode', '-xerror',
        '-i', input_file,
        '-vf', 'scale=3840:2160:flags=lanczos',
        '-c:v', 'libx264',
        '-crf', '18', '-preset', 'faster',
        '-c:a', 'copy',
        '-movflags', '+faststart',
        '-y', output_file
    ]

    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, bufsize=1)
    except FileNotFoundError:
        if status_cb: status_cb("❌ FFmpeg not found.")
        return None

    q = queue.Queue()
    def reader():
        for line in iter(process.stderr.readline, ''):
            if line: q.put(line.rstrip())
        process.stderr.close()
    threading.Thread(target=reader, daemon=True).start()

    last = -1
    cancelled = False
    while process.poll() is None or not q.empty():
        if cancel_event and cancel_event.is_set():
            cancelled = True
            try:
                process.terminate()
            except Exception:
                pass
            break
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        m = re.search(r'time=(\d+):(\d+):(\d+)', line)
        if m:
            h, mn, s = map(int, m.groups())
            secs = h * 3600 + mn * 60 + s
            if secs - last >= 30:
                last = secs
                if status_cb:
                    status_cb(f"🔼 Upscaling… {h:02d}:{mn:02d}:{s:02d} encoded")

    if cancelled:
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()
        if status_cb: status_cb("🛑 Upscale cancelled.")
        safe_delete(output_file)
        return None

    if process.returncode != 0:
        if status_cb: status_cb("❌ Upscaling failed.")
        safe_delete(output_file)
        return None

    if not os.path.exists(output_file) or os.path.getsize(output_file) < 1024 * 1024:
        if status_cb: status_cb("❌ Upscaled file missing or too small.")
        safe_delete(output_file)
        return None

    if status_cb: status_cb(f"✅ Upscaled: {os.path.basename(output_file)}")

    if delete_source:
        try:
            os.remove(input_file)
            if status_cb:
                status_cb(f"🗑️ Deleted original: {os.path.basename(input_file)}")
        except Exception as e:
            if status_cb:
                status_cb(f"⚠️ Could not delete original: {e}")

    return output_file


def find_downloaded_file(base_name):
    for root, dirs, files in os.walk(MOVIES_FOLDER):
        dirs[:] = [d for d in dirs if d != TEMP_FOLDER]
        for f in files:
            if f.startswith(base_name) and not f.endswith(".part") \
                    and "_4k" not in f:
                return os.path.join(root, f)
    return None


def find_subtitle_file(base_name):
    for root, dirs, files in os.walk(MOVIES_FOLDER):
        dirs[:] = [d for d in dirs if d != TEMP_FOLDER]
        for f in files:
            if f.startswith(base_name) and \
                    f.lower().endswith((".srt", ".vtt", ".ass", ".ssa")):
                return os.path.join(root, f)
    return None


def verify_and_faststart(input_file, status_cb=None, cancel_event=None):
    """
    Make a downloaded MP4 seekable/skippable by writing the moov atom to the
    front (-movflags +faststart) and fail on any container/bitstream error
    (-err_detect explode -xerror) so a corrupted/glitchy download is caught.
    Stream copy only — no re-encode, so it's fast and CPU-cheap.
    """
    if not os.path.exists(input_file):
        if status_cb: status_cb("❌ File not found for faststart pass.")
        return None

    base, ext = os.path.splitext(input_file)
    if ext.lower() not in (".mp4", ".mov", ".m4a"):
        # Only MP4-family containers benefit from +faststart; leave others
        # untouched so e.g. audio-only webm downloads are never failed here.
        return input_file

    tmp_file = f"{base}.faststart{ext}"

    if status_cb:
        status_cb(f"🔧 Making seekable / checking glitches: {os.path.basename(input_file)}")

    cmd = [
        'ffmpeg', '-v', 'error',
        '-err_detect', 'explode', '-xerror',
        '-i', input_file,
        '-map', '0',
        '-c', 'copy',
        '-movflags', '+faststart',
        '-y', tmp_file
    ]

    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError:
        if status_cb: status_cb("❌ FFmpeg not found.")
        return None

    q = queue.Queue()

    def reader():
        for line in iter(process.stderr.readline, ''):
            if line: q.put(line.rstrip())
        process.stderr.close()
    threading.Thread(target=reader, daemon=True).start()

    cancelled = False
    while process.poll() is None or not q.empty():
        if cancel_event and cancel_event.is_set():
            cancelled = True
            try:
                process.terminate()
            except Exception:
                pass
            break
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if "error" in line.lower():
            if status_cb: status_cb(f"⚠️ {line[:180]}")

    if cancelled:
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()
        if status_cb: status_cb("🛑 faststart pass cancelled.")
        safe_delete(tmp_file)
        return None

    if process.returncode != 0:
        if status_cb: status_cb("❌ faststart / glitch check failed.")
        safe_delete(tmp_file)
        return None

    if not os.path.exists(tmp_file):
        if status_cb: status_cb("❌ faststart output missing.")
        return None

    try:
        os.replace(tmp_file, input_file)
    except Exception as e:
        if status_cb: status_cb(f"⚠️ could not replace file: {e}")
        safe_delete(tmp_file)
        return None

    if status_cb: status_cb("✅ MP4 seekable (faststart), no glitches found.")
    return input_file


# =============================================================
# yt-dlp runner
# =============================================================
def _ytdlp_run(m3u8_url, title, episode_info, progress,
               status_msg, set_status, notify, loop, channel,
               job_id, cancel_event,
               max_retries, retry, retry_fn):

    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    quality_map = {
        "1": "bestvideo+bestaudio/best",
        "2": "bestvideo[height>=2160]+bestaudio/best",
        "3": "bestvideo[height>=1080]+bestaudio/best",
        "4": "bestvideo[height>=720]+bestaudio/best",
        "5": "bestvideo[height>=480]+bestaudio/best",
        "6": "bestaudio/best",
    }
    fmt = quality_map.get(progress.quality_choice, "bestvideo+bestaudio/best")

    # Build output base name (no extension — yt-dlp fills that in)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if title:
        safe = re.sub(r'[^a-zA-Z0-9\- ]', '', title).replace(' ', '_')
        if episode_info:
            base_name = (f"{safe}_S{episode_info['season']:02d}"
                         f"E{episode_info['episode']:02d}_Q{progress.quality_choice}")
        else:
            base_name = f"{safe}_{ts}_Q{progress.quality_choice}"
    else:
        base_name = f"video_{ts}"

    # Series episodes land in a per-show / per-season subfolder;
    # movies and manual downloads stay flat in the root.
    if episode_info:
        out_dir = os.path.join(
            MOVIES_FOLDER, safe, f"Season_{episode_info['season']}")
        os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = MOVIES_FOLDER
    out_path = os.path.join(out_dir, base_name + ".%(ext)s")
    temp_dir = os.path.join(MOVIES_FOLDER, TEMP_FOLDER)

    yt_cmd = [
        PYTHON_PATH, "-m", YTDLP_MODULE,
        "--referer", "https://vidcore.io/",
        "--concurrent-fragments", str(CONCURRENT_FRAGMENTS),
        "--fragment-retries", "10",
        "--retry-sleep", "2",
        "--http-chunk-size", "10M",
        "--continue",                              # resume from .part if present
        "--paths", f"temp:{temp_dir}",             # fragments go here
        m3u8_url,
    ]

    if progress.additional_choice == "1":
        yt_cmd += ["-f", fmt.replace("+bestaudio", "")]
    elif progress.additional_choice == "2":
        yt_cmd += ["-f", "bestaudio/best"]
    else:
        yt_cmd += ["-f", fmt]

    if progress.additional_choice != "2":
        # Force MP4 container — seekable and universally playable
        yt_cmd += ["--merge-output-format", "mp4"]
        yt_cmd += ["--remux-video", "mp4"]

    yt_cmd += ["-o", out_path, "--progress", "--newline"]

    try:
        process = subprocess.Popen(yt_cmd,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, bufsize=1)
    except Exception as e:
        set_status(f"❌ yt-dlp failed to start: {e}", force=True)
        return False

    q = queue.Queue()
    def reader(stream):
        for line in iter(stream.readline, ''):
            if line: q.put(line.rstrip())
        stream.close()
    threading.Thread(target=reader, args=(process.stdout,), daemon=True).start()
    threading.Thread(target=reader, args=(process.stderr,), daemon=True).start()

    while process.poll() is None or not q.empty():
        if cancelled():
            try:
                process.terminate()
            except Exception:
                pass
            break
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if "[download]" in line or "ETA" in line:
            set_status(line.strip()[:150])
            set_live_status(job_id, line.strip()[:150])
        elif "ERROR" in line:
            set_status(f"⚠️ {line.strip()[:180]}", force=True)

    # -- Cancelled: delete everything --
    if cancelled():
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()
        cleanup_download_artifacts(base_name)
        set_status("🛑 cancelled — files deleted", force=True)
        set_live_status(job_id, "🛑 cancelled")
        return False

    # -- Failed: retry --
    if process.returncode != 0:
        if retry < max_retries:
            set_status(f"yt-dlp failed, retrying… ({retry+1}/{max_retries})", force=True)
            time.sleep(3)
            return retry_fn(retry + 1)
        set_status("❌ yt-dlp failed permanently", force=True)
        return False

    # -- Success --
    set_status(f"✅ downloaded: {base_name}", force=True)

    # Remove from persisted movie state
    MovieDownloadsState.load().remove(base_name)

    # Locate the file yt-dlp actually created
    actual = find_downloaded_file(base_name)
    if not actual:
        set_status("❌ could not find downloaded file", force=True)
        return False

    want_upscale = progress.upscale and progress.additional_choice != "2" \
        and progress.quality_choice in ("1", "2", "3")

    # Subtitles come from OpenSubtitles only — yt-dlp never downloads subs,
    # so subtitle handling can never break the video download. Fetching runs
    # in a background thread so a slow OpenSubtitles can't delay completion.
    # The subtitle is named to match the final video file exactly (minus the
    # file type) so the TV picks it up automatically. Upscaling renames the
    # video to <base_name>_4k.ext, so the subtitle is prefixed accordingly.
    if progress.subtitles and not find_subtitle_file(base_name):
        sub_base = f"{base_name}_4k" if want_upscale else base_name
        threading.Thread(
            target=fetch_subtitles,
            args=(progress.imdb_id, title, episode_info, sub_base, out_dir),
            kwargs={"post": lambda m: _upscale_post(loop, channel, m)},
            daemon=True).start()

    if want_upscale:
        # Hand the file to the background upscale worker so the next
        # download can start immediately while ffmpeg encodes this one.
        enqueue_upscale(actual, job_id=job_id, label=base_name,
                        cancel_event=cancel_event,
                        loop=loop, channel=channel,
                        set_status=set_status)
    elif progress.additional_choice != "2":
        # Non-upscaled video files get a faststart remux so they are
        # seekable/skippable and get checked for encoding glitches.
        result = verify_and_faststart(actual,
                                      status_cb=lambda m: set_status(m, force=True),
                                      cancel_event=cancel_event)
        if not result:
            if cancelled():
                set_status("🛑 cancelled during post-processing", force=True)
                safe_delete(actual)
                return False
            set_status("❌ glitch/corruption detected — will retry later", force=True)
            return False

    set_status("✅ done", force=True)
    return True


# =============================================================
# Chrome + clipboard downloader
# =============================================================
def download_one(url, title, episode_info, progress, notify, loop, channel,
                 job_id, cancel_event,
                 is_movie=False, retry=0):

    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    max_retries = 3

    header = ""
    if episode_info:
        header = f"📺 S{episode_info['season']:02d}E{episode_info['episode']:02d}"
    elif is_movie:
        header = "🎬 Movie"

    status_holder = {}
    async def _create_status():
        status_holder["msg"] = await channel.send(f"{header}\n_initializing…_")
    asyncio.run_coroutine_threadsafe(_create_status(), loop).result()
    status_msg = status_holder["msg"]

    last_edit = 0.0
    def set_status(text, force=False):
        nonlocal last_edit
        now = time.time()
        if not force and now - last_edit < STATUS_EDIT_INTERVAL:
            return
        last_edit = now
        async def _edit():
            try:
                await status_msg.edit(content=f"{header}\n`{text[:180]}`")
            except Exception as e:
                print(f"[status edit] {e}")
        asyncio.run_coroutine_threadsafe(_edit(), loop)

    set_status("starting…", force=True)

    if cancelled():
        set_status("🛑 cancelled before start", force=True)
        return False

    if not check_internet():
        set_status("waiting for internet…", force=True)
        waited = 0
        while waited < 300 and not check_internet():
            if cancelled():
                set_status("🛑 cancelled while waiting", force=True)
                return False
            time.sleep(5); waited += 5
        if not check_internet():
            set_status("❌ still offline, skipping", force=True)
            return False

    kill_chrome()
    time.sleep(2)

    try:
        chrome_process = subprocess.Popen([
            CHROME_PATH,
            f"--user-data-dir={CHROME_USER_DATA}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=CalculateNativeWinOcclusion",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            url
        ])
    except FileNotFoundError:
        set_status(f"❌ Chromium not found at {CHROME_PATH}", force=True)
        return False

    m3u8_url = None
    last_clip = ""
    max_wait = 120
    for attempt in range(max_wait):
        if cancelled():
            try: chrome_process.terminate()
            except Exception: pass
            kill_chrome()
            set_status("🛑 cancelled while waiting for URL", force=True)
            return False
        try:
            clip = pyperclip.paste()
        except Exception:
            clip = ""
        if clip and clip != last_clip and ".m3u8" in clip and clip.startswith("http"):
            m3u8_url = clip
            break
        last_clip = clip
        set_status(f"waiting for m3u8 URL… ({attempt}s)")
        time.sleep(1)

    try:
        chrome_process.terminate()
        time.sleep(1)
        kill_chrome()
    except Exception:
        pass

    if not m3u8_url:
        if retry < max_retries:
            set_status(f"retrying… ({retry+1}/{max_retries})", force=True)
            time.sleep(3)
            return download_one(url, title, episode_info, progress,
                                notify, loop, channel, job_id, cancel_event,
                                is_movie, retry + 1)
        if is_movie and "movie" in url:
            imdb_id = url.split("/movie/")[-1]
            series_url = f"https://vidcore.io/tv/{imdb_id}/1/1"
            set_status("trying series URL…", force=True)
            return download_one(series_url, title,
                                {"season": 1, "episode": 1},
                                progress, notify, loop, channel,
                                job_id, cancel_event, False, 0)
        set_status("❌ could not capture m3u8 URL", force=True)
        return False

    return _ytdlp_run(
        m3u8_url, title, episode_info, progress,
        status_msg=status_msg, set_status=set_status,
        notify=notify, loop=loop, channel=channel,
        job_id=job_id, cancel_event=cancel_event,
        max_retries=max_retries, retry=retry,
        retry_fn=lambda r: download_one(url, title, episode_info, progress,
                                        notify, loop, channel, job_id,
                                        cancel_event, is_movie, r)
    )


# =============================================================
# Subtitle downloader (OpenSubtitles REST API)
# =============================================================
_OS_TOKEN = {"token": None}


def os_login():
    if not OS_API_KEY or not OS_USERNAME or not OS_PASSWORD:
        return None
    if _OS_TOKEN.get("token"):
        return _OS_TOKEN["token"]
    try:
        r = requests.post(
            f"{OS_API_BASE}/login",
            headers={"Api-Key": OS_API_KEY,
                     "User-Agent": "MovieDownloaderBot/1.0",
                     "Content-Type": "application/json"},
            json={"username": OS_USERNAME, "password": OS_PASSWORD},
            timeout=15)
        r.raise_for_status()
        token = r.json().get("token")
        _OS_TOKEN["token"] = token
        return token
    except Exception as e:
        print(f"[opensubtitles login] {e}")
        return None


def os_search(imdb_id, episode_info=None):
    token = os_login()
    if not token:
        return None
    params = {"imdb_id": str(imdb_id)}
    if episode_info:
        params["season_number"] = str(episode_info.get("season"))
        params["episode_number"] = str(episode_info.get("episode"))
        params["type"] = "episode"
    else:
        params["type"] = "movie"
    langs = []
    for part in str(SUBTITLE_LANGS).split(","):
        code = part.strip().split("*")[0].strip()
        if code and code not in langs:
            langs.append(code)
    if langs:
        params["languages"] = ",".join(langs)
    try:
        r = requests.get(
            f"{OS_API_BASE}/subtitles",
            headers={"Api-Key": OS_API_KEY,
                     "Authorization": f"Bearer {token}",
                     "User-Agent": "MovieDownloaderBot/1.0"},
            params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[opensubtitles search] {e}")
        return None


def os_download(file_id):
    token = os_login()
    if not token:
        return None
    try:
        r = requests.post(
            f"{OS_API_BASE}/download",
            headers={"Api-Key": OS_API_KEY,
                     "Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "MovieDownloaderBot/1.0"},
            json={"file_id": file_id}, timeout=20)
        r.raise_for_status()
        link = r.json().get("link")
        if not link:
            return None
        rr = requests.get(link, headers={"User-Agent": "MovieDownloaderBot/1.0"},
                          timeout=30)
        rr.raise_for_status()
        return rr.content
    except Exception as e:
        print(f"[opensubtitles download] {e}")
        return None


def fetch_subtitles(imdb_id, title, episode_info, base_name, out_dir, post=None):
    """
    Download the best matching subtitle (from SUBTITLE_LANGS) for the given
    imdb_id / episode and save it next to the video as <base_name>.<ext> so it
    matches the video file name exactly (TV players pick it up automatically).
    """
    def post_msg(text):
        try:
            if post:
                post(text)
            else:
                print(text)
        except Exception as e:
            print(f"[subtitle post] {e}")

    if not imdb_id or str(imdb_id).lower() == "manual":
        return
    if not OS_API_KEY:
        post_msg(f"⚠️ OpenSubtitles not configured — subtitle skipped for `{base_name}`")
        return

    # Skip if a subtitle already exists alongside this file
    try:
        for f in os.listdir(out_dir):
            if f.startswith(base_name) and \
                    f.lower().endswith((".srt", ".vtt", ".ass", ".ssa")):
                return
    except OSError:
        pass

    data = os_search(imdb_id, episode_info)
    if not data or not data.get("data"):
        post_msg(f"⚠️ No subtitle found on OpenSubtitles for `{title}`.")
        return

    langs = [p.strip().split("*")[0].strip().lower()
             for p in str(SUBTITLE_LANGS).split(",") if p.strip()]
    chosen = None
    for lang in langs:
        for item in data.get("data", []):
            attrs = item.get("attributes", {}) or {}
            files = attrs.get("files") or []
            item_lang = (attrs.get("language") or "").lower()
            if not files or not item_lang.startswith(lang):
                continue
            chosen = (files[0].get("file_id"), lang)
            break
        if chosen:
            break

    if not chosen:
        post_msg(f"⚠️ No subtitle in requested languages for `{title}` on OpenSubtitles.")
        return

    file_id, _ = chosen
    content = os_download(file_id)
    if content is None:
        post_msg(f"⚠️ Subtitle download failed for `{base_name}`.")
        return

    # Unpack if OpenSubtitles returned a zip (multi-file subtitle)
    ext = ".srt"
    if content[:2] == b"PK":
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                names = [n for n in z.namelist()
                         if n.lower().endswith((".srt", ".vtt", ".ass", ".ssa"))]
                if names:
                    names.sort()
                    content = z.read(names[0])
                    ext = os.path.splitext(names[0])[1].lower()
        except Exception as e:
            print(f"[subtitle unzip] {e}")

    sub_path = os.path.join(out_dir, f"{base_name}{ext}")
    try:
        with open(sub_path, "wb") as f:
            f.write(content)
        post_msg(f"💬 Subtitle saved: {os.path.basename(sub_path)}")
    except Exception as e:
        print(f"[subtitle save] {e}")


# =============================================================
# Direct-URL downloader (m3u8, YouTube or any http(s) source; persists URL for resume)
# =============================================================
def download_from_url(url, title, episode_info, progress, notify, loop, channel,
                      job_id, cancel_event,
                      is_movie=True, retry=0):
    return _download_m3u8_url(url, title, episode_info, progress, notify, loop,
                              channel, job_id, cancel_event, is_movie, retry)


def _download_m3u8_url(m3u8_url, title, episode_info, progress, notify, loop, channel,
                       job_id, cancel_event,
                       is_movie=True, retry=0):

    max_retries = 3

    header = "🔗 Manual URL"
    if episode_info:
        header = f"📺 S{episode_info['season']:02d}E{episode_info['episode']:02d}"

    status_holder = {}
    async def _create_status():
        status_holder["msg"] = await channel.send(f"{header}\n_initializing…_")
    asyncio.run_coroutine_threadsafe(_create_status(), loop).result()
    status_msg = status_holder["msg"]

    last_edit = 0.0
    def set_status(text, force=False):
        nonlocal last_edit
        now = time.time()
        if not force and now - last_edit < STATUS_EDIT_INTERVAL:
            return
        last_edit = now
        async def _edit():
            try:
                await status_msg.edit(content=f"{header}\n`{text[:180]}`")
            except Exception as e:
                print(f"[status edit] {e}")
        asyncio.run_coroutine_threadsafe(_edit(), loop)

    set_status("starting direct download…", force=True)

    if cancel_event is not None and cancel_event.is_set():
        set_status("🛑 cancelled before start", force=True)
        return False

    if not check_internet():
        set_status("waiting for internet…", force=True)
        waited = 0
        while waited < 300 and not check_internet():
            if cancel_event is not None and cancel_event.is_set():
                set_status("🛑 cancelled while waiting", force=True)
                return False
            time.sleep(5); waited += 5
        if not check_internet():
            set_status("❌ still offline, skipping", force=True)
            return False

    # Persist the manual URL so we can resume if interrupted
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r'[^a-zA-Z0-9\- ]', '', title or "manual").replace(' ', '_')
    base_name = f"{safe}_{ts}_Q{progress.quality_choice}"
    MovieDownloadsState.load().add(
        base_name, m3u8_url, title or "manual",
        progress.quality_choice, progress.additional_choice
    )

    return _ytdlp_run(
        m3u8_url, title, episode_info, progress,
        status_msg=status_msg, set_status=set_status,
        notify=notify, loop=loop, channel=channel,
        job_id=job_id, cancel_event=cancel_event,
        max_retries=max_retries, retry=retry,
        retry_fn=lambda r: download_from_url(m3u8_url, title, episode_info,
                                             progress, notify, loop, channel,
                                             job_id, cancel_event, is_movie, r)
    )


# =============================================================
# Selection parser
# =============================================================
def make_range_selection(start, end, seasons):
    """Build an inclusive episode list from a (season, episode) range."""
    (s1, e1), (s2, e2) = start, end
    result = []
    for sn in range(s1, s2 + 1):
        if sn not in seasons:
            continue
        lo = e1 if sn == s1 else 1
        hi = e2 if sn == s2 else seasons[sn]
        for ep in range(lo, hi + 1):
            result.append((sn, ep))
    return result


def parse_selection(user_input, seasons):
    s = (user_input or "").strip().lower()
    if s == "all":
        return [(sn, ep) for sn in sorted(seasons) for ep in range(1, seasons[sn] + 1)]

    result = []

    def parse_part(part):
        part = part.strip()
        if not part:
            return None
        if "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                return None
            return list(range(int(a), int(b) + 1))
        if "," in part:
            if not all(x.isdigit() for x in part.split(",")):
                return None
            return [int(x) for x in part.split(",")]
        if part.isdigit():
            return [int(part)]
        return None

    if ":" in s:
        for chunk in s.split(";"):
            if ":" not in chunk:
                return None
            season_str, eps_str = chunk.split(":", 1)
            if not season_str.isdigit():
                return None
            season = int(season_str)
            if season not in seasons:
                return None
            eps = parse_part(eps_str)
            if not eps:
                return None
            for e in eps:
                if 1 <= e <= seasons[season]:
                    result.append((season, e))
        return result

    for chunk in s.replace(",", " ").split():
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                return None
            for sn in range(int(a), int(b) + 1):
                if sn in seasons:
                    for ep in range(1, seasons[sn] + 1):
                        result.append((sn, ep))
        elif chunk.isdigit():
            sn = int(chunk)
            if sn in seasons:
                for ep in range(1, seasons[sn] + 1):
                    result.append((sn, ep))
        else:
            return None

    return result


# =============================================================
# UI
# =============================================================
class OwnerRestrictedView(discord.ui.View):
    def __init__(self, owner_id, timeout=120):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.answer = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Only the requester can answer this.", ephemeral=True)
            return False
        return True


class QualitySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose quality…",
            options=[
                discord.SelectOption(label="Best available", value="1",
                                     description="Auto-select the highest quality"),
                discord.SelectOption(label="4K (2160p)", value="2"),
                discord.SelectOption(label="1080p", value="3"),
                discord.SelectOption(label="720p", value="4"),
                discord.SelectOption(label="480p", value="5"),
                discord.SelectOption(label="Audio only", value="6"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.answer = self.values[0]
        await interaction.response.defer()
        self.view.stop()


class QualityView(OwnerRestrictedView):
    def __init__(self, owner_id):
        super().__init__(owner_id)
        self.add_item(QualitySelect())


class ModeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Video / audio / both?",
            options=[
                discord.SelectOption(label="Video + audio (default)", value="3"),
                discord.SelectOption(label="Video only", value="1"),
                discord.SelectOption(label="Audio only", value="2"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.answer = self.values[0]
        await interaction.response.defer()
        self.view.stop()


class ModeView(OwnerRestrictedView):
    def __init__(self, owner_id):
        super().__init__(owner_id)
        self.add_item(ModeSelect())


class YesNoView(OwnerRestrictedView):
    def __init__(self, owner_id, prompt_yes="Yes", prompt_no="No"):
        super().__init__(owner_id)
        yes = discord.ui.Button(label=prompt_yes, style=discord.ButtonStyle.success)
        no  = discord.ui.Button(label=prompt_no,  style=discord.ButtonStyle.secondary)
        yes.callback = self._yes
        no.callback  = self._no
        self.add_item(yes)
        self.add_item(no)

    async def _yes(self, interaction):
        self.answer = True
        await interaction.response.defer()
        self.stop()

    async def _no(self, interaction):
        self.answer = False
        await interaction.response.defer()
        self.stop()


class RangeSeasonSelect(discord.ui.Select):
    def __init__(self, view, side):
        self._view = view
        self._side = side
        options = [discord.SelectOption(label=f"Season {s}", value=str(s))
                   for s in view.sorted_seasons]
        super().__init__(
            placeholder=f"{side.title()} point — season…",
            options=options,
        )

    async def callback(self, interaction):
        sn = int(self.values[0])
        ep_select = (self._view.start_episode if self._side == "start"
                     else self._view.end_episode)
        ep_select.options = [
            discord.SelectOption(label=f"Episode {e}", value=str(e))
            for e in range(1, self._view.seasons[sn] + 1)
        ]
        ep_select.disabled = False
        ep_select.placeholder = f"{self._side.title()} point — episode…"
        self._view._chosen[self._side]["season"] = sn
        self._view._chosen[self._side]["episode"] = None
        await interaction.response.edit_message(view=self._view)


class RangeEpisodeSelect(discord.ui.Select):
    def __init__(self, view, side):
        self._view = view
        self._side = side
        super().__init__(
            placeholder=f"{side.title()} point — episode…",
            options=[discord.SelectOption(
                label="— choose a season first —", value="0")],
            disabled=True,
        )

    async def callback(self, interaction):
        self._view._chosen[self._side]["episode"] = int(self.values[0])
        await interaction.response.defer()


class SeasonEpisodeRangeView(OwnerRestrictedView):
    def __init__(self, owner_id, seasons, timeout=180):
        super().__init__(owner_id, timeout=timeout)
        self.seasons = seasons
        self.sorted_seasons = sorted(seasons)
        self.answer = None
        self._chosen = {
            "start": {"season": None, "episode": None},
            "end": {"season": None, "episode": None},
        }

        self.start_season  = RangeSeasonSelect(self, "start")
        self.start_episode = RangeEpisodeSelect(self, "start")
        self.end_season    = RangeSeasonSelect(self, "end")
        self.end_episode   = RangeEpisodeSelect(self, "end")
        self.add_item(self.start_season)
        self.add_item(self.start_episode)
        self.add_item(self.end_season)
        self.add_item(self.end_episode)

        all_btn = discord.ui.Button(label="📦 All episodes",
                                    style=discord.ButtonStyle.primary)
        all_btn.callback = self._all
        self.add_item(all_btn)

        confirm = discord.ui.Button(label="✅ Download range",
                                    style=discord.ButtonStyle.success)
        confirm.callback = self._confirm
        self.add_item(confirm)

    async def _all(self, interaction):
        s1 = self.sorted_seasons[0]
        s2 = self.sorted_seasons[-1]
        self.answer = ((s1, 1), (s2, self.seasons[s2]))
        await interaction.response.defer()
        self.stop()

    async def _confirm(self, interaction):
        start = self._chosen["start"]
        end = self._chosen["end"]
        if None in (start["season"], start["episode"],
                    end["season"], end["episode"]):
            await interaction.response.send_message(
                "❌ Pick a season and an episode for BOTH the start and end.",
                ephemeral=True)
            return
        s1, e1 = start["season"], start["episode"]
        s2, e2 = end["season"], end["episode"]
        if (s2, e2) < (s1, e1):
            await interaction.response.send_message(
                "❌ The end point is before the start point.", ephemeral=True)
            return
        self.answer = ((s1, e1), (s2, e2))
        await interaction.response.defer()
        self.stop()


async def ask_quality_in_channel(ctx):
    view = QualityView(ctx.author.id)
    msg = await ctx.send("🎚️ **Quality?**", view=view)
    await view.wait()
    try:
        await msg.delete()
    except Exception:
        pass
    return view.answer or "1"


async def ask_mode_in_channel(ctx):
    view = ModeView(ctx.author.id)
    msg = await ctx.send("📼 **Download mode?**", view=view)
    await view.wait()
    try:
        await msg.delete()
    except Exception:
        pass
    return view.answer or "3"


async def ask_yesno_in_channel(ctx, text):
    view = YesNoView(ctx.author.id)
    msg = await ctx.send(text, view=view)
    await view.wait()
    try:
        await msg.delete()
    except Exception:
        pass
    return bool(view.answer)


async def ask_series_range_in_channel(ctx, seasons):
    """
    Dropdown range picker (start point → end point). Returns
    (selection, need_text): selection is None with need_text True when the
    catalog is too big for Discord selects (>25 options), None with
    need_text False when the user timed out / cancelled, otherwise the
    parsed episode list.
    """
    if len(seasons) > 25 or any(c > 25 for c in seasons.values()):
        return None, True

    view = SeasonEpisodeRangeView(ctx.author.id, seasons)
    msg = await ctx.send(
        "🎯 **Pick the range to download.** Choose the *start* point "
        "(season + episode), then the *end* point — everything in between "
        "will be downloaded.",
        view=view)
    await view.wait()
    try:
        await msg.delete()
    except Exception:
        pass

    if view.answer is None:
        return None, False
    return make_range_selection(view.answer[0], view.answer[1], seasons), False


# =============================================================
# Runners
# =============================================================
async def notify(channel, msg):
    try:
        await channel.send(msg)
    except Exception as e:
        print(f"[notify] {e}")


async def run_series(ctx, info, options, selection, job_id, cancel_event):
    imdb_id = info["imdb_id"]
    title = info["title"]
    seasons = info["seasons"]

    progress = DownloadProgress.load()
    if not progress or progress.imdb_id != imdb_id or progress.completed:
        progress = DownloadProgress()

    progress.title = title
    progress.imdb_id = imdb_id
    progress.kind = "series"
    progress.season_episode_counts = seasons
    progress.selected_episodes = selection
    progress.total_episodes = len(selection)
    if not progress.start_time:
        progress.start_time = time.time()
    progress.quality_choice = options["quality"]
    progress.additional_choice = options["additional"]
    progress.subtitles = options["subtitles"]
    progress.upscale = options["upscale"]
    progress.owner_id = ctx.author.id
    progress.channel_id = ctx.channel.id
    if not hasattr(progress, "completed_episodes"):
        progress.completed_episodes = []
    progress.save()

    save_state(owner_id=ctx.author.id, channel_id=ctx.channel.id)

    completed_set = {(s, e) for s, e in progress.completed_episodes}
    remaining = [ep for ep in selection if ep not in completed_set]

    await ctx.send(
        f"▶️ Starting **{title}** — {len(selection)} episodes in selection, "
        f"{len(completed_set & set(selection))} already done, "
        f"{len(remaining)} to go "
        f"(Q{options['quality']}, upscale={'on' if options['upscale'] else 'off'})"
    )

    loop = asyncio.get_running_loop()
    all_ok = True

    for season, episode in selection:
        if cancel_event.is_set():
            await ctx.send("🛑 Series cancelled.")
            break

        if (season, episode) in completed_set:
            await ctx.send(f"⏭️ S{season:02d}E{episode:02d} already downloaded, skipping")
            continue

        ep_info = {"season": season, "episode": episode}

        url = construct_vidcore_url(imdb_id, True, season, episode)
        ok = await asyncio.to_thread(
            download_one,
            url, title, ep_info, progress,
            lambda m: notify(ctx.channel, m),
            loop,
            ctx.channel,
            job_id,
            cancel_event,
            False, 0
        )

        if ok:
            progress.downloaded_episodes += 1
            key = [season, episode]
            if key not in progress.completed_episodes:
                progress.completed_episodes.append(key)
            completed_set.add((season, episode))
            progress.failed_episodes = [
                f for f in progress.failed_episodes
                if not (f["season"] == season and f["episode"] == episode)
            ]
        else:
            if cancel_event.is_set():
                break
            progress.failed_episodes.append(ep_info)
            all_ok = False
        progress.save()

        await ctx.send(
            f"📊 Progress: {progress.percent():.1f}% "
            f"({progress.downloaded_episodes}/{progress.total_episodes})"
        )

    if not cancel_event.is_set():
        progress.completed = all_ok
    progress.save()

    if cancel_event.is_set():
        return

    if all_ok:
        await ctx.send(f"🎉 **{title}** — all selected episodes downloaded!")
    else:
        failed = ", ".join(f"S{e['season']}E{e['episode']}" for e in progress.failed_episodes)
        await ctx.send(
            f"⚠️ **{title}** — done with failures: {failed}\n"
            f"💡 Use `!dlurl <m3u8_url|youtube_url> <title>` to supply a manual source for a failed episode."
        )

    try:
        dm = await ctx.author.create_dm()
        if all_ok:
            await dm.send(
                f"🎉 Done!\n**{title}** — "
                f"{progress.downloaded_episodes}/{progress.total_episodes} episodes downloaded."
            )
        else:
            await dm.send(
                f"⚠️ Finished with errors.\n**{title}** — "
                f"{progress.downloaded_episodes}/{progress.total_episodes} downloaded, "
                f"{len(progress.failed_episodes)} failed: "
                + ", ".join(f"S{e['season']}E{e['episode']}"
                            for e in progress.failed_episodes)
            )
    except Exception as e:
        print(f"[dm] {e}")


async def run_movie(ctx, info, options, job_id, cancel_event):
    imdb_id = info["imdb_id"]
    title = info["title"]

    progress = DownloadProgress()
    progress.title = title
    progress.imdb_id = imdb_id
    progress.kind = "movie"
    progress.quality_choice = options["quality"]
    progress.additional_choice = options["additional"]
    progress.subtitles = options["subtitles"]
    progress.upscale = options["upscale"]
    progress.owner_id = ctx.author.id
    progress.channel_id = ctx.channel.id
    progress.total_episodes = 1
    progress.downloaded_episodes = 0
    progress.completed_episodes = []
    progress.save()

    save_state(owner_id=ctx.author.id, channel_id=ctx.channel.id)

    loop = asyncio.get_running_loop()
    url = construct_vidcore_url(imdb_id, False)

    ok = await asyncio.to_thread(
        download_one,
        url, title, None, progress,
        lambda m: notify(ctx.channel, m),
        loop,
        ctx.channel,
        job_id,
        cancel_event,
        True, 0
    )

    if cancel_event.is_set():
        return

    if ok:
        progress.downloaded_episodes = 1
        progress.completed_episodes = [["movie", "movie"]]
        progress.completed = True
        progress.save()
        await ctx.send(f"🎉 **{title}** — downloaded!")
    else:
        progress.save()
        await ctx.send(f"⚠️ **{title}** — failed.")

    try:
        dm = await ctx.author.create_dm()
        if ok:
            await dm.send(f"🎉 Done!\n**{title}** downloaded successfully.")
        else:
            await dm.send(f"⚠️ **{title}** — download failed.")
    except Exception as e:
        print(f"[dm] {e}")


async def run_movie_with_url(ctx, info, options, url, job_id, cancel_event):
    title = info["title"]

    progress = DownloadProgress()
    progress.title = title
    progress.imdb_id = "manual"
    progress.kind = "movie_url"
    progress.quality_choice = options["quality"]
    progress.additional_choice = options["additional"]
    progress.subtitles = options["subtitles"]
    progress.upscale = options["upscale"]
    progress.owner_id = ctx.author.id
    progress.channel_id = ctx.channel.id
    progress.total_episodes = 1
    progress.downloaded_episodes = 0
    progress.completed_episodes = []
    progress.save()

    save_state(owner_id=ctx.author.id, channel_id=ctx.channel.id)

    loop = asyncio.get_running_loop()

    ok = await asyncio.to_thread(
        download_from_url,
        url, title, None, progress,
        lambda m: notify(ctx.channel, m),
        loop,
        ctx.channel,
        job_id,
        cancel_event,
        True, 0
    )

    if cancel_event.is_set():
        return

    if ok:
        progress.downloaded_episodes = 1
        progress.completed_episodes = [["movie", "movie"]]
        progress.completed = True
        progress.save()
        await ctx.send(f"🎉 **{title}** — downloaded!")
    else:
        progress.save()
        await ctx.send(f"⚠️ **{title}** — failed.")

    try:
        dm = await ctx.author.create_dm()
        if ok:
            await dm.send(f"🎉 Done!\n**{title}** downloaded successfully.")
        else:
            await dm.send(f"⚠️ **{title}** — download failed.")
    except Exception as e:
        print(f"[dm] {e}")


# =============================================================
# Download queue
# =============================================================
class _StubUser:
    def __init__(self, uid):
        self.id = uid

    async def create_dm(self):
        return await bot.create_dm(discord.Object(id=self.id))


class _JobCtx:
    def __init__(self, channel, author):
        self.channel = channel
        self.author = author

    async def send(self, m):
        await self.channel.send(m)


class Job:
    def __init__(self, kind, ctx, info, options, selection=None):
        self.job_id = next(job_id_counter)
        self.kind = kind
        self.owner_id = ctx.author.id
        self.owner_label = f"<@{ctx.author.id}>"
        self.channel = ctx.channel
        self.author = ctx.author
        self.ctx = ctx
        self.info = info
        self.options = options
        self.selection = selection
        self.title = info.get("title", "")
        self.status = "queued"
        self.task = None
        self.done_event = asyncio.Event()
        self.manual_url = None
        self.cancel_event = threading.Event()

    def describe(self):
        try:
            if self.kind == "series":
                sel = self.selection or []
                total = len(sel)
                seasons = sorted({s for s, _ in sel})
                if len(seasons) == 1:
                    sn = seasons[0]
                    full = self.info.get("seasons", {}).get(sn)
                    label = f"S{sn}"
                    if full and total == full:
                        label += " (full)"
                    else:
                        label += f" · {total} eps"
                elif seasons:
                    label = "S" + ",".join(str(s) for s in seasons) + f" · {total} eps"
                else:
                    label = f"{total} eps"
                return f"📺 {self.title} ({label})"
            if self.kind == "movie_url":
                return f"🔗 {self.title} (manual)"
            return f"🎬 {self.title}"
        except Exception:
            prefix = "📺" if self.kind == "series" else "🎬"
            return f"{prefix} {self.title}"


job_queue = deque()
job_id_counter = itertools.count(1)
job_cond = asyncio.Condition()
queue_worker_task = None
AUTO_RESUMED_ONCE = False


def ensure_queue_worker():
    global queue_worker_task
    if queue_worker_task is None or queue_worker_task.done():
        queue_worker_task = asyncio.create_task(run_queue_worker())
        print("[worker] queue worker started")


async def _notify_cond():
    async with job_cond:
        job_cond.notify_all()


def enqueue(job):
    job_queue.append(job)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_notify_cond())
    except RuntimeError:
        pass
    return 1 + sum(1 for j in job_queue
                   if j.status in ("queued", "running") and j is not job)


def prune_queue():
    while job_queue and job_queue[0].status in ("done", "cancelled"):
        job_queue.popleft()


def queue_status_text():
    jobs = [j for j in job_queue if j.status in ("queued", "running")]
    if not jobs:
        return "📭 No jobs in the queue."
    lines = ["⏳ **Download queue:**"]
    for pos, j in enumerate(jobs, 1):
        icon = "▶️" if j.status == "running" else "⏳"
        lines.append(f"{pos}. {icon} `#{j.job_id}` — {j.describe()} — by {j.owner_label}")
    return "\n".join(lines)


async def run_job(job):
    try:
        if job.kind == "series":
            await run_series(job.ctx, job.info, job.options, job.selection,
                             job.job_id, job.cancel_event)
        elif job.kind == "movie":
            await run_movie(job.ctx, job.info, job.options,
                            job.job_id, job.cancel_event)
        elif job.kind == "movie_url":
            await run_movie_with_url(job.ctx, job.info, job.options,
                                     job.manual_url, job.job_id, job.cancel_event)
        job.status = "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
        await notify(job.channel, f"🛑 Job #{job.job_id} cancelled.")
        raise
    except Exception as e:
        print(f"[queue worker] {e}")
        import traceback; traceback.print_exc()
        job.status = "cancelled"
        try:
            await notify(job.channel, f"⚠️ Job #{job.job_id} crashed: {e}")
        except Exception:
            pass
    finally:
        clear_live_status(job.job_id)
        job.done_event.set()
        async with job_cond:
            prune_queue()
            job_cond.notify_all()


async def run_queue_worker():
    print("[worker] loop running")
    while True:
        job = None
        async with job_cond:
            while job is None:
                job = next((j for j in job_queue if j.status == "queued"), None)
                if job is None:
                    await job_cond.wait()
            job.status = "running"
        print(f"[worker] starting job #{job.job_id}: {job.describe()}")
        await notify(job.channel, f"▶️ **Job #{job.job_id} starting**: {job.describe()}")
        job.task = asyncio.create_task(run_job(job))
        await job.done_event.wait()


# =============================================================
# Bot events
# =============================================================
@bot.event
async def on_ready():
    global AUTO_RESUMED_ONCE

    print(f"✅ Logged in as {bot.user}")
    ensure_queue_worker()

    if AUTO_RESUMED_ONCE:
        return
    AUTO_RESUMED_ONCE = True

    # --- Resume in-flight manual downloads ---
    mstate = MovieDownloadsState.load()
    if mstate.entries:
        state = load_state() or {}
        channel_id = state.get("channel_id")
        channel = None
        if channel_id:
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    pass

        if channel:
            for prefix, entry in list(mstate.entries.items()):
                await channel.send(
                    f"♻️ Resuming manual download **{entry['title']}** (prefix `{prefix}`)"
                )
                # Rebuild a minimal progress object
                prog = DownloadProgress()
                prog.title = entry["title"]
                prog.imdb_id = "manual"
                prog.kind = "movie_url"
                prog.quality_choice = entry.get("quality", "1")
                prog.additional_choice = entry.get("additional", "3")
                prog.owner_id = state.get("owner_id", 0)
                prog.channel_id = channel_id

                info = {"imdb_id": "manual", "title": entry["title"], "type": "movie"}
                opts = {"quality": prog.quality_choice,
                        "additional": prog.additional_choice,
                        "subtitles": False, "upscale": False}

                job = Job("movie_url", _JobCtx(channel, _StubUser(prog.owner_id)),
                          info, opts)
                job.manual_url = entry["url"]
                enqueue(job)

    # --- Resume series ---
    p = DownloadProgress.load()
    if not p or p.completed:
        return

    if not hasattr(p, "completed_episodes"):
        p.completed_episodes = []

    if p.imdb_id == "manual" or p.kind == "movie_url":
        return

    if p.kind == "movie" and not p.selected_episodes:
        return

    if p.total_episodes and p.downloaded_episodes >= p.total_episodes:
        return

    state = load_state() or {}
    channel_id = state.get("channel_id") or getattr(p, "channel_id", 0)
    owner_id   = state.get("owner_id")   or getattr(p, "owner_id", 0)
    if not channel_id:
        print("[resume] No saved channel — skipping auto-resume.")
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            print(f"[resume] Channel not found: {e}")
            return

    completed_count = len(p.completed_episodes)
    await channel.send(
        f"♻️ Auto-resuming **{p.title}** "
        f"({completed_count}/{p.total_episodes} done, skipping completed)"
    )

    info = {"imdb_id": p.imdb_id, "title": p.title, "seasons": p.season_episode_counts}
    opts = {
        "quality": p.quality_choice,
        "additional": p.additional_choice,
        "subtitles": p.subtitles,
        "upscale": p.upscale,
    }

    selection = p.selected_episodes or \
        [(s, e) for s in sorted(p.season_episode_counts)
         for e in range(1, p.season_episode_counts[s] + 1)]

    job = Job("series", _JobCtx(channel, _StubUser(owner_id)), info, opts, selection)
    enqueue(job)
    await channel.send(f"♻️ Auto-resume queued as job #{job.job_id}.")


# =============================================================
# Commands
# =============================================================
@bot.command(name="dl")
async def dl(ctx, *, query: str):
    if ALLOWED_USER_ID and ctx.author.id != ALLOWED_USER_ID:
        await ctx.send("⛔ Not authorized.")
        return

    ensure_queue_worker()

    await ctx.send(f"🔍 Searching IMDb for `{query}`…")
    results = search_imdb(query, limit=5)
    if not results:
        await ctx.send("❌ No results.")
        return

    lines = []
    for i, r in enumerate(results, 1):
        cat = classify(r.get('q', ''), r.get('qid', ''))
        lines.append(f"{i}. `{r.get('id')}` — {r.get('l')} ({r.get('y','?')}) [{cat}]")
    await ctx.send("**Results:**\n" + "\n".join(lines) +
                   "\nReply with the number (or `0` to cancel).")

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg = await bot.wait_for("message", check=check, timeout=60)
    except asyncio.TimeoutError:
        await ctx.send("⏰ Timed out.")
        return

    if msg.content.strip() == "0":
        await ctx.send("Cancelled.")
        return

    try:
        idx = int(msg.content.strip()) - 1
        result = results[idx]
    except Exception:
        await ctx.send("❌ Invalid selection.")
        return

    imdb_id = result.get("id")
    title = result.get("l")
    category = classify(result.get("q", ""), result.get("qid", ""))

    await ctx.send(f"📋 **{title}** — `{imdb_id}` — {category}")

    quality    = await ask_quality_in_channel(ctx)
    additional = await ask_mode_in_channel(ctx)
    subs       = await ask_yesno_in_channel(ctx, "💬 **Download subtitles?**")
    upscale    = await ask_yesno_in_channel(ctx, "🔼 **Upscale to 4K if not already 4K?**")

    options = {
        "quality": quality,
        "additional": additional,
        "subtitles": subs,
        "upscale": upscale,
    }

    info = {"imdb_id": imdb_id, "title": title, "type": category}

    if category == "series":
        await ctx.send("📺 Fetching seasons/episodes…")
        seasons = await asyncio.to_thread(get_series_info, imdb_id)
        if not seasons:
            await ctx.send("⚠️ Could not auto-detect — manual entry in channel.")
            seasons = {}
            await ctx.send("📝 Send each season as `1 6` "
                           "(season 1, 6 episodes). Type `done` to finish.")
            while True:
                try:
                    m = await bot.wait_for("message", check=check, timeout=120)
                except asyncio.TimeoutError:
                    break
                if m.content.strip().lower() == "done":
                    break
                parts = m.content.strip().split()
                if len(parts) == 2 and all(p.isdigit() for p in parts):
                    seasons[int(parts[0])] = int(parts[1])
                    await ctx.send(f"✔️ Season {parts[0]}: {parts[1]} eps")
                else:
                    await ctx.send("❌ Try `1 6` or `done`.")
            if not seasons:
                await ctx.send("❌ Aborting — no seasons.")
                return

        info["seasons"] = seasons

        lines = [f"**{title}** — available:"]
        for sn in sorted(seasons):
            lines.append(f"• Season {sn}: {seasons[sn]} episodes")
        await ctx.send("\n".join(lines))

        selection, need_text = await ask_series_range_in_channel(ctx, seasons)
        if need_text:
            # Fallback for huge catalogs (>25 options per dropdown)
            await ctx.send(
                "🎯 **Which episodes?** Reply with one of:\n"
                "`all` = everything\n"
                "`2` = only season 2\n"
                "`1 3` = seasons 1 and 3\n"
                "`1-2` = seasons 1 through 2\n"
                "`2:3-5` = season 2, episodes 3 to 5\n"
                "`2:1,3` = season 2, episodes 1 and 3"
            )
            try:
                m = await bot.wait_for("message", check=check, timeout=120)
            except asyncio.TimeoutError:
                await ctx.send("⏰ Timed out.")
                return

            selection = parse_selection(m.content, seasons)
            if not selection:
                await ctx.send("❌ Could not parse selection. Aborting.")
                return
        elif not selection:
            await ctx.send("❌ No range selected. Aborting.")
            return

        await ctx.send(f"▶️ {len(selection)} episodes selected.")

        job = Job("series", _JobCtx(ctx.channel, ctx.author), info, options, selection)
        position = enqueue(job)
        await ctx.send(f"⏳ Queued as job #{job.job_id} — position **{position}** in the queue.")
        return

    else:
        job = Job("movie", _JobCtx(ctx.channel, ctx.author), info, options)
        position = enqueue(job)
        await ctx.send(f"⏳ Queued as job #{job.job_id} — position **{position}** in the queue.")


@bot.command(name="dlurl")
async def dlurl(ctx, url: str, *, title: str = None):
    if ALLOWED_USER_ID and ctx.author.id != ALLOWED_USER_ID:
        await ctx.send("⛔ Not authorized.")
        return

    ensure_queue_worker()

    if not url.startswith(("http://", "https://")):
        await ctx.send("❌ That doesn't look like an http(s) URL "
                       "(m3u8, YouTube or direct link).")
        return

    if not title:
        title = "manual_download"

    await ctx.send(f"📋 Manual source: `{url[:80]}…`\nTitle: **{title}**")

    quality    = await ask_quality_in_channel(ctx)
    additional = await ask_mode_in_channel(ctx)
    subs       = await ask_yesno_in_channel(ctx, "💬 **Download subtitles?**")
    upscale    = await ask_yesno_in_channel(ctx, "🔼 **Upscale to 4K?**")

    options = {
        "quality": quality,
        "additional": additional,
        "subtitles": subs,
        "upscale": upscale,
    }

    info = {"imdb_id": "manual", "title": title, "type": "movie"}

    job = Job("movie_url", _JobCtx(ctx.channel, ctx.author), info, options)
    job.manual_url = url
    position = enqueue(job)
    await ctx.send(f"⏳ Queued as job #{job.job_id} — position **{position}** in the queue.")


@bot.command(name="status")
async def status(ctx):
    if ALLOWED_USER_ID and ctx.author.id != ALLOWED_USER_ID:
        return

    parts = []

    running_jobs = [j for j in job_queue if j.status == "running"]
    if running_jobs:
        lines = []
        for j in running_jobs:
            live = get_live_status(j.job_id)
            if live:
                lines.append(f"▶️ **#{j.job_id}** {j.describe()}\n`{live}`")
            else:
                lines.append(f"▶️ **#{j.job_id}** {j.describe()}\n_(starting…)_")
        parts.append("**Live status:**\n" + "\n".join(lines))

    upscales = get_upscale_activities()
    if upscales:
        ulines = []
        for u in sorted(upscales, key=lambda e: e.get("job_id", 0)):
            job_ref = f"#{u['job_id']}" if u.get("job_id") else "-"
            ulines.append(f"🔼 **job {job_ref}** {u.get('label', '?')}\n`{u.get('text', '…')}`")
        parts.append("**Upscaling:**\n" + "\n".join(ulines))

    if parts:
        await ctx.send("\n\n".join(parts))
        return

    p = DownloadProgress.load()
    if not p:
        await ctx.send("📭 No saved progress.")
        return

    done = len(getattr(p, "completed_episodes", []))
    total = p.total_episodes or 1

    is_movie = (
        p.total_episodes == 1
        and not p.season_episode_counts
        and (p.completed_episodes == [["movie", "movie"]] or not p.selected_episodes)
    )

    if is_movie:
        state = "✅ downloaded" if p.completed else "⏳ not downloaded"
        msg = f"📊 **{p.title}** — {state}"
        if p.failed_episodes:
            msg += f"\n❌ Failed: {len(p.failed_episodes)}"
        await ctx.send(msg)
    else:
        await ctx.send(
            f"📊 **{p.title}** — {p.percent():.1f}% "
            f"({done}/{total} episodes complete)"
            + (f"\n❌ Failed: {len(p.failed_episodes)}" if p.failed_episodes else "")
        )


@bot.command(name="queue")
async def queue_cmd(ctx):
    if ALLOWED_USER_ID and ctx.author.id != ALLOWED_USER_ID:
        return
    await ctx.send(queue_status_text())


@bot.command(name="cancel")
async def cancel(ctx, job_id: int = None):
    if ALLOWED_USER_ID and ctx.author.id != ALLOWED_USER_ID:
        return

    if job_id is not None:
        job = next((j for j in job_queue
                    if j.job_id == job_id and j.owner_id == ctx.author.id
                    and j.status in ("queued", "running")), None)
        if job is None:
            await ctx.send(f"❌ No active job #{job_id} of yours found.")
            return
        if job.status == "queued":
            job_queue.remove(job)
            await ctx.send(f"✅ Removed job #{job.job_id} from the queue.")
        else:
            job.cancel_event.set()
            if job.task:
                job.task.cancel()
            await ctx.send(f"🛑 Cancelling job #{job.job_id}…")
        return

    job = next((j for j in job_queue
                if j.owner_id == ctx.author.id and j.status == "running"), None)
    if job:
        job.cancel_event.set()
        if job.task:
            job.task.cancel()
        await ctx.send(f"🛑 Cancelling job #{job.job_id}…")
        return
    queued = next((j for j in job_queue
                   if j.owner_id == ctx.author.id and j.status == "queued"), None)
    if queued:
        await ctx.send(f"⚠️ Nothing running. Use `!cancel {queued.job_id}` "
                       "to remove your queued job.")
    else:
        await ctx.send("Nothing running.")


if __name__ == "__main__":
    if not load_credentials():
        print("❌ No DISCORD_TOKEN found. Fill in the file "
              f"'{CREDENTIALS_FILE}' and restart.")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)