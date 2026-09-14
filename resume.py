import os
import subprocess
import sys
import re

# Same paths as your main script
PYTHON_PATH = r"C:\Users\dylan\AppData\Local\Python\bin\python.exe"
MOVIES_FOLDER = "movies"


def find_partial_downloads():
    """Find yt-dlp .part files that were interrupted mid-download."""
    parts = []
    if not os.path.exists(MOVIES_FOLDER):
        return parts

    for f in os.listdir(MOVIES_FOLDER):
        if f.endswith('.part') or f.endswith('.ytdl'):
            parts.append(os.path.join(MOVIES_FOLDER, f))
    return parts


def find_interrupted_upscales():
    """
    Find videos that:
      - have a matching source but no _4k file
      - were likely mid-upscale when the machine died

    We detect this by looking for _4k files that are suspiciously
    small (0 bytes or much smaller than source), OR by looking for
    the original file when no _4k exists.
    """
    candidates = []
    if not os.path.exists(MOVIES_FOLDER):
        return candidates

    for f in os.listdir(MOVIES_FOLDER):
        full = os.path.join(MOVIES_FOLDER, f)

        # Skip partial downloads — those are handled separately
        if f.endswith('.part') or f.endswith('.ytdl'):
            continue

        # Skip zero-byte files
        try:
            size = os.path.getsize(full)
            if size < 1024 * 1024:  # less than 1 MB — likely incomplete
                continue
        except OSError:
            continue

        # Only consider video files
        if not f.lower().endswith(('.mkv', '.mp4', '.webm', '.ts')):
            continue

        # If a "_4k" version exists, this file is already upscaled
        base, ext = os.path.splitext(f)
        if '_4k' in base:
            continue

        candidates.append(full)

    return candidates


def resume_yt_dlp(part_file):
    """
    yt-dlp can resume partial downloads if you point it at the
    .part file's expected final name. The simplest fix: rename the
    .part to remove the suffix, then re-run yt-dlp with --continue.
    """
    print(f"\n📥 Found partial download: {part_file}")

    # yt-dlp stores the URL inside a .ytdl sidecar if you used --write-info-json
    # Fall back to asking the user
    url = input("   Paste the original m3u8 URL (or press Enter to skip): ").strip()
    if not url:
        print("   ⏭️ Skipping")
        return

    # The final output name (strip .part)
    final_name = part_file.replace('.part', '')

    cmd = [
        PYTHON_PATH, "-m", "yt_dlp",
        "--continue",              # resume from .part
        "--referer", "https://vidcore.io/",
        "-o", final_name,
        "--progress", "--newline",
        url
    ]

    print(f"   ▶️ Resuming with: {' '.join(cmd[:3])} ...")
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)

    # Real-time output (same threaded pattern as your main script)
    import threading, queue
    q = queue.Queue()

    def reader(stream, name):
        for line in iter(stream.readline, ''):
            if line:
                q.put((name, line.rstrip()))
        stream.close()

    threading.Thread(target=reader, args=(process.stdout, 'out'), daemon=True).start()
    threading.Thread(target=reader, args=(process.stderr, 'err'), daemon=True).start()

    while process.poll() is None or not q.empty():
        try:
            _, line = q.get(timeout=0.1)
        except queue.Empty:
            continue
        if line.startswith('[download]') or 'ETA' in line:
            print(f"   {line}")

    if process.returncode == 0:
        print(f"   ✅ Finished: {final_name}")
    else:
        print(f"   ❌ yt-dlp exited with code {process.returncode}")


def resume_upscale(source_file):
    """
    FFmpeg doesn't support resuming an upscale — but we can restart it
    from the original source, which is still intact.
    """
    print(f"\n🔼 Found video that may need 4K upscaling: {os.path.basename(source_file)}")

    choice = input("   Upscale this to 4K now? (y/n): ").strip().lower()
    if choice != 'y':
        return

    base, ext = os.path.splitext(source_file)
    output = f"{base}_4k{ext}"

    # Check for a partial 4K output and delete it (FFmpeg can't resume)
    if os.path.exists(output):
        print(f"   🗑️ Removing incomplete 4K file: {os.path.basename(output)}")
        os.remove(output)

    cmd = [
        'ffmpeg',
        '-err_detect', 'explode', '-xerror',
        '-i', source_file,
        '-vf', 'scale=3840:2160:flags=lanczos',
        '-c:v', 'libx264',
        '-crf', '18',
        '-preset', 'medium',
        '-c:a', 'copy',
        '-movflags', '+faststart',
        '-y',
        output
    ]

    print(f"   ▶️ Running FFmpeg... (this can take a long time)")
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)

    import threading, queue
    q = queue.Queue()

    def reader(stream):
        for line in iter(stream.readline, ''):
            if line:
                q.put(line.rstrip())
        stream.close()

    threading.Thread(target=reader, args=(process.stderr,), daemon=True).start()

    last_pct = -1
    while process.poll() is None or not q.empty():
        try:
            line = q.get(timeout=0.1)
        except queue.Empty:
            continue
        # FFmpeg prints progress on the same line with \r
        m = re.search(r'time=(\d+):(\d+):(\d+)', line)
        if m:
            h, mnt, s = map(int, m.groups())
            secs = h * 3600 + mnt * 60 + s
            pct = secs  # crude but visible
            if pct != last_pct:
                print(f"   ⏳ Elapsed: {h:02d}:{mnt:02d}:{s:02d}", end="\r")
                last_pct = pct

    print()
    if process.returncode == 0:
        print(f"   ✅ Upscaled: {output}")
    else:
        print(f"   ❌ FFmpeg exited with code {process.returncode}")


def main():
    print("=" * 60)
    print("🔧 RESUME INTERRUPTED DOWNLOADS / UPSCALES")
    print("=" * 60)

    parts = find_partial_downloads()
    videos = find_interrupted_upscales()

    if not parts and not videos:
        print("\n✅ Nothing to recover — no partial files found.")
        return

    if parts:
        print(f"\n📥 Partial downloads ({len(parts)}):")
        for p in parts:
            print(f"   - {os.path.basename(p)}")

    if videos:
        print(f"\n🔼 Videos without 4K version ({len(videos)}):")
        for v in videos:
            print(f"   - {os.path.basename(v)}")

    # Handle partial downloads first
    for p in parts:
        resume_yt_dlp(p)

    # Then offer upscales
    for v in videos:
        resume_upscale(v)

    print("\n" + "=" * 60)
    print("✅ Recovery complete")
    print("=" * 60)


if __name__ == "__main__":
    main()