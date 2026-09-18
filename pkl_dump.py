#!/usr/bin/env python3
"""
pkl_dump.py — Read-only inspector for the bot's state files.

Usage:
    python pkl_dump.py                    # dump all known files
    python pkl_dump.py <file.pkl>         # dump one file
    python pkl_dump.py --json             # also write JSON next to each file
    python pkl_dump.py --no-copy          # read in place (unsafe)
    python pkl_dump.py <folder>           # dump every .pkl in a folder

Known files (auto-detected in MOVIES_FOLDER):
    download_progress.pkl   -> DownloadProgress object
    movie_downloads.pkl     -> {prefix: {url, title, quality, additional}}
    bot_state.json          -> {owner_id, channel_id}

Safety:
    By default each file is copied to a temp location before being read,
    so a partially-written file never crashes the bot or the tool.
"""

import os
import sys
import json
import shutil
import pickle
import tempfile
from datetime import datetime


# =============================================================
# Stubs so pickle can resolve classes defined in bot.py's __main__
# =============================================================
class DownloadProgress:
    """Stub — matches bot.py's class name so pickle can find it.
    We only read attributes via getattr, so no methods are needed."""
    pass


class MovieDownloadsState:
    """Stub for the same reason."""
    pass
# =============================================================
# Paths
# =============================================================
MOVIES_FOLDER    = "/mnt/minecraft-backups/videos"
PROGRESS_FILE    = "download_progress.pkl"
MOVIE_STATE_FILE = "movie_downloads.pkl"
STATE_FILE       = "bot_state.json"


# =============================================================
# Reading
# =============================================================
def safe_load(path, copy=True):
    """
    Load a pickle from path. If copy=True, first copy to a temp file
    so we never read a half-written file the bot is currently writing.
    """
    if not os.path.exists(path):
        return None, "file does not exist"

    read_from = path
    tmp_path = None
    if copy:
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".pkl")
            os.close(fd)
            shutil.copy2(path, tmp_path)
            read_from = tmp_path
        except Exception as e:
            return None, f"copy failed: {e}"

    try:
        with open(read_from, 'rb') as f:
            obj = pickle.load(f)
        return obj, None
    except Exception as e:
        return None, f"unpickle failed: {e}"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def safe_load_json(path, copy=True):
    if not os.path.exists(path):
        return None, "file does not exist"

    read_from = path
    tmp_path = None
    if copy:
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".json")
            os.close(fd)
            shutil.copy2(path, tmp_path)
            read_from = tmp_path
        except Exception as e:
            return None, f"copy failed: {e}"

    try:
        with open(read_from) as f:
            return json.load(f), None
    except Exception as e:
        return None, f"json parse failed: {e}"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# =============================================================
# Pretty printers
# =============================================================
def sec(title):
    return "\n" + "=" * 60 + f"\n{title}\n" + "=" * 60


def fmt_ep(pair):
    """Format [season, episode] or (season, episode) as S01E02."""
    try:
        s, e = pair
        return f"S{int(s):02d}E{int(e):02d}"
    except Exception:
        return str(pair)


def dump_download_progress(obj):
    lines = []
    if obj is None:
        lines.append("(no data)")
        return "\n".join(lines)

    def g(name, default="?"):
        return getattr(obj, name, default)

    lines.append(f"title:                {g('title')}")
    lines.append(f"imdb_id:              {g('imdb_id')}")
    lines.append(f"kind:                 {g('kind')}")
    lines.append(f"total_episodes:       {g('total_episodes')}")
    lines.append(f"downloaded_episodes:  {g('downloaded_episodes')}")
    lines.append(f"completed:            {g('completed')}")
    lines.append(f"quality_choice:       {g('quality_choice')}")
    lines.append(f"additional_choice:    {g('additional_choice')}")
    lines.append(f"subtitles:            {g('subtitles')}")
    lines.append(f"upscale:              {g('upscale')}")
    lines.append(f"owner_id:             {g('owner_id')}")
    lines.append(f"channel_id:           {g('channel_id')}")
    lines.append(f"download_folder:      {g('download_folder')}")

    st = g('start_time', None)
    if st:
        try:
            lines.append(f"start_time:           {datetime.fromtimestamp(st)}")
        except Exception:
            lines.append(f"start_time:           {st}")
    else:
        lines.append(f"start_time:           None")

    # Percentage
    total = g('total_episodes', 0) or 0
    done = g('downloaded_episodes', 0) or 0
    pct = (done / total * 100) if total else 0
    lines.append(f"percent:              {pct:.1f}%")

    # Season episode counts
    sec_counts = g('season_episode_counts', {}) or {}
    if sec_counts:
        lines.append("")
        lines.append("season_episode_counts:")
        for sn in sorted(sec_counts):
            lines.append(f"  Season {sn}: {sec_counts[sn]} episodes")

    # Selected episodes
    sel = g('selected_episodes', []) or []
    if sel:
        lines.append("")
        lines.append(f"selected_episodes ({len(sel)}):")
        for ep in sel:
            lines.append(f"  - {fmt_ep(ep)}")

    # Completed episodes
    comp = g('completed_episodes', []) or []
    if comp:
        lines.append("")
        lines.append(f"completed_episodes ({len(comp)}):")
        for ep in comp:
            lines.append(f"  - {fmt_ep(ep)}")

    # Failed episodes
    failed = g('failed_episodes', []) or []
    if failed:
        lines.append("")
        lines.append(f"failed_episodes ({len(failed)}):")
        for ep in failed:
            if isinstance(ep, dict):
                lines.append(f"  - {fmt_ep((ep.get('season'), ep.get('episode')))}")
            else:
                lines.append(f"  - {fmt_ep(ep)}")

    return "\n".join(lines)


def dump_movie_state(obj):
    lines = []
    if obj is None:
        lines.append("(no data)")
        return "\n".join(lines)
    if not obj:
        lines.append("(empty — no in-flight manual downloads)")
        return "\n".join(lines)

    lines.append(f"entries: {len(obj)}")
    for prefix, entry in obj.items():
        lines.append("")
        lines.append(f"--- {prefix} ---")
        for k in ("title", "url", "quality", "additional"):
            v = entry.get(k, "?")
            if k == "url" and isinstance(v, str) and len(v) > 80:
                v = v[:80] + "…"
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def dump_json_obj(obj):
    if obj is None:
        return "(no data)"
    return json.dumps(obj, indent=2, default=str)


# =============================================================
# JSON conversion for --json mode
# =============================================================
def progress_to_dict(obj):
    if obj is None:
        return None
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items()}
    return str(obj)


# =============================================================
# Main logic
# =============================================================
KNOWN = [
    ("download_progress.pkl",  "DownloadProgress"),
    ("movie_downloads.pkl",    "MovieDownloadsState (dict)"),
    ("bot_state.json",         "Bot state (JSON)"),
]


def dump_one(path, use_copy=True, write_json=False):
    name = os.path.basename(path)

    if name.endswith(".json"):
        obj, err = safe_load_json(path, copy=use_copy)
        title = f"{name}  —  JSON state"
    else:
        obj, err = safe_load(path, copy=use_copy)
        title = f"{name}  —  pickle"

    print(sec(title))
    print(f"path: {path}")
    print(f"size: {os.path.getsize(path)} bytes")
    print(f"mtime: {datetime.fromtimestamp(os.path.getmtime(path))}")

    if err:
        print(f"\n❌ {err}")
        return

    print()

    if name == PROGRESS_FILE:
        print(dump_download_progress(obj))
    elif name == MOVIE_STATE_FILE:
        print(dump_movie_state(obj))
    elif name == STATE_FILE:
        print(dump_json_obj(obj))
    else:
        # Unknown pickle — generic dump
        print("(unknown file type — raw repr)")
        print(repr(obj))

    if write_json:
        out_path = path + ".json"
        try:
            with open(out_path, "w") as f:
                json.dump(progress_to_dict(obj) if not isinstance(obj, dict) else obj,
                          f, indent=2, default=str)
            print(f"\n💾 JSON written to: {out_path}")
        except Exception as e:
            print(f"\n⚠️ could not write JSON: {e}")


def main():
    args = sys.argv[1:]
    use_copy = "--no-copy" not in args
    write_json = "--json" in args

    # Strip flags
    positional = [a for a in args if not a.startswith("--")]

    print("=" * 60)
    print("pkl_dump.py — read-only state inspector")
    print("=" * 60)
    print(f"mode: {'copy-then-read' if use_copy else 'read-in-place'}")
    print(f"json output: {'yes' if write_json else 'no'}")

    if positional:
        target = positional[0]
        if os.path.isdir(target):
            # Dump every .pkl and known json in the folder
            found = False
            for name in sorted(os.listdir(target)):
                if name.endswith(".pkl") or name == STATE_FILE:
                    dump_one(os.path.join(target, name),
                             use_copy=use_copy, write_json=write_json)
                    found = True
            if not found:
                print(f"\n❌ No .pkl or state files found in {target}")
        else:
            dump_one(target, use_copy=use_copy, write_json=write_json)
        return

    # No args — dump all known files in MOVIES_FOLDER
    found_any = False
    for name, _ in KNOWN:
        p = os.path.join(MOVIES_FOLDER, name)
        if os.path.exists(p):
            dump_one(p, use_copy=use_copy, write_json=write_json)
            found_any = True

    if not found_any:
        print(f"\n❌ No known state files found in {MOVIES_FOLDER}")
        print("Known files searched:")
        for name, _ in KNOWN:
            print(f"  - {name}")


if __name__ == "__main__":
    main()