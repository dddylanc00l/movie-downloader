import subprocess
import time
import pyperclip
import os
import sys
import re
from datetime import datetime

# Your Python path
PYTHON_PATH = r"C:\Users\dylan\AppData\Local\Python\bin\python.exe"

def open_vidcore_and_download(url):
    """Open VidCore, auto-copy URL, auto-download, auto-close Chrome"""
    
    print("="*70)
    print("🎬 VIDCORE AUTO-DOWNLOADER (SILENT MODE)")
    print("="*70)
    
    if not url or 'vidcore.io' not in url:
        print("❌ Invalid URL!")
        print("Usage: python fetch.py 'https://vidcore.io/tv/tt9140554/1/2'")
        return
    
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
        return
    
    print(f"\n📋 URL: {m3u8_url[:100]}...")
    
    # Save URL
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"m3u8_url_{timestamp}.txt", "w") as f:
        f.write(m3u8_url)
    print(f"💾 Saved to: m3u8_url_{timestamp}.txt")
    
    # Auto-download with yt-dlp
    print("\n📥 Auto-downloading video with yt-dlp...")
    try:
        result = subprocess.run([
            PYTHON_PATH,
            "-m", "yt_dlp",
            "-v",
            "--referer", "https://vidcore.io/",
            m3u8_url
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            print("✅ Download complete!")
        else:
            print(f"❌ Download failed")
            print(f"   Error: {result.stderr[:200] if result.stderr else 'Unknown'}")
    except Exception as e:
        print(f"❌ Error: {e}")
    
    print("\n" + "="*70)
    print("✅ All done!")
    print(f"📁 Files saved in: {os.getcwd()}")
    print("="*70)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("Enter VidCore URL: ").strip()
    
    try:
        import pyperclip
    except ImportError:
        subprocess.run([PYTHON_PATH, "-m", "pip", "install", "pyperclip"])
        import pyperclip
    
    open_vidcore_and_download(url)