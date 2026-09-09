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
    """Search IMDb using the API and return the selected result with metadata"""
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

def get_movie_info_manual(imdb_id):
    """Get movie/TV show info from IMDb page - only used if API result is unavailable"""
    try:
        url = f"https://www.imdb.com/title/{imdb_id}/"
        response = requests.get(url, headers=HEADERS, timeout=15)
        
        if response.status_code != 200:
            return imdb_id, False, 0
        
        text = response.text
        
        # ---- DETECTION METHODS ----
        is_series = False
        
        # Method 1: Check for "TV Series" text
        if 'TV Series' in text:
            is_series = True
        
        # Method 2: Check JSON-LD for TVSeries type
        json_ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.DOTALL)
        for json_ld in json_ld_matches:
            try:
                data = json.loads(json_ld)
                if isinstance(data, dict):
                    if data.get('@type') == 'TVSeries':
                        is_series = True
                        break
            except:
                pass
        
        # Method 3: Check for TV rating
        if not is_series:
            if 'TV-MA' in text or 'TV-14' in text or 'TV-PG' in text or 'TV-G' in text or 'TV-Y' in text:
                is_series = True
        
        # Method 4: Check for Season/Episode patterns
        if not is_series:
            if re.search(r'Season\s+\d+', text) or re.search(r'S\d+E\d+', text):
                is_series = True
        
        # Get title
        title_match = re.search(r'<title>(.*?)</title>', text)
        title = title_match.group(1).strip() if title_match else imdb_id
        title = title.replace(' - IMDb', '').strip()
        title = re.sub(r'\(TV Series.*?\)', '', title).strip()
        
        # Get number of seasons
        seasons = 0
        if is_series:
            for json_ld in json_ld_matches:
                try:
                    data = json.loads(json_ld)
                    if isinstance(data, dict):
                        if 'numberOfSeasons' in data:
                            seasons = int(data['numberOfSeasons'])
                            break
                except:
                    pass
            
            if seasons == 0:
                season_matches = re.findall(r'Season\s+(\d+)', text)
                if season_matches:
                    seasons = max([int(s) for s in season_matches])
                else:
                    seasons = 1
        
        return title, is_series, seasons
        
    except Exception as e:
        print(f"❌ Error getting movie info: {e}")
        return imdb_id, False, 0

def get_episode_count(imdb_id, season):
    """Get number of episodes in a season from IMDb"""
    try:
        url = f"https://www.imdb.com/title/{imdb_id}/episodes?season={season}"
        response = requests.get(url, headers=HEADERS, timeout=15)
        
        if response.status_code != 200:
            return 10
        
        text = response.text
        
        # Count episode containers
        episode_count = len(re.findall(r'episode-container', text))
        
        if episode_count > 0:
            return episode_count
        
        # Count episode links
        episode_links = re.findall(r'/title/tt\d+/episodes\?season=' + str(season) + r'&episode=\d+', text)
        if episode_links:
            return len(set(episode_links))
        
        # Default
        print(f"⚠️ Could not determine episode count for season {season}")
        user_count = input(f"How many episodes in season {season}? (default: 10): ").strip()
        try:
            return int(user_count) if user_count else 10
        except:
            return 10
        
    except Exception as e:
        print(f"⚠️ Could not determine episode count: {e}")
        return 10

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
    """Open VidCore and download the video"""
    
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
                print(f"   ⏳ Waiting... {attempt}s", end="\r")
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
    
    # Save and download
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if title:
        safe_title = re.sub(r'[^a-zA-Z0-9\- ]', '', title).replace(' ', '_')
        if episode_info:
            output_name = f"{safe_title}_S{episode_info['season']:02d}E{episode_info['episode']:02d}.%(ext)s"
        else:
            output_name = f"{safe_title}_{timestamp}.%(ext)s"
    else:
        output_name = f"video_{timestamp}.%(ext)s"
    
    try:
        result = subprocess.run([
            PYTHON_PATH,
            "-m", "yt_dlp",
            "--referer", "https://vidcore.io/",
            m3u8_url,
            "-o", output_name
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            print(f"✅ Downloaded: {output_name}")
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
        if retry_count < max_retries:
            print(f"\n🔄 Retry {retry_count + 1}/{max_retries}")
            time.sleep(3)
            return open_vidcore_and_download(url, title, episode_info, progress, retry_count + 1)
        return False

def download_series_auto(imdb_id, title, seasons):
    """Download all episodes of a series automatically"""
    
    print(f"\n📺 DOWNLOADING SERIES: {title}")
    print(f"📊 Detected {seasons} season(s)")
    
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
        progress.start_time = time.time()
    
    # Which seasons?
    if seasons > 1:
        season_input = input(f"\nEnter seasons to download (e.g., 1,2,3 or 'all'): ").strip()
        if season_input.lower() == 'all':
            season_list = list(range(1, seasons + 1))
        else:
            try:
                season_list = [int(s.strip()) for s in season_input.split(',')]
            except:
                print("⚠️ Invalid input, downloading all seasons")
                season_list = list(range(1, seasons + 1))
    else:
        season_list = [1]
    
    # Calculate total episodes
    total_episodes = 0
    episode_counts = {}
    for season in season_list:
        count = get_episode_count(imdb_id, season)
        episode_counts[season] = count
        total_episodes += count
    
    progress.total_episodes = total_episodes
    progress.season_episode_counts = episode_counts
    progress.save()
    
    print(f"\n📊 Total episodes to download: {total_episodes}")
    print("🚀 Starting automatic download...")
    
    # Download each episode
    for season in season_list:
        episode_count = episode_counts.get(season, 10)
        
        print(f"\n{'='*70}")
        print(f"📺 SEASON {season} ({episode_count} episodes)")
        print(f"{'='*70}")
        
        for episode in range(1, episode_count + 1):
            # Check if already downloaded
            if progress.downloaded_episodes >= progress.total_episodes:
                print("\n✅ All episodes downloaded!")
                progress.completed = True
                progress.save()
                return
            
            # Check if this episode was downloaded
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
    
    # Search IMDb - returns the full result object
    result = search_imdb(movie_name)
    if not result:
        print("\n❌ Could not find movie/TV show")
        return
    
    # Extract info from the API result
    imdb_id = result.get('id', '')
    title = result.get('l', imdb_id)
    year = result.get('y', '')
    
    # CRITICAL: Use the API's type detection!
    is_series = result.get('q', '') == 'TV series' or result.get('q', '') == 'TVSeries'
    
    print(f"\n✅ IMDb ID: {imdb_id}")
    print(f"📽️ Title: {title} ({year})")
    print(f"📺 Type: {'TV Series' if is_series else 'Movie'}")
    
    # Get seasons count if it's a series
    seasons = 0
    if is_series:
        # Try to get seasons from the API result
        if 'numberOfSeasons' in result:
            seasons = int(result['numberOfSeasons'])
        else:
            # If not available, use manual scraping
            _, _, seasons = get_movie_info_manual(imdb_id)
    
    if is_series:
        print("\n📺 This is a TV Series")
        if seasons > 0:
            print(f"📊 Seasons: {seasons}")
        
        download_all = input("\nDownload entire series? (y/n): ").lower() == 'y'
        
        if download_all:
            download_series_auto(imdb_id, title, seasons)
        else:
            season = input("Enter season number (1): ") or "1"
            episode = input("Enter episode number (1): ") or "1"
            try:
                season = int(season)
                episode = int(episode)
            except:
                season, episode = 1, 1
            
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
            # Use the API result for the IMDb ID
            result = search_imdb(arg)
            if result:
                imdb_id = result.get('id', arg)
                title = result.get('l', imdb_id)
                is_series = result.get('q', '') == 'TV series' or result.get('q', '') == 'TVSeries'
                
                if is_series:
                    print(f"\n📺 TV Series: {title}")
                    _, _, seasons = get_movie_info_manual(imdb_id)
                    download_all = input("\nDownload entire series? (y/n): ").lower() == 'y'
                    if download_all:
                        download_series_auto(imdb_id, title, seasons)
                    else:
                        season = int(input("Enter season number (1): ") or "1")
                        episode = int(input("Enter episode number (1): ") or "1")
                        url = construct_vidcore_url(imdb_id, True, season, episode)
                        episode_info = {'season': season, 'episode': episode}
                        open_vidcore_and_download(url, title, episode_info)
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
                download_series_auto(progress.imdb_id, progress.title, len(progress.season_episode_counts))
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
                # Use the API result
                result = search_imdb(imdb_id)
                if result:
                    title = result.get('l', imdb_id)
                    is_series = result.get('q', '') == 'TV series' or result.get('q', '') == 'TVSeries'
                    
                    if is_series:
                        print(f"\n📺 This is a TV Series: {title}")
                        _, _, seasons = get_movie_info_manual(imdb_id)
                        download_all = input("\nDownload entire series? (y/n): ").lower() == 'y'
                        if download_all:
                            download_series_auto(imdb_id, title, seasons)
                        else:
                            season = int(input("Enter season number (1): ") or "1")
                            episode = int(input("Enter episode number (1): ") or "1")
                            url = construct_vidcore_url(imdb_id, True, season, episode)
                            episode_info = {'season': season, 'episode': episode}
                            open_vidcore_and_download(url, title, episode_info)
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