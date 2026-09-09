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

# Your Python path
PYTHON_PATH = r"C:\Users\dylan\AppData\Local\Python\bin\python.exe"

# Progress file
PROGRESS_FILE = "download_progress.pkl"

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
    
    def save(self):
        try:
            with open(PROGRESS_FILE, 'wb') as f:
                pickle.dump(self, f)
        except:
            pass
    
    def load(self):
        try:
            with open(PROGRESS_FILE, 'rb') as f:
                return pickle.load(f)
        except:
            return None
    
    def get_progress_percent(self):
        if self.total_episodes == 0:
            return 0
        return (self.downloaded_episodes / self.total_episodes) * 100

def search_imdb(query):
    """Search IMDb using the API"""
    print(f"\n🔍 Searching IMDb for: {query}")
    
    try:
        first_char = query[0].lower()
        url = f"https://v2.sg.media-imdb.com/suggestion/{first_char}/{query}.json"
        
        response = requests.get(url, headers=HEADERS, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            results = data.get('d', [])
            
            if results:
                filtered_results = []
                for result in results:
                    imdb_id = result.get('id', '')
                    if imdb_id.startswith('tt'):
                        filtered_results.append(result)
                
                if filtered_results:
                    print(f"✅ Found {len(filtered_results)} results:")
                    for i, result in enumerate(filtered_results[:5], 1):
                        imdb_id = result.get('id', '')
                        title = result.get('l', '')
                        year = result.get('y', '')
                        is_series = result.get('q', '') == 'TV series' or result.get('q', '') == 'TVSeries'
                        type_display = 'TV Series' if is_series else 'Movie'
                        print(f"   {i}. {imdb_id} - {title} ({year}) [{type_display}]")
                    
                    if len(filtered_results) == 1:
                        return filtered_results[0]
                    else:
                        choice = input(f"\nSelect number (1-{min(5, len(filtered_results))}): ").strip()
                        try:
                            idx = int(choice) - 1
                            if 0 <= idx < len(filtered_results):
                                return filtered_results[idx]
                        except:
                            pass
                        return filtered_results[0]
    except Exception as e:
        print(f"   ⚠️ Search failed: {e}")
    
    return None

def get_series_info(imdb_id):
    """
    Get accurate season and episode info from IMDb.
    Uses multiple methods to ensure accuracy.
    """
    try:
        url = f"https://www.imdb.com/title/{imdb_id}/"
        response = requests.get(url, headers=HEADERS, timeout=15)
        
        if response.status_code != 200:
            return {}, 0
        
        text = response.text
        
        # ---- METHOD 1: JSON-LD Structured Data ----
        seasons_info = {}
        json_ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.DOTALL)
        
        for json_ld in json_ld_matches:
            try:
                data = json.loads(json_ld)
                if isinstance(data, dict):
                    # Check if it's a TV series
                    if data.get('@type') == 'TVSeries':
                        # Get number of seasons
                        if 'numberOfSeasons' in data:
                            total_seasons = int(data['numberOfSeasons'])
                            
                            # Try to get episode counts per season from containsSeason
                            if 'containsSeason' in data:
                                for season_data in data['containsSeason']:
                                    if isinstance(season_data, dict):
                                        season_num = int(season_data.get('seasonNumber', 0))
                                        episode_count = int(season_data.get('numberOfEpisodes', 0))
                                        if season_num > 0 and episode_count > 0:
                                            seasons_info[season_num] = episode_count
                            
                            # If we got seasons from containsSeason, return it
                            if seasons_info:
                                print(f"✅ Found {len(seasons_info)} seasons from JSON-LD")
                                for season, count in seasons_info.items():
                                    print(f"   Season {season}: {count} episodes")
                                return seasons_info, len(seasons_info)
            except:
                pass
        
        # ---- METHOD 2: Parse the episode page ----
        # If JSON-LD didn't work, we need to check each season individually
        print("🔍 Scraping episode pages to find seasons...")
        
        # First, try to find how many seasons there are
        season_matches = re.findall(r'data-season-number="(\d+)"', text)
        if not season_matches:
            season_matches = re.findall(r'Season\s+(\d+)', text)
        if not season_matches:
            season_matches = re.findall(r'"season":"(\d+)"', text)
        
        if season_matches:
            season_numbers = sorted(set([int(s) for s in season_matches]))
            total_seasons = len(season_numbers)
            print(f"📊 Found {total_seasons} season(s)")
            
            # For each season, get the episode count
            for season_num in season_numbers:
                episode_count = get_episode_count_from_page(imdb_id, season_num)
                if episode_count > 0:
                    seasons_info[season_num] = episode_count
                    print(f"   Season {season_num}: {episode_count} episodes")
                else:
                    # If we can't get the count, prompt the user
                    episode_count = input(f"How many episodes in season {season_num}? (default: 10): ").strip()
                    try:
                        episodes = int(episode_count) if episode_count else 10
                        seasons_info[season_num] = episodes
                    except:
                        seasons_info[season_num] = 10
            
            return seasons_info, len(seasons_info)
        
        # ---- METHOD 3: Fallback - prompt user ----
        print("⚠️ Could not automatically detect seasons and episodes")
        seasons_info = {}
        
        while True:
            season_num = input("\nEnter season number (or 'done' to finish): ").strip()
            if season_num.lower() == 'done':
                break
            try:
                season = int(season_num)
                episode_count = input(f"Enter number of episodes for season {season}: ").strip()
                try:
                    episodes = int(episode_count) if episode_count else 10
                    seasons_info[season] = episodes
                except:
                    seasons_info[season] = 10
            except:
                print("❌ Invalid input")
        
        return seasons_info, len(seasons_info)
        
    except Exception as e:
        print(f"❌ Error getting series info: {e}")
        return {}, 0

def get_episode_count_from_page(imdb_id, season):
    """Get episode count for a specific season from the episode page"""
    try:
        url = f"https://www.imdb.com/title/{imdb_id}/episodes?season={season}"
        response = requests.get(url, headers=HEADERS, timeout=15)
        
        if response.status_code != 200:
            return 0
        
        text = response.text
        
        # Method 1: Count episode containers
        episode_count = len(re.findall(r'episode-container', text))
        if episode_count > 0:
            return episode_count
        
        # Method 2: Count episode links
        episode_links = re.findall(r'/title/tt\d+/episodes\?season=' + str(season) + r'&episode=\d+', text)
        if episode_links:
            return len(set(episode_links))
        
        # Method 3: Look for "episode" in JSON-LD
        json_ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.DOTALL)
        for json_ld in json_ld_matches:
            try:
                data = json.loads(json_ld)
                if isinstance(data, dict):
                    if data.get('@type') == 'TVEpisode':
                        return 1
                    if 'numberOfEpisodes' in data:
                        return int(data['numberOfEpisodes'])
            except:
                pass
        
        return 0
        
    except Exception as e:
        print(f"⚠️ Could not get episode count for season {season}: {e}")
        return 0

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

def open_vidcore_and_download(url, title=None, episode_info=None, progress=None, retry_count=0):
    """Open VidCore and download the video with yt-dlp progress"""
    
    max_retries = 3
    
    if progress and progress.total_episodes > 0:
        print(f"\n📊 Progress: {progress.get_progress_percent():.1f}% ({progress.downloaded_episodes}/{progress.total_episodes})")
    
    if episode_info:
        print(f"📺 S{episode_info['season']:02d}E{episode_info['episode']:02d}")
    
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
            return open_vidcore_and_download(url, title, episode_info, progress, retry_count + 1)
        return False
    
    # Save and download with yt-dlp showing progress
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if title:
        safe_title = re.sub(r'[^a-zA-Z0-9\- ]', '', title).replace(' ', '_')
        if episode_info:
            output_name = f"{safe_title}_S{episode_info['season']:02d}E{episode_info['episode']:02d}.%(ext)s"
        else:
            output_name = f"{safe_title}_{timestamp}.%(ext)s"
    else:
        output_name = f"video_{timestamp}.%(ext)s"
    
    print(f"\n📥 Downloading: {output_name}")
    print("   yt-dlp will show progress below:")
    print("-" * 50)
    
    try:
        # Run yt-dlp with progress output
        result = subprocess.run([
            PYTHON_PATH,
            "-m", "yt_dlp",
            "--referer", "https://vidcore.io/",
            m3u8_url,
            "-o", output_name,
            "--progress",
            "--newline",
            "--no-playlist"
        ], capture_output=True, text=True)
        
        # yt-dlp outputs progress to stderr
        if result.stderr:
            print(result.stderr)
        if result.stdout:
            print(result.stdout)
        
        if result.returncode == 0:
            print(f"\n✅ Downloaded: {output_name}")
            return True
        else:
            if retry_count < max_retries:
                print(f"\n🔄 Retry {retry_count + 1}/{max_retries}")
                if "Connection" in str(result.stderr) or "timeout" in str(result.stderr).lower():
                    wait_for_internet()
                time.sleep(3)
                return open_vidcore_and_download(url, title, episode_info, progress, retry_count + 1)
            return False
            
    except Exception as e:
        print(f"❌ Error: {e}")
        if retry_count < max_retries:
            print(f"\n🔄 Retry {retry_count + 1}/{max_retries}")
            time.sleep(3)
            return open_vidcore_and_download(url, title, episode_info, progress, retry_count + 1)
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
        progress.save()
    
    # Ask which seasons to download
    if total_seasons > 1:
        season_input = input(f"\nEnter seasons to download (e.g., 1,2,3 or 'all'): ").strip()
        if season_input.lower() == 'all':
            season_list = sorted(seasons_info.keys())
        else:
            try:
                season_list = [int(s.strip()) for s in season_input.split(',')]
            except:
                print("⚠️ Invalid input, downloading all seasons")
                season_list = sorted(seasons_info.keys())
    else:
        season_list = sorted(seasons_info.keys())
    
    print(f"\n📊 Total episodes to download: {sum(seasons_info[s] for s in season_list)}")
    print("🚀 Starting automatic download...")
    
    # Download each episode
    for season in season_list:
        episode_count = seasons_info.get(season, 10)
        
        print(f"\n{'='*70}")
        print(f"📺 SEASON {season} ({episode_count} episodes)")
        print(f"{'='*70}")
        
        # Ask which episodes to download in this season
        episode_input = input(f"Enter episodes to download (e.g., 1,2,3 or 'all'): ").strip()
        if episode_input.lower() == 'all':
            episode_list = list(range(1, episode_count + 1))
        else:
            try:
                episode_list = [int(e.strip()) for e in episode_input.split(',')]
            except:
                print("⚠️ Invalid input, downloading all episodes")
                episode_list = list(range(1, episode_count + 1))
        
        for episode in episode_list:
            # Check if all episodes are done
            if progress.downloaded_episodes >= progress.total_episodes:
                print("\n✅ All episodes downloaded!")
                progress.completed = True
                progress.save()
                return
            
            # Check if this episode was already downloaded
            episode_key = {'season': season, 'episode': episode}
            if episode_key in progress.failed_episodes:
                print(f"\n🔄 Retrying S{season:02d}E{episode:02d}")
            else:
                print(f"\n📺 S{season:02d}E{episode:02d}")
            
            # Construct URL using IMDb ID directly
            url = construct_vidcore_url(imdb_id, True, season, episode)
            episode_info = {'season': season, 'episode': episode}
            
            # Download
            success = open_vidcore_and_download(url, title, episode_info, progress)
            
            if success:
                progress.downloaded_episodes += 1
                progress.failed_episodes = [ep for ep in progress.failed_episodes 
                                          if not (ep['season'] == season and ep['episode'] == episode)]
                progress.save()
                print(f"✅ Downloaded S{season:02d}E{episode:02d}")
            else:
                print(f"⚠️ Failed S{season:02d}E{episode:02d}")
                if episode_key not in progress.failed_episodes:
                    progress.failed_episodes.append(episode_key)
                progress.save()
            
            # Small delay between episodes
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
    """Main function: search IMDb, get URL, download"""
    
    print("="*70)
    print("🎬 VIDCORE AUTO-DOWNLOADER (IMDb Lookup)")
    print("="*70)
    
    # Search IMDb
    result = search_imdb(movie_name)
    if not result:
        print("\n❌ Could not find movie/TV show")
        return
    
    # Extract info from the API result
    imdb_id = result.get('id', '')
    title = result.get('l', imdb_id)
    year = result.get('y', '')
    
    # Use the API's type detection
    is_series = result.get('q', '') == 'TV series' or result.get('q', '') == 'TVSeries'
    
    print(f"\n✅ IMDb ID: {imdb_id}")
    print(f"📽️ Title: {title} ({year})")
    print(f"📺 Type: {'TV Series' if is_series else 'Movie'}")
    
    if is_series:
        print("\n📺 Getting season and episode information from IMDb...")
        seasons_info, total_seasons = get_series_info(imdb_id)
        
        if seasons_info:
            print(f"\n✅ Found {total_seasons} season(s):")
            for season, count in sorted(seasons_info.items()):
                print(f"   Season {season}: {count} episodes")
            
            download_all = input("\nDownload entire series? (y/n): ").lower() == 'y'
            
            if download_all:
                download_series_auto(imdb_id, title, seasons_info)
            else:
                # Manual selection
                season = int(input("Enter season number: ") or "1")
                episode = int(input("Enter episode number: ") or "1")
                url = construct_vidcore_url(imdb_id, True, season, episode)
                episode_info = {'season': season, 'episode': episode}
                open_vidcore_and_download(url, title, episode_info)
        else:
            print("❌ Could not get season information")
            # Fallback: let user manually enter
            season = int(input("Enter season number (1): ") or "1")
            episode = int(input("Enter episode number (1): ") or "1")
            url = construct_vidcore_url(imdb_id, True, season, episode)
            episode_info = {'season': season, 'episode': episode}
            open_vidcore_and_download(url, title, episode_info)
    else:
        url = construct_vidcore_url(imdb_id, False)
        open_vidcore_and_download(url, title)

def main():
    """Main entry point"""
    
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if 'vidcore.io' in arg:
            open_vidcore_and_download(arg)
            return
        elif arg.startswith('tt'):
            result = search_imdb(arg)
            if result:
                imdb_id = result.get('id', arg)
                title = result.get('l', imdb_id)
                is_series = result.get('q', '') == 'TV series' or result.get('q', '') == 'TVSeries'
                
                if is_series:
                    print(f"\n📺 TV Series: {title}")
                    seasons_info, total_seasons = get_series_info(imdb_id)
                    if seasons_info:
                        download_all = input("\nDownload entire series? (y/n): ").lower() == 'y'
                        if download_all:
                            download_series_auto(imdb_id, title, seasons_info)
                        else:
                            season = int(input("Enter season number (1): ") or "1")
                            episode = int(input("Enter episode number (1): ") or "1")
                            url = construct_vidcore_url(imdb_id, True, season, episode)
                            episode_info = {'season': season, 'episode': episode}
                            open_vidcore_and_download(url, title, episode_info)
                    else:
                        url = construct_vidcore_url(imdb_id, False)
                        open_vidcore_and_download(url, title)
                else:
                    print(f"\n🎬 Movie: {title}")
                    url = construct_vidcore_url(imdb_id, False)
                    open_vidcore_and_download(url, title)
            return
        else:
            movie_name = ' '.join(sys.argv[1:])
            download_by_name(movie_name)
            return
    else:
        print("="*70)
        print("🎬 VIDCORE AUTO-DOWNLOADER")
        print("="*70)
        
        # Check for saved progress
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
                    title = result.get('l', imdb_id)
                    is_series = result.get('q', '') == 'TV series' or result.get('q', '') == 'TVSeries'
                    
                    if is_series:
                        print(f"\n📺 This is a TV Series: {title}")
                        seasons_info, total_seasons = get_series_info(imdb_id)
                        if seasons_info:
                            download_all = input("\nDownload entire series? (y/n): ").lower() == 'y'
                            if download_all:
                                download_series_auto(imdb_id, title, seasons_info)
                            else:
                                season = int(input("Enter season number (1): ") or "1")
                                episode = int(input("Enter episode number (1): ") or "1")
                                url = construct_vidcore_url(imdb_id, True, season, episode)
                                episode_info = {'season': season, 'episode': episode}
                                open_vidcore_and_download(url, title, episode_info)
                        else:
                            url = construct_vidcore_url(imdb_id, False)
                            open_vidcore_and_download(url, title)
                    else:
                        print(f"\n🎬 Movie: {title}")
                        url = construct_vidcore_url(imdb_id, False)
                        open_vidcore_and_download(url, title)
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