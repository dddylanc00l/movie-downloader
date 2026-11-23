#!/usr/bin/env python3
"""Test the OpenSubtitles API configuration used by bot.py.

Examples:
    python opensubtitles_test.py
    python opensubtitles_test.py --imdb tt0371746
    python opensubtitles_test.py --imdb tt0944947 --season 1 --episode 1
    python opensubtitles_test.py --download en
"""

import argparse
import os
import re
import sys
from urllib.parse import urlparse

try:
    import requests
except ModuleNotFoundError:
    print("ERROR: the requests package is not installed for this Python interpreter.")
    print("Run this script with the same Python environment that starts bot.py.")
    sys.exit(2)


API_URL = "https://api.opensubtitles.com/api/v1"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.txt")


def load_api_key():
    if not os.path.exists(CREDENTIALS_FILE):
        return ""
    with open(CREDENTIALS_FILE, encoding="utf-8") as credentials_file:
        for raw_line in credentials_file:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() == "OPENSUBTITLES_API_KEY":
                    return value.strip()
    return ""


def api_headers(api_key):
    return {
        "Api-Key": api_key,
        "User-Agent": "VidCore-OpenSubtitles-Test/1.0",
        "Accept": "application/json",
    }


def main():
    parser = argparse.ArgumentParser(
        description="Check the OpenSubtitles API key and subtitle search."
    )
    parser.add_argument("--imdb", default="tt0371746",
                        help="IMDb ID to search (default: Iron Man tt0371746)")
    parser.add_argument("--season", type=int, help="TV season number")
    parser.add_argument("--episode", type=int, help="TV episode number")
    parser.add_argument("--download", choices=("en", "nl"),
                        help="Download the highest-ranked subtitle for this language")
    parser.add_argument("--output-dir", default=SCRIPT_DIR,
                        help="Directory to save a downloaded subtitle (default: script directory)")
    args = parser.parse_args()

    api_key = load_api_key()
    if not api_key:
        print(f"ERROR: OPENSUBTITLES_API_KEY is missing from {CREDENTIALS_FILE}")
        print("Add this line, then run this script again:")
        print("OPENSUBTITLES_API_KEY=your_key_here")
        return 2

    imdb_id = args.imdb.lower().removeprefix("tt").lstrip("0")
    if not imdb_id.isdigit():
        print("ERROR: --imdb must look like tt0371746 or 0371746.")
        return 2

    params = {"languages": "en,nl", "order_by": "download_count",
              "order_direction": "desc"}
    if args.season is not None or args.episode is not None:
        if args.season is None or args.episode is None:
            print("ERROR: supply both --season and --episode for a series test.")
            return 2
        params.update({"parent_imdb_id": imdb_id, "season_number": args.season,
                       "episode_number": args.episode})
    else:
        params["imdb_id"] = imdb_id

    print(f"Testing OpenSubtitles with key ending in ...{api_key[-4:]}")
    print(f"Searching: {params}")
    try:
        response = requests.get(f"{API_URL}/subtitles", params=params,
                                headers=api_headers(api_key), timeout=30)
        print(f"Search response: HTTP {response.status_code}")
        if not response.ok:
            print(response.text[:500])
            return 1
        results = response.json().get("data", [])
    except requests.RequestException as error:
        print(f"ERROR: could not reach OpenSubtitles: {error}")
        return 1

    matches = {}
    for result in results:
        attrs = result.get("attributes", {})
        language = (attrs.get("language") or "").lower()
        files = attrs.get("files") or []
        if language in ("en", "nl") and files and language not in matches:
            matches[language] = {
                "file_id": files[0].get("file_id"),
                "release": attrs.get("release", "unknown release"),
                "downloads": attrs.get("download_count", 0),
            }

    for language in ("en", "nl"):
        match = matches.get(language)
        if match:
            print(f"OK: {language}: file_id={match['file_id']}, "
                  f"downloads={match['downloads']}, release={match['release']}")
        else:
            print(f"NOT FOUND: {language}")

    if args.download:
        match = matches.get(args.download)
        if not match:
            print(f"Cannot test a download: no {args.download} result was returned.")
            return 1
        try:
            download_response = requests.post(
                f"{API_URL}/download",
                params={"file_id": match["file_id"], "sub_format": "srt"},
                headers=api_headers(api_key), timeout=30,
            )
            print(f"Download-link response: HTTP {download_response.status_code}")
            if not download_response.ok:
                print(download_response.text[:500])
                return 1
            link = download_response.json().get("link")
            if not link:
                print("ERROR: OpenSubtitles returned no download link.")
                return 1
            print("Temporary subtitle download link was issued; downloading file...")
            file_response = requests.get(link, timeout=60)
            print(f"File download response: HTTP {file_response.status_code}")
            if not file_response.ok:
                print(file_response.text[:500])
                return 1

            output_dir = os.path.abspath(args.output_dir)
            os.makedirs(output_dir, exist_ok=True)
            release = re.sub(r'[<>:"/\\|?*]+', "_", match["release"]).strip(". ")
            filename = f"{release or 'subtitle'}.{args.download}.srt"
            output_path = os.path.join(output_dir, filename)
            with open(output_path, "wb") as subtitle_file:
                subtitle_file.write(file_response.content)
            print(f"OK: saved subtitle to {output_path}")
        except requests.RequestException as error:
            print(f"ERROR: could not request subtitle download link: {error}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
