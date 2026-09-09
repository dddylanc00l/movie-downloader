import subprocess
import time
import pyperclip
import os
import sys
import re
import requests
import json
from datetime import datetime
from urllib.parse import quote
import pickle
import shutil

# Your Python path
PYTHON_PATH = r"C:\Users\dylan\AppData\Local\Python\bin\python.exe"

# Progress file
PROGRESS_FILE = "download_progress.pkl"
MOVIES_FOLDER = "movies"

# Create movies folder if it doesn't exist
os.makedirs(MOVIES_FOLDER, exist_ok=True)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

class DownloadProgress:
    def __init__(self):
        self.title = ""
        self.imdb_id = ""
        self.total_episodes = 0
        self.downloaded_episodes = 0
        self.current_episode = 0
        self.current_season = 0
        self.completed = False
        self.failed_episodes = []
        self.season_episode_counts = {}
        self.start_time = None
        self.download_folder = MOVIES_FOLDER
    
    def save(self):
        try:
            with open(os.path.join(self.download_folder, PROGRESS_FILE), 'wb') as f:
                pickle.dump(self, f)
        except:
            pass
    
    def load(self):
        try:
            with open(os.path.join(MOVIES_FOLDER, PROGRESS_FILE), 'rb') as f:
                return pickle.load(f)
        except:
            return None
    
    def get_progress_percent(self):
        if self.total_episodes == 0:
            return 0
        return (self.downloaded_episodes / self.total_episodes) * 100

# ============ IMDb SCRAPER FUNCTIONS ============

def classify(q_value: str) -> str:
    """
    Turn IMDb's free-text 'q' field into a clean category.
    Handles 'TV series', 'TV mini-series', 'TV special', 'TV movie',
    'Podcast series', missing/empty values, and case differences.
    """
    q = (q_value or "").strip().lower()

    if "series" in q:
        return "series"
    if q in ("tv movie", "movie", "video", "short"):
        return "movie"
    if "tv" in q:
        # e.g. "tv special" — treat as a one-off, not a season-based series
        return "movie"
    return "unknown"

def search_imdb(query: str, limit: int = 5):
    """Search IMDb's suggestion API and return a list of candidate results."""
    if not query:
        return []

    first_char = query[0].lower() if query[0].isalnum() else "a"
    url = f"https://v2.sg.media-imdb.com/suggestion/{first_char}/{query}.json"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"⚠️ Search failed: {e}")
        return []

    results = [r for r in data.get('d', []) if r.get('id', '').startswith('tt')]
    return results[:limit]

def get_series_info(imdb_id: str) -> dict:
    """
    Return {season_number: episode_count} for a series by scraping the
    public IMDb episodes page. Returns {} if it can't be determined
    automatically (caller should fall back to manual entry).
    """
    seasons_info = {}

    try:
        url = f"https://www.imdb.com/title/{imdb_id}/episodes"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        text = resp.text
    except requests.RequestException as e:
        print(f"❌ Could not access episode page: {e}")
        return {}

    season_numbers = []
    season_options = re.findall(r'<option[^>]*value="(\d+)"[^>]*>', text)
    if season_options:
        season_numbers = sorted({int(s) for s in season_options if s.isdigit()})

    if not season_numbers:
        season_attrs = re.findall(r'data-season-number="(\d+)"', text)
        if season_attrs:
            season_numbers = sorted({int(s) for s in season_attrs})

    if not season_numbers:
        season_text = re.findall(r'Season\s+(\d+)', text)
        if season_text:
            season_numbers = sorted({int(s) for s in season_text})

    for season_num in season_numbers:
        episode_count = 0
        season_url = f"https://www.imdb.com/title/{imdb_id}/episodes?season={season_num}"
        try:
            season_resp = requests.get(season_url, headers=HEADERS, timeout=15)
            season_resp.raise_for_status()
            season_text = season_resp.text
        except requests.RequestException:
            continue

        episode_containers = re.findall(r'episode-container', season_text)
        if episode_containers:
            episode_count = len(episode_containers)
        else:
            episode_links = re.findall(
                rf'/title/tt\d+/episodes\?season={season_num}&episode=(\d+)', season_text
            )
            if episode_links:
                episode_count = len(set(episode_links))
            else:
                episode_items = re.findall(r'itemprop="episode"', season_text)
                if episode_items:
                    episode_count = len(episode_items)

        if episode_count > 0:
            seasons_info[season_num] = episode_count

    return seasons_info

def lookup_imdb(query: str):
    """Search for a title, classify it, and return metadata."""
    results = search_imdb(query)
    if not results:
        print(f"❌ No results found for '{query}'")
        return None

    if len(results) > 1:
        print(f"\n✅ Found {len(results)} results for '{query}':")
        for i, r in enumerate(results, 1):
            category = classify(r.get('q', ''))
            print(f"   {i}. {r.get('id')} — {r.get('l')} ({r.get('y', '?')}) [{category}]")
        choice = input(f"\nSelect number (1-{len(results)}), or Enter for first: ").strip()
        try:
            idx = int(choice) - 1 if choice else 0
        except ValueError:
            idx = 0
        result = results[idx] if 0 <= idx < len(results) else results[0]
    else:
        result = results[0]

    imdb_id = result.get('id', '')
    title = result.get('l', imdb_id)
    year = result.get('y', '?')
    category = classify(result.get('q', ''))

    print(f"\n✅ {title} ({year}) — {imdb_id} — classified as: {category}")

    info = {"imdb_id": imdb_id, "title": title, "year": year, "type": category}

    if category == "series":
        print("\n📺 Fetching season/episode info...")
        seasons_info = get_series_info(imdb_id)
        if seasons_info:
            total_eps = sum(seasons_info.values())
            print(f"📺 {len(seasons_info)} season(s), {total_eps} total episodes:")
            for s, c in sorted(seasons_info.items()):
                print(f"   Season {s}: {c} episodes")
            info["seasons"] = seasons_info
        else:
            print("⚠️ Could not auto-detect season/episode counts.")
            info["seasons"] = {}

    return info

# ============ DOWNLOAD FUNCTIONS ============

def construct_vidcore_url(imdb_id, is_series=False, season=1, episode=1):
    """Construct the VidCore URL using IMDb ID directly"""
    if is_series:
        return f"https://vidcore.io/tv/{imdb_id}/{season}/{episode}"
    else:
        return f"https://vidcore.io/movie/{imdb_id}"

def check_internet():
    try:
        requests.get('https://www.google.com', timeout=5)
        return True
    except:
        return False

def wait_for_internet(max_wait=300):
    print("\n🌐 Internet connection lost. Waiting...")
    waited = 0
    while waited < max_wait:
        if check_internet():
            print("✅ Internet restored!")
            return True
        time.sleep(5)
        waited += 5
        print(f"   Waiting... {waited}s / {max_wait}s", end="\r")
    return False

def open_vidcore_and_download(url, title=None, episode_info=None, progress=None, retry_count=0, is_movie=False):
    """Open VidCore and download the video with quality selection"""
    
    max_retries = 3
    
    if progress and progress.total_episodes > 0:
        print(f"\n📊 Progress: {progress.get_progress_percent():.1f}% ({progress.downloaded_episodes}/{progress.total_episodes})")
    
    if episode_info:
        print(f"📺 S{episode_info['season']:02d}E{episode_info['episode']:02d}")
    elif is_movie:
        print(f"🎬 MOVIE: {title}")
    
    if not check_internet():
        if not wait_for_internet():
            return False
    
    # Close Chrome
    try:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
        time.sleep(2)
    except:
        pass
    
    # Open Chrome with URL
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    user_data = r"C:\Users\dylan\AppData\Local\Google\Chrome\User Data"
    
    chrome_process = subprocess.Popen([
        chrome_path,
        f"--user-data-dir={user_data}",
        "--disable-blink-features=AutomationControlled",
        url
    ])
    
    m3u8_url = None
    last_clipboard = ""
    max_wait = 120
    
    for attempt in range(max_wait):
        try:
            current = pyperclip.paste()
            if current != last_clipboard and '.m3u8' in current and current.startswith('http'):
                m3u8_url = current
                break
            last_clipboard = current
            if attempt % 10 == 0:
                print(f"   ⏳ Waiting for URL... {attempt}s", end="\r")
            time.sleep(1)
        except:
            time.sleep(1)
    
    # Close Chrome
    try:
        chrome_process.terminate()
        time.sleep(1)
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    except:
        pass
    
    if not m3u8_url:
        if retry_count < max_retries:
            print(f"\n🔄 Retry {retry_count + 1}/{max_retries}")
            time.sleep(3)
            return open_vidcore_and_download(url, title, episode_info, progress, retry_count + 1, is_movie)
        
        # If this was a movie URL that failed, try series URL as fallback
        if is_movie and 'movie' in url:
            print("\n🔄 Movie URL failed. Trying series URL format as fallback...")
            imdb_id = url.split('/movie/')[-1]
            series_url = f"https://vidcore.io/tv/{imdb_id}/1/1"
            print(f"   Trying: {series_url}")
            return open_vidcore_and_download(series_url, title, {'season': 1, 'episode': 1}, progress, 0, False)
        
        return False
    
    # ---- QUALITY SELECTION ----
    print("\n" + "="*70)
    print("📋 QUALITY OPTIONS:")
    print("   1. Best available (auto)")
    print("   2. 4K (2160p) - if available")
    print("   3. 1080p - Full HD")
    print("   4. 720p - HD")
    print("   5. 480p - Standard")
    print("   6. Audio only")
    print("="*70)
    
    quality_choice = input("\nSelect quality option (1-6): ").strip() or "1"
    
    # Map quality choice to yt-dlp format selector
    quality_map = {
        '1': 'bestvideo+bestaudio/best',
        '2': 'bestvideo[height>=2160]+bestaudio/best',
        '3': 'bestvideo[height>=1080]+bestaudio/best',
        '4': 'bestvideo[height>=720]+bestaudio/best',
        '5': 'bestvideo[height>=480]+bestaudio/best',
        '6': 'bestaudio/best',
    }
    
    # Additional quality options
    print("\n📋 ADDITIONAL OPTIONS:")
    print("   1. Download video only")
    print("   2. Download audio only")
    print("   3. Download both (default)")
    
    additional_choice = input("\nSelect option (1-3): ").strip() or "3"
    
    format_selector = quality_map.get(quality_choice, 'bestvideo+bestaudio/best')
    
    # Build yt-dlp command
    yt_dlp_cmd = [
        PYTHON_PATH,
        "-m", "yt_dlp",
        "--referer", "https://vidcore.io/",
        m3u8_url,
    ]
    
    # Add format selector
    if additional_choice == '1':
        yt_dlp_cmd.extend(["-f", format_selector.replace('+bestaudio', '')])
    elif additional_choice == '2':
        yt_dlp_cmd.extend(["-f", "bestaudio/best"])
    else:
        yt_dlp_cmd.extend(["-f", format_selector])
    
    # Add merge format for best compatibility
    if additional_choice != '2':
        yt_dlp_cmd.extend(["--merge-output-format", "mkv"])
    
    # Add subtitle options
    subs = input("\nDownload subtitles? (y/n): ").strip().lower()
    if subs == 'y':
        yt_dlp_cmd.append("--write-subs")
        yt_dlp_cmd.append("--sub-lang")
        yt_dlp_cmd.append("en")
    
    # Save and download
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    download_folder = MOVIES_FOLDER
    
    if title:
        safe_title = re.sub(r'[^a-zA-Z0-9\- ]', '', title).replace(' ', '_')
        if episode_info:
            quality_label = quality_choice
            filename = f"{safe_title}_S{episode_info['season']:02d}E{episode_info['episode']:02d}_Q{quality_label}.%(ext)s"
        else:
            quality_label = quality_choice
            filename = f"{safe_title}_{timestamp}_Q{quality_label}.%(ext)s"
    else:
        filename = f"video_{timestamp}.%(ext)s"
    
    output_path = os.path.join(download_folder, filename)
    yt_dlp_cmd.extend(["-o", output_path])
    
    # Add progress options
    yt_dlp_cmd.extend(["--progress", "--newline"])
    
    # Show what's being downloaded
    print(f"\n📥 Downloading: {output_path}")
    print(f"⚙️  Format: {format_selector}")
    print("-" * 50)
    
    try:
        process = subprocess.Popen(
            yt_dlp_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        while True:
            output = process.stderr.readline()
            if output == '' and process.poll() is not None:
                break
            if output:
                if '[download]' in output or 'ETA' in output:
                    print(f"   {output.strip()}")
                else:
                    print(f"   {output.strip()}")
        
        returncode = process.poll()
        
        if returncode == 0:
            print(f"\n✅ Downloaded: {filename}")
            return True
        else:
            if retry_count < max_retries:
                print(f"\n🔄 Retry {retry_count + 1}/{max_retries}")
                time.sleep(3)
                return open_vidcore_and_download(url, title, episode_info, progress, retry_count + 1, is_movie)
            return False
            
    except Exception as e:
        print(f"❌ Error: {e}")
        if retry_count < max_retries:
            print(f"\n🔄 Retry {retry_count + 1}/{max_retries}")
            time.sleep(3)
            return open_vidcore_and_download(url, title, episode_info, progress, retry_count + 1, is_movie)
        return False

def download_series_auto(imdb_id, title, seasons_info):
    """Download all episodes of a series automatically"""
    
    total_seasons = len(seasons_info)
    total_episodes = sum(seasons_info.values())
    
    print(f"\n📺 DOWNLOADING SERIES: {title}")
    print(f"📊 Found {total_seasons} season(s) with {total_episodes} total episodes:")
    for season, count in sorted(seasons_info.items()):
        print(f"   Season {season}: {count} episodes")
    
    # Check for saved progress
    progress = DownloadProgress().load()
    
    if progress and progress.imdb_id == imdb_id and not progress.completed:
        print(f"\n📊 Found saved progress!")
        print(f"   ✅ Downloaded: {progress.downloaded_episodes}/{progress.total_episodes}")
        print(f"   📊 Progress: {progress.get_progress_percent():.1f}%")
        if progress.failed_episodes:
            print(f"   ❌ Failed: {len(progress.failed_episodes)} episodes (will retry)")
    else:
        progress = DownloadProgress()
        progress.title = title
        progress.imdb_id = imdb_id
        progress.season_episode_counts = seasons_info
        progress.total_episodes = total_episodes
        progress.start_time = time.time()
        progress.download_folder = MOVIES_FOLDER
        progress.save()
    
    # Show download options
    print(f"\n{'='*70}")
    print("📋 DOWNLOAD OPTIONS:")
    print("   1. Download a single episode")
    print("   2. Download from Season X, Episode Y to Season Z, Episode W")
    print("   3. Download entire series")
    print(f"{'='*70}")
    
    choice = input("\nSelect option (1-3): ").strip()
    
    if choice == '1':
        season = int(input("Enter season number: ").strip() or "1")
        episode = int(input("Enter episode number: ").strip() or "1")
        
        url = construct_vidcore_url(imdb_id, True, season, episode)
        episode_info = {'season': season, 'episode': episode}
        open_vidcore_and_download(url, title, episode_info, progress, 0, False)
        
    elif choice == '2':
        start_season = int(input("Enter starting season: ").strip() or "1")
        start_episode = int(input("Enter starting episode: ").strip() or "1")
        end_season = int(input("Enter ending season: ").strip() or start_season)
        end_episode = int(input("Enter ending episode: ").strip() or start_episode)
        
        if start_season > end_season or (start_season == end_season and start_episode > end_episode):
            print("❌ Invalid range: start > end")
            return
        
        episodes_to_download = []
        for season in range(start_season, end_season + 1):
            max_ep = seasons_info.get(season, 10)
            if season == start_season and season == end_season:
                ep_start = start_episode
                ep_end = min(end_episode, max_ep)
            elif season == start_season:
                ep_start = start_episode
                ep_end = max_ep
            elif season == end_season:
                ep_start = 1
                ep_end = min(end_episode, max_ep)
            else:
                ep_start = 1
                ep_end = max_ep
            
            for ep in range(ep_start, ep_end + 1):
                episodes_to_download.append((season, ep))
        
        print(f"\n📊 Will download {len(episodes_to_download)} episodes")
        confirm = input("Proceed? (y/n): ").strip().lower()
        if confirm != 'y':
            return
        
        for season, episode in episodes_to_download:
            if progress.downloaded_episodes >= progress.total_episodes:
                print("\n✅ All episodes downloaded!")
                progress.completed = True
                progress.save()
                return
            
            episode_key = {'season': season, 'episode': episode}
            url = construct_vidcore_url(imdb_id, True, season, episode)
            episode_info = {'season': season, 'episode': episode}
            
            success = open_vidcore_and_download(url, title, episode_info, progress, 0, False)
            
            if success:
                progress.downloaded_episodes += 1
                progress.failed_episodes = [ep for ep in progress.failed_episodes 
                                          if not (ep['season'] == season and ep['episode'] == episode)]
                progress.save()
            else:
                print(f"⚠️ Failed S{season:02d}E{episode:02d}")
                if episode_key not in progress.failed_episodes:
                    progress.failed_episodes.append(episode_key)
                progress.save()
            
            time.sleep(2)
        
    elif choice == '3':
        print(f"\n📊 Will download {total_episodes} episodes")
        confirm = input("Proceed? (y/n): ").strip().lower()
        if confirm != 'y':
            return
        
        for season in sorted(seasons_info.keys()):
            episode_count = seasons_info[season]
            
            print(f"\n{'='*70}")
            print(f"📺 SEASON {season} ({episode_count} episodes)")
            print(f"{'='*70}")
            
            for episode in range(1, episode_count + 1):
                if progress.downloaded_episodes >= progress.total_episodes:
                    print("\n✅ All episodes downloaded!")
                    progress.completed = True
                    progress.save()
                    return
                
                episode_key = {'season': season, 'episode': episode}
                url = construct_vidcore_url(imdb_id, True, season, episode)
                episode_info = {'season': season, 'episode': episode}
                
                success = open_vidcore_and_download(url, title, episode_info, progress, 0, False)
                
                if success:
                    progress.downloaded_episodes += 1
                    progress.failed_episodes = [ep for ep in progress.failed_episodes 
                                              if not (ep['season'] == season and ep['episode'] == episode)]
                    progress.save()
                else:
                    print(f"⚠️ Failed S{season:02d}E{episode:02d}")
                    if episode_key not in progress.failed_episodes:
                        progress.failed_episodes.append(episode_key)
                    progress.save()
                
                time.sleep(2)
    
    # Final summary
    print(f"\n{'='*70}")
    print("📊 DOWNLOAD SUMMARY")
    print(f"{'='*70}")
    print(f"✅ Downloaded: {progress.downloaded_episodes}/{progress.total_episodes} episodes")
    
    if progress.failed_episodes:
        print(f"❌ Failed: {len(progress.failed_episodes)} episodes")
        print("   Failed episodes:")
        for ep in progress.failed_episodes:
            print(f"   - Season {ep['season']}, Episode {ep['episode']}")
        print("\n💡 Run the script again to retry failed episodes")
    else:
        print("✅ All episodes downloaded successfully!")
        progress.completed = True
    
    progress.save()

def download_by_name(movie_name):
    """Main function: use IMDb scraper to get info, then download"""
    
    print("="*70)
    print("🎬 VIDCORE AUTO-DOWNLOADER")
    print("="*70)
    
    # Use the improved IMDb scraper
    info = lookup_imdb(movie_name)
    if not info:
        print("\n❌ Could not find movie/TV show")
        return
    
    imdb_id = info['imdb_id']
    title = info['title']
    category = info['type']
    
    if category == "series":
        seasons_info = info.get('seasons', {})
        
        if seasons_info:
            download_series_auto(imdb_id, title, seasons_info)
        else:
            print("\n⚠️ No season information available.")
            print("💡 Manual mode:")
            
            seasons_info = {}
            while True:
                season_num = input("\nEnter season number (or 'done' to finish): ").strip()
                if season_num.lower() == 'done':
                    break
                try:
                    season = int(season_num)
                    episode_input = input(f"Enter number of episodes for season {season}: ").strip()
                    try:
                        episodes = int(episode_input) if episode_input else 10
                        seasons_info[season] = episodes
                        print(f"   Season {season}: {episodes} episodes")
                    except:
                        seasons_info[season] = 10
                        print(f"   Season {season}: 10 episodes (default)")
                except:
                    print("❌ Invalid input")
            
            if seasons_info:
                download_series_auto(imdb_id, title, seasons_info)
            else:
                season = int(input("Enter season number (1): ") or "1")
                episode = int(input("Enter episode number (1): ") or "1")
                url = construct_vidcore_url(imdb_id, True, season, episode)
                episode_info = {'season': season, 'episode': episode}
                open_vidcore_and_download(url, title, episode_info, None, 0, False)
    else:
        # Movie
        print(f"\n🎬 MOVIE: {title}")
        url = construct_vidcore_url(imdb_id, False)
        success = open_vidcore_and_download(url, title, None, None, 0, True)
        
        if not success:
            print("\n❌ Movie URL failed. Would you like to try the series format?")
            if input("Try series format? (y/n): ").strip().lower() == 'y':
                season = int(input("Enter season number (1): ") or "1")
                episode = int(input("Enter episode number (1): ") or "1")
                series_url = construct_vidcore_url(imdb_id, True, season, episode)
                episode_info = {'season': season, 'episode': episode}
                open_vidcore_and_download(series_url, title, episode_info, None, 0, False)

def main():
    """Main entry point"""
    
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if 'vidcore.io' in arg:
            open_vidcore_and_download(arg)
            return
        elif arg.startswith('tt'):
            # IMDb ID provided directly
            imdb_id = arg
            result = search_imdb(arg)
            if result:
                title = result[0].get('l', imdb_id) if result else imdb_id
                category = classify(result[0].get('q', '')) if result else "unknown"
                
                if category == "series":
                    print(f"\n📺 TV Series: {title}")
                    seasons_info = get_series_info(imdb_id)
                    if seasons_info:
                        download_series_auto(imdb_id, title, seasons_info)
                    else:
                        season = int(input("Enter season number (1): ") or "1")
                        episode = int(input("Enter episode number (1): ") or "1")
                        url = construct_vidcore_url(imdb_id, True, season, episode)
                        episode_info = {'season': season, 'episode': episode}
                        open_vidcore_and_download(url, title, episode_info)
                else:
                    print(f"\n🎬 Movie: {title}")
                    url = construct_vidcore_url(imdb_id, False)
                    success = open_vidcore_and_download(url, title, None, None, 0, True)
                    
                    if not success:
                        print("\n❌ Movie URL failed. Would you like to try the series format?")
                        if input("Try series format? (y/n): ").strip().lower() == 'y':
                            season = int(input("Enter season number (1): ") or "1")
                            episode = int(input("Enter episode number (1): ") or "1")
                            series_url = construct_vidcore_url(imdb_id, True, season, episode)
                            episode_info = {'season': season, 'episode': episode}
                            open_vidcore_and_download(series_url, title, episode_info)
            return
        else:
            movie_name = ' '.join(sys.argv[1:])
            download_by_name(movie_name)
            return
    else:
        print("="*70)
        print("🎬 VIDCORE AUTO-DOWNLOADER")
        print("="*70)
        
        # Check for saved progress in movies folder
        progress = DownloadProgress().load()
        if progress and not progress.completed:
            print(f"\n📊 Found saved progress!")
            print(f"   📽️ Title: {progress.title}")
            print(f"   ✅ Downloaded: {progress.downloaded_episodes}/{progress.total_episodes}")
            print(f"   📊 Progress: {progress.get_progress_percent():.1f}%")
            if input("\nResume download? (y/n): ").lower() == 'y':
                download_series_auto(progress.imdb_id, progress.title, progress.season_episode_counts)
                return
        
        print("\nOptions:")
        print("  1. Download by movie/TV show name")
        print("  2. Download by IMDb ID")
        print("  3. Download by VidCore URL")
        
        choice = input("\nSelect option (1-3): ").strip()
        
        if choice == '1':
            movie_name = input("\nEnter movie or TV show name: ").strip()
            if movie_name:
                download_by_name(movie_name)
        elif choice == '2':
            imdb_id = input("\nEnter IMDb ID (e.g., tt9140554): ").strip()
            if imdb_id.startswith('tt'):
                result = search_imdb(imdb_id)
                if result:
                    title = result[0].get('l', imdb_id) if result else imdb_id
                    category = classify(result[0].get('q', '')) if result else "unknown"
                    
                    if category == "series":
                        print(f"\n📺 This is a TV Series: {title}")
                        seasons_info = get_series_info(imdb_id)
                        if seasons_info:
                            download_series_auto(imdb_id, title, seasons_info)
                        else:
                            season = int(input("Enter season number (1): ") or "1")
                            episode = int(input("Enter episode number (1): ") or "1")
                            url = construct_vidcore_url(imdb_id, True, season, episode)
                            episode_info = {'season': season, 'episode': episode}
                            open_vidcore_and_download(url, title, episode_info)
                    else:
                        print(f"\n🎬 Movie: {title}")
                        url = construct_vidcore_url(imdb_id, False)
                        success = open_vidcore_and_download(url, title, None, None, 0, True)
                        
                        if not success:
                            print("\n❌ Movie URL failed. Would you like to try the series format?")
                            if input("Try series format? (y/n): ").strip().lower() == 'y':
                                season = int(input("Enter season number (1): ") or "1")
                                episode = int(input("Enter episode number (1): ") or "1")
                                series_url = construct_vidcore_url(imdb_id, True, season, episode)
                                episode_info = {'season': season, 'episode': episode}
                                open_vidcore_and_download(series_url, title, episode_info)
            else:
                print("❌ Invalid IMDb ID")
        elif choice == '3':
            url = input("\nEnter VidCore URL: ").strip()
            if url:
                open_vidcore_and_download(url)

if __name__ == "__main__":
    try:
        import pyperclip
    except ImportError:
        subprocess.run([PYTHON_PATH, "-m", "pip", "install", "pyperclip"])
        import pyperclip
    
    main()