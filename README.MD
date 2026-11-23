# Movie Downloader
"niet illegaal om te maken maar wel illegaal om te gebruiken"
A small collection of Python scripts and browser userscripts for finding a movie/series on IMDb, opening it on [VidCore](https://vidcore.io), capturing the underlying HLS `.m3u8` stream URL, downloading it with **yt-dlp**, and optionally upscaling it to 4K with **FFmpeg**.

For TV series the whole season can be queued and processed episode-by-episode, and partial downloads are automatically resumed after a reboot.

---

## Table of contents

- [How it works (the trick)](#how-it-works-the-trick)
- [Components](#components)
  - [`bot.py` — Discord bot (recommended)](#botpy--discord-bot-recommended)
  - [`movie.py` — interactive CLI](#moviepy--interactive-cli)
  - [`fetch.py` — quick single download](#fetchpy--quick-single-download)
  - [`resume.py` — recovery tool](#resumepy--recovery-tool)
  - [`tampermonkeyscript1` / `tampermonkeyscript2` — browser capture](#tampermonkeyscript1--tampermonkeyscript2--browser-capture)
  - [`movie.txt` — scratch notes](#movietxt--scratch-notes)
- [Installation](#installation)
- [Bot commands](#bot-commands)
- [Notes & limitations](#notes--limitations)

---

## How it works (the trick)

VidCore does not expose the final video URL directly. The downloader works around that:

1. **Find the title** — an IMDb search is done via the public suggestion API, and for series the season/episode counts are scraped from the IMDb episode page.
2. **Open the page in a real browser** — a headless-ish Chromium instance is launched with the VidCore URL.
3. **Capture the stream URL** — a Tampermonkey userscript (running in that browser) intercepts network/DOM/player events, and as soon as it sees an `.m3u8` URL it copies it to the system **clipboard** via `GM_setClipboard`. The Python script polls `pyperclip.paste()` until a valid `.m3u8` URL appears.
4. **Download** — the captured URL is handed to `yt-dlp` (`--cookies-from-browser`, a `vidcore.io` referer, and a few fragment/retry tuning flags). Series episodes are merged to `.mkv`.
5. **Optional upscale** — if requested, FFmpeg scales the file to 3840x2160 (`scale=3840:2160:flags=lanczos`, CRF 18) and deletes the non-4K original.
6. **Resume** — progress is pickled to `download_progress.pkl` so interrupted series can be continued (skipping already-completed episodes).

Because Chrome is launched/clobbered (see `pkill -f chromium` / `taskkill`) and the clipboard is a single shared resource, **only one download should run at a time** — `bot.py` enforces this with its download queue.

---

## Components

### `bot.py` — Discord bot (recommended)

The main, actively maintained tool. A `!`-prefixed Discord bot that does the whole flow through chat messages.

**Key features**

- `!dl <movie or show>` — IMDb search with numbered results, then interactive dropdowns/buttons for quality, mode (audio/video/both), subtitles, and 4K upscale, then episode selection.
- **Global download queue** — only one job runs at a time (required because Chrome + clipboard are shared). Any number of users can queue unlimited jobs; they run one at a time, newest last.
- Per-episode live status messages that update a single Discord message with progress / retries / errors.
- Auto-resume after a reboot — unfinished series are re-queued on `on_ready`, skipping completed episodes.
- DMs the requester only when their job finishes (success or failure).
- `!cancel` / `!queue` commands for managing queued jobs.

**Config** (top of file)

| Setting | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | The bot token (set to a placeholder — paste yours). |
| `ALLOWED_USER_ID` | `0` = anyone can use the bot; otherwise only this numeric Discord ID. |
| `MOVIES_FOLDER` | Where finished files are written (e.g. `/mnt/minecraft-backups/videos`). |
| `PROGRESS_FILE` / `STATE_FILE` | Pickle/JSON files for resume + saved channel/owner. |
| `CHROME_PATH` / `CHROME_USER_DATA` | Chromium binary and profile used for the clipboard capture trick. |
| `CONCURRENT_FRAGMENTS` | yt-dlp download parallelism (5). |
| `STATUS_EDIT_INTERVAL` | How often the status message is edited (8s). |

**Bot commands**

| Command | Description |
| --- | --- |
| `!dl <query>` | Search IMDb, answer the prompts, and enqueue a job. |
| `!queue` | List the running job and all queued jobs with their IDs and positions. |
| `!cancel [job_id]` | Cancel your running job; with a job ID, remove/cancel that specific job. |
| `!status` | Show saved progress percent + failures for the last series. |

**Runner internals**

- `run_series(ctx, info, options, selection)` — downloads each selected `(season, episode)` through `download_one()`, tracking completed/failed episodes in `DownloadProgress`, saving after every episode, and DMing on completion.
- `run_movie(...)` — same flow for a single movie.
- `download_one(...)` — opens Chrome, polls the clipboard for the `.m3u8`, runs yt-dlp, and (if requested) upscales. Retries up to 3 times; movie URLs fall back to the series URL format on repeated failure.
- `on_ready` — loads saved progress and re-queues the unfinished series through the queue worker.

### `movie.py` — interactive CLI

The standalone, terminal-driven version of the same idea. Everything happens with `input()` prompts.

- `python movie.py` — menu-driven (search by name, IMDb ID, or VidCore URL).
- `python movie.py "movie or show name"` — jump straight into the name search.
- `python movie.py ttXXXXXXX` — start from an IMDb ID.
- `python movie.py "https://vidcore.io/..."` — skip IMDb entirely and download a specific URL.

It has its own `DownloadProgress` class (with resume), the same IMDb scraping helpers, threaded subprocess output streaming (avoids buffering deadlocks), and optional FFmpeg 4K upscale. There is no queue here — it's a single-process tool.

> Note: this file hardcodes a Windows Chrome path and Python path, while `bot.py` targets a Linux (Raspberry Pi) setup. Paths are at the top of each file.

### `fetch.py` — quick single download

A minimal wrapper around the Chrome + clipboard capture + yt-dlp flow:

```bash
python fetch.py "https://vidcore.io/tv/tt9140554/1/2"
```

- Closes existing Chrome, opens the URL, waits for the `.m3u8` to appear in the clipboard, closes Chrome, saves the URL to `m3u8_url_<timestamp>.txt`, and downloads with yt-dlp.

### `resume.py` — recovery tool

Run this after an interrupted/crashed download to recover what was left behind:

- `find_partial_downloads()` — scans the movies folder for yt-dlp `.part` / `.ytdl` leftovers and re-runs yt-dlp with `--continue`.
- `find_interrupted_upscales()` — finds video files that lack a `_4k` version (an upscale that never finished) and offers to restart the FFmpeg upscale from the intact source.

### `tampermonkeyscript1` / `tampermonkeyscript2` — browser capture

Tampermonkey userscripts installed in the Chromium profile used by the downloaders. They are the "front end" of the clipboard trick: they watch the page and copy any `.m3u8` URL to the clipboard the moment the video player requests it.

- **`tampermonkeyscript1`** — "VidCore M3U8 Auto-Copier (Enhanced)": hooks `fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource`, `HLS.js`, DOM mutation watching, the Performance API, and messages.
- **`tampermonkeyscript2`** — "VidCore M3U8 Capture (Aggressive)": a newer/more aggressive version with the same hooks plus periodic DOM + Performance scans, `src`/`<source>` attribute hooks, Shaka Player support, and a `Ctrl+Shift+M` manual-scan shortcut. Only one needs to be installed.

Without one of these installed in the profile that `bot.py`/`movie.py`/`fetch.py` launch, the scripts will wait for the m3u8 forever and time out.

### `movie.txt` — scratch notes

Just a leftover notes file with example commands (Windows-style python invocation, a raw yt-dlp command, and a sample `master.m3u8` URL). Safe to ignore or delete.

---

## Installation

Requirements (all on the machine running the downloader):

- Python 3.9+ with `discord.py`, `requests`, `pyperclip`, and `yt-dlp`
  (`pip install discord.py requests pyperclip yt_dlp`)
- **FFmpeg** (and `ffprobe`) on PATH — needed for merging to mkv and 4K upscaling
- **Chromium/Chrome** + a **Tampermonkey** userscript installed in the profile the script launches (see `CHROME_PATH` / `CHROME_USER_DATA` in your script)

Then:

```bash
# run the Discord bot
python bot.py

# or the interactive CLI
python movie.py "Inception"
```

---

## Notes & limitations

- **One download at a time.** Chrome and the clipboard are single shared resources; all downloads go through the queue in `bot.py` (or run alone in the CLI scripts). Running two CLI jobs at once will corrupt each other.
- **Cancelling a running job** stops the Discord-side task but does *not* kill the already-spawned yt-dlp/ffmpeg subprocess.
- **Queue is in-memory** — a bot restart clears unstarted queued jobs (finished/in-progress series resumes via `download_progress.pkl`).
- The m3u8 capture depends on the site's player and the userscript hooks staying valid; if capture starts timing out, the Tampermonkey script may need updating.
- Video content on streaming sites is usually copyrighted — use responsibly.

---

## File overview

| File | Role |
| --- | --- |
| `bot.py` | Discord bot with queue, IMDb search, status messages, resume. |
| `movie.py` | Interactive terminal version of the same downloader. |
| `fetch.py` | Quick URL → try-ydl download one-liner. |
| `resume.py` | Recovers partial downloads and interrupted 4K upscales. |
| `tampermonkeyscript1` / `tampermonkeyscript2` | Browser userscripts that capture the m3u8 URL to the clipboard. |
| `movie.txt` | Scratch notes / example commands. |