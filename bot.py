#!/usr/bin/env python3
"""
VidCore Discord Downloader — Raspberry Pi edition
- In-channel dropdown/button prompts (restricted to the requester)
- One editable status message per episode; live progress via !status
- Auto-upscale to 4K; deletes the non-4K source afterwards
- Cancel deletes partial downloads completely
- DMs the user ONLY when the job finishes
- Auto-resumes interrupted downloads ONCE per process
- Manual override: !dlurl <m3u8_url> [title]
- MP4 output (seekable) for maximum player compatibility
"""

import subprocess
import time
import pyperclip
import os
import sys
import re
import json
import pickle
import asyncio
import itertools
import shutil
import requests
import threading
import queue
from collections import deque
from datetime import datetime

import discord
from discord.ext import commands

# =============================================================
# CONFIG — Raspberry Pi
# =============================================================
DISCORD_TOKEN   = "PASTE_YOUR_BOT_TOKEN_HERE"
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

# Tunables
CONCURRENT_FRAGMENTS = 5
STATUS_EDIT_INTERVAL = 8.0

# =============================================================
# Setup
# =============================================================
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
    yt-dlp metadata, and the temp fragment directory).
    """
    try:
        for f in os.listdir(MOVIES_FOLDER):
            full = os.path.join(MOVIES_FOLDER, f)
            # Skip the temp dir itself
            if os.path.isdir(full):
                continue
            # Match anything starting with the prefix, including
            # .part, .ytdl, .f*.mp4 fragment files, etc.
            if f.startswith(output_prefix):
                safe_delete(full)
        # Also nuke any yt-dlp fragment dir for this download
        for entry in os.listdir(os.path.join(MOVIES_FOLDER, TEMP_FOLDER)):
            if entry.startswith(output_prefix):
                safe_delete(os.path.join(MOVIES_FOLDER, TEMP_FOLDER, entry))
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
    seasons = {}
    try:
        r = requests.get(f"https://www.imdb.com/title/{imdb_id}/episodes",
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        print(f"[imdb episodes] {e}")
        return {}

    season_nums = []
    for pat in [
        r'<option[^>]*value="(\d+)"[^>]*>',
        r'data-season-number="(\d+)"',
        r'Season\s+(\d+)',
    ]:
        found = re.findall(pat, text)
        if found:
            season_nums = sorted({int(s) for s in found if str(s).isdigit()})
            break

    for s in season_nums:
        try:
            sr = requests.get(
                f"https://www.imdb.com/title/{imdb_id}/episodes?season={s}",
                headers=HEADERS, timeout=15)
            sr.raise_for_status()
            st = sr.text
        except Exception:
            continue

        count = 0
        c = re.findall(r'episode-container', st)
        if c:
            count = len(c)
        else:
            c = re.findall(rf'/title/tt\d+/episodes\?season={s}&episode=(\d+)', st)
            if c:
                count = len(set(c))
            else:
                c = re.findall(r'itemprop="episode"', st)
                if c:
                    count = len(c)
        if count > 0:
            seasons[s] = count
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
        'ffmpeg', '-i', input_file,
        '-vf', 'scale=3840:2160:flags=lanczos',
        '-c:v', 'libx264',
        '-crf', '18', '-preset', 'faster',
        '-c:a', 'copy',
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

    out_path = os.path.join(MOVIES_FOLDER, base_name + ".%(ext)s")
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

    if progress.subtitles:
        yt_cmd += ["--write-subs", "--sub-lang", "en"]

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

    # -- Upscale --
    if progress.upscale and progress.additional_choice != "2" \
            and progress.quality_choice in ("1", "2", "3"):
        # Find whatever file yt-dlp actually created
        actual = None
        for f in os.listdir(MOVIES_FOLDER):
            if f.startswith(base_name) and not f.endswith(".part") \
                    and "_4k" not in f and os.path.isfile(
                        os.path.join(MOVIES_FOLDER, f)):
                actual = os.path.join(MOVIES_FOLDER, f)
                break

        if not actual:
            set_status("❌ could not find downloaded file for upscale", force=True)
            return False

        result = upscale_to_4k(actual,
                               status_cb=lambda m: set_status(m, force=True),
                               delete_source=True,
                               cancel_event=cancel_event)
        if not result:
            if cancelled():
                set_status("🛑 cancelled during upscale", force=True)
                # Delete the non-4K source too since user cancelled
                safe_delete(actual)
                return False
            set_status("❌ upscale failed — will retry next time", force=True)
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
# Direct-URL downloader (also persists URL for resume)
# =============================================================
def download_from_url(m3u8_url, title, episode_info, progress, notify, loop, channel,
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

        url = construct_vidcore_url(imdb_id, True, season, episode)
        ep_info = {"season": season, "episode": episode}

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
            f"💡 Use `!dlurl <m3u8_url> <title>` to supply a manual source for a failed episode."
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

    if ".m3u8" not in url or not url.startswith("http"):
        await ctx.send("❌ That doesn't look like an m3u8 URL.")
        return

    if not title:
        title = "manual_download"

    await ctx.send(f"📋 Manual URL: `{url[:80]}…`\nTitle: **{title}**")

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

    running_jobs = [j for j in job_queue if j.status == "running"]
    if running_jobs:
        lines = []
        for j in running_jobs:
            live = get_live_status(j.job_id)
            if live:
                lines.append(f"▶️ **#{j.job_id}** {j.describe()}\n`{live}`")
            else:
                lines.append(f"▶️ **#{j.job_id}** {j.describe()}\n_(starting…)_")
        await ctx.send("**Live status:**\n" + "\n".join(lines))
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
    bot.run(DISCORD_TOKEN)