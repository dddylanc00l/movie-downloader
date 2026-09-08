import subprocess
import time
import pyperclip
import os
import sys
import re
import requests
from datetime import datetime
from urllib.parse import quote

# Your Python path
PYTHON_PATH = r"C:\Users\dylan\AppData\Local\Python\bin\python.exe"

def search_imdb(query):
    """Search IMDb for a movie/TV show and return the IMDb ID"""
    print(f"\n🔍 Searching IMDb for: {query}")
    
    # Search IMDb
    search_url = f"https://www.imdb.com/find?q={quote(query)}&ref_=nv_sr_sm"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests.get(search_url, headers=headers)
        
        if response.status_code != 200:
            print(f"❌ Failed to search IMDb: {response.status_code}")
            return None
        
        # Look for IMDb ID pattern in the response
        imdb_ids = re.findall(r'/title/(tt\d+)/', response.text)
        
        if not imdb_ids:
            print("❌ No IMDb results found")
            return None
        
        # Get unique IDs
        imdb_ids = list(set(imdb_ids))
        
        print(f"✅ Found {len(imdb_ids)} results:")
        for i, imdb_id in enumerate(imdb_ids[:5], 1):
            print(f"   {i}. {imdb_id}")
        
        if len(imdb_ids) == 1:
            return imdb_ids[0]
        else:
            choice = input(f"\nSelect number (1-{min(5, len(imdb_ids))}): ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(imdb_ids):
                    return imdb_ids[idx]
            except:
                pass
            return imdb_ids[0]
            
    except Exception as e:
        print(f"❌ Error searching IMDb: {e}")
        return None

def get_movie_info(imdb_id):
    """Get movie/TV show info from IMDb"""
    try:
        url = f"https://www.imdb.com/title/{imdb_id}/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            return None, None
        
        # Check if it's a TV series
        is_series = 'TV Series' in response.text or 'TV Series' in response.text
        
        # Try to get title
        title_match = re.search(r'<title>(.*?)</title>', response.text)
        title = title_match.group(1).strip() if title_match else imdb_id
        title = title.replace(' - IMDb', '').strip()
        
        return title, is_series
        
    except Exception as e:
        print(f"❌ Error getting movie info: {e}")
        return None, None

def construct_vidcore_url(imdb_id, is_series=False, season=1, episode=1):
    """Construct the VidCore URL based on IMDb ID and type"""
    if is_series:
        # For TV series: https://vidcore.io/tv/{imdb_id}/{season}/{episode}
        # Note: VidCore uses the IMDb ID directly in the URL
        return f"https://vidcore.io/tv/{imdb_id}/{season}/{episode}"
    else:
        # For movies: https://vidcore.io/movie/{imdb_id}
        return f"https://vidcore.io/movie/{imdb_id}"

def open_vidcore_and_download(url, title=None):
    """Open VidCore and download the video"""
    
    print("\n" + "="*70)
    if title:
        print(f"🎬 DOWNLOADING: {title}")
    print("="*70)
    
    print(f"\n📄 Target URL: {url}")
    
    # Close existing Chrome
    print("\n📌 Closing existing Chrome windows...")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
        print("✅ Chrome closed")
        time.sleep(2)
    except:
        pass
    
    # Open Chrome with URL
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    user_data = r"C:\Users\dylan\AppData\Local\Google\Chrome\User Data"
    
    print("\n🚀 Opening Chrome...")
    chrome_process = subprocess.Popen([
        chrome_path,
        f"--user-data-dir={user_data}",
        "--disable-blink-features=AutomationControlled",
        url
    ])
    
    print("✅ Chrome opened!")
    print("\n⏳ Waiting for Tampermonkey to copy the URL...")
    print("   (Click Play on the video when it loads)")
    print("="*70)
    
    m3u8_url = None
    last_clipboard = ""
    max_wait = 120
    
    for attempt in range(max_wait):
        try:
            current = pyperclip.paste()
            
            if current != last_clipboard and '.m3u8' in current and current.startswith('http'):
                print(f"\n✅ Auto-detected m3u8 URL!")
                m3u8_url = current
                break
            
            last_clipboard = current
            
            if attempt % 5 == 0:
                print(f"   Waiting... {attempt+1}/{max_wait} seconds", end="\r")
            
            time.sleep(1)
            
        except Exception as e:
            print(f"   ⚠️ Error: {e}")
            time.sleep(1)
    
    # Auto-close Chrome
    print("\n🔒 Closing Chrome...")
    try:
        chrome_process.terminate()
        time.sleep(1)
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
        print("✅ Chrome closed")
    except:
        pass
    
    if not m3u8_url:
        print("\n❌ No m3u8 URL detected")
        return False
    
    # Save URL with title if available
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"m3u8_url_{timestamp}.txt"
    if title:
        # Sanitize title for filename
        safe_title = re.sub(r'[^a-zA-Z0-9\- ]', '', title)
        safe_title = safe_title.replace(' ', '_')
        filename = f"m3u8_url_{safe_title}_{timestamp}.txt"
    
    with open(filename, "w") as f:
        f.write(m3u8_url)
    print(f"\n💾 Saved to: {filename}")
    
    # Download with yt-dlp
    print("\n📥 Downloading video with yt-dlp...")
    try:
        output_name = f"video_{timestamp}.%(ext)s"
        if title:
            safe_title = re.sub(r'[^a-zA-Z0-9\- ]', '', title)
            safe_title = safe_title.replace(' ', '_')
            output_name = f"{safe_title}_{timestamp}.%(ext)s"
        
        result = subprocess.run([
            PYTHON_PATH,
            "-m", "yt_dlp",
            "-v",
            "--referer", "https://vidcore.io/",
            m3u8_url,
            "-o", output_name
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            print("✅ Download complete!")
            return True
        else:
            print(f"❌ Download failed")
            print(f"   Error: {result.stderr[:200] if result.stderr else 'Unknown'}")
            return False
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def download_by_name(movie_name):
    """Main function: search IMDb, get URL, download"""
    
    print("="*70)
    print("🎬 VIDCORE AUTO-DOWNLOADER (IMDb Lookup)")
    print("="*70)
    
    # Search IMDb
    imdb_id = search_imdb(movie_name)
    if not imdb_id:
        return
    
    print(f"\n✅ IMDb ID: {imdb_id}")
    
    # Get movie info
    title, is_series = get_movie_info(imdb_id)
    
    if title:
        print(f"📽️ Title: {title}")
        print(f"📺 Type: {'TV Series' if is_series else 'Movie'}")
    
    if is_series:
        print("\n📺 This is a TV Series")
        season = input("Enter season number (1): ") or "1"
        episode = input("Enter episode number (1): ") or "1"
        url = construct_vidcore_url(imdb_id, True, season, episode)
        
        # Ask if user wants to download entire series
        download_all = input("\nDownload entire series? (y/n): ").lower() == 'y'
        
        if download_all:
            # This would need a list of episodes - for now, just download season
            print("\n📺 Downloading entire season...")
            # You could implement season scraping here
            # For now, just download the first episode
            
    else:
        url = construct_vidcore_url(imdb_id, False)
    
    # Download
    success = open_vidcore_and_download(url, title)
    
    if success:
        print("\n" + "="*70)
        print(f"✅ Successfully downloaded: {title if title else imdb_id}")
        print("="*70)

def main():
    """Main entry point"""
    if len(sys.argv) > 1:
        # If argument is a URL, use it directly
        url_arg = sys.argv[1]
        if 'vidcore.io' in url_arg:
            print(f"📄 Using direct URL: {url_arg}")
            open_vidcore_and_download(url_arg)
            return
        else:
            # Treat as movie name
            movie_name = ' '.join(sys.argv[1:])
            download_by_name(movie_name)
            return
    else:
        # Interactive mode
        print("="*70)
        print("🎬 VIDCORE AUTO-DOWNLOADER")
        print("="*70)
        print("\nOptions:")
        print("  1. Download by movie/TV show name")
        print("  2. Download by VidCore URL")
        print("  3. Download by IMDb ID")
        
        choice = input("\nSelect option (1-3): ").strip()
        
        if choice == '1':
            movie_name = input("\nEnter movie or TV show name: ").strip()
            if movie_name:
                download_by_name(movie_name)
        elif choice == '2':
            url = input("\nEnter VidCore URL: ").strip()
            if url:
                open_vidcore_and_download(url)
        elif choice == '3':
            imdb_id = input("\nEnter IMDb ID (e.g., tt9140554): ").strip()
            if imdb_id.startswith('tt'):
                title, is_series = get_movie_info(imdb_id)
                url = construct_vidcore_url(imdb_id, is_series)
                open_vidcore_and_download(url, title)
            else:
                print("❌ Invalid IMDb ID")
        else:
            print("❌ Invalid option")

if __name__ == "__main__":
    try:
        import pyperclip
    except ImportError:
        subprocess.run([PYTHON_PATH, "-m", "pip", "install", "pyperclip"])
        import pyperclip
    
    main()