#!/usr/bin/python3
import os
import json
import re
import sys
import time
import platform
import subprocess
import threading
import requests
from collections import deque
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
import warnings
warnings.filterwarnings('ignore')
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== AYARLAR ====================
if os.path.exists("/storage/emulated/0/") and platform.system() != 'Windows':
    BASE_PATH = "/storage/emulated/0/IPTV"
else:
    BASE_PATH = r"C:\Users\KEMAL\Desktop\IPTV"

TV_ORDER_FILE = os.path.join(BASE_PATH, "Dizin.txt")
CLOUD_FILE = os.path.join(BASE_PATH, "Cloud.txt")  # Artık IPTV+CLOUD+RADIO hepsi bu dosyada, #TYPE: etiketleriyle ayrılıyor
OUTPUT_FILE = os.path.join(BASE_PATH, "TV.m3u")
YEDEK_FILE = os.path.join(BASE_PATH, "Yedek.m3u")
CACHE_FILE = os.path.join(BASE_PATH, "cache.json")
FALLBACK_URL = "https://github.com/SekoBes/cloudly/raw/refs/heads/main/NO_SIGNAL.mp4"

HLS_WORKERS = 10
YT_WORKERS = 10

print(f"📁 Platform: {platform.system()}")
print(f"📁 BASE_PATH: {BASE_PATH}")

# ==================== ENV ====================
# .env dosyasını (varsa) okuyup ortam değişkenlerine ekler. Gerçek env değişkenleri önceliklidir.
def load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_dotenv(os.path.join(BASE_PATH, "env"))

def require_env(name):
    val = os.environ.get(name)
    if not val:
        print(f"⚠ Ortam değişkeni eksik: {name}")
        sys.exit(1)
    return val

config = {}
config["cloudflare"] = {
    "account_id": require_env("CF_ACCOUNT_ID"),
    "api_token": require_env("CF_API_TOKEN"),
    "worker_name": require_env("CF_WORKER_NAME"),
    "subdomain": os.environ.get("CF_SUBDOMAIN"),
    "worker_url": require_env("CF_WORKER_URL"),
    "buff_worker_name": os.environ.get("CF_BUFF_WORKER_NAME"),
    "buff_worker_url": os.environ.get("CF_BUFF_WORKER_URL"),
}
config["pythonanywhere"] = {
    "username": os.environ.get("PA_USERNAME"),
    "token": os.environ.get("PA_TOKEN"),
    "api_token": os.environ.get("PA_API_TOKEN"),
}
config["paths"] = {
    "local_logos": os.environ.get("LOCAL_LOGOS"),
    "remote_logos": os.environ.get("REMOTE_LOGOS"),
    "local_tvlogos": os.environ.get("LOCAL_TVLOGOS"),
    "remote_tvlogos": os.environ.get("REMOTE_TVLOGOS"),
    "remote_cloud": os.environ.get("REMOTE_CLOUD"),
}
config["github"] = {
    "token": os.environ.get("GITHUB_TOKEN"),
    "branch": os.environ.get("GITHUB_BRANCH"),
}
config["telegram"] = {
    "TOKEN": require_env("TELEGRAM_TOKEN"),
    "CHAT_ID": require_env("TELEGRAM_CHAT_ID"),
}

TOKEN = config["telegram"]["TOKEN"]
CHAT_ID = config["telegram"]["CHAT_ID"]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=1)
SESSION.mount('http://', adapter)
SESSION.mount('https://', adapter)

# ==================== CACHE ====================
def write_text_atomic(path, text):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp_path, path)

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        now = time.time()
        return {k:v for k,v in data.items() if v.get('exp',0) > now}
    except Exception:
        return {}

def save_cache(cache):
    try:
        write_text_atomic(CACHE_FILE, json.dumps(cache, indent=2))
    except Exception:
        pass

def telegram_mesaj_gonder(mesaj):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=8)
    except Exception as e:
        print(f"❌ Telegram hatası: {e}")

# ==================== DOSYA OKUMA ====================
VALID_TYPES = {'IPTV': 'iptv', 'CLOUD': 'cloud', 'RADIO': 'radio'}

def read_all_channels_file(file_path, default_type='cloud', show_result=True):
    groups = {'iptv': {}, 'cloud': {}, 'radio': {}}
    order = []
    if not os.path.exists(file_path):
        print(f"⚠ Dosya bulunamadi: {file_path}")
        return groups['iptv'], groups['cloud'], groups['radio'], order
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception as e:
        print(f"⚠ Dosya okuma hatasi {file_path}: {e}")
        return groups['iptv'], groups['cloud'], groups['radio'], order
    if content.startswith(chr(65279)):
        content = content.lstrip(chr(65279))
    raw_lines = content.splitlines()
    current_type = default_type.lower()
    valid_map = {'IPTV':'iptv','CLOUD':'cloud','RADIO':'radio','iptv':'iptv','cloud':'cloud','radio':'radio'}
    i=0
    while i < len(raw_lines):
        line = raw_lines[i].strip()
        if not line:
            i+=1
            continue
        if line.upper().startswith('#TYPE:'):
            tag = line.split(':',1)[1].strip().upper()
            if tag in VALID_TYPES:
                current_type = VALID_TYPES[tag]
            elif tag.lower() in valid_map:
                current_type = valid_map[tag.lower()]
            else:
                print(f"   ⚠ Bilinmeyen #TYPE etiketi: '{tag}' (satir {i+1})")
            i+=1
            continue
        if line.startswith('#EXTINF:'):
            extinf_line = line
            j=i+1
            url=""
            while j < len(raw_lines):
                nxt = raw_lines[j].strip()
                if nxt=="":
                    j+=1
                    continue
                if nxt.startswith('#'):
                    break
                url=nxt
                break
            if url:
                name = extinf_line.split(',',1)[1].strip() if ',' in extinf_line else extinf_line
                is_youtube = (current_type == 'cloud')
                groups[current_type].setdefault(name, []).append((extinf_line, url, is_youtube, current_type))
                order.append(name)
            i = j+1 if url else i+1
            continue
        i+=1
    if show_result:
        label_map = {'iptv': 'TV Kanalları', 'cloud': 'Cloud Kanalları', 'radio': 'Radyo Kanalları'}
        # Etiket yerine link türüne göre say: hazır yayın linki (.m3u8 vb.) TV, web sayfası Cloud
        _hazir = is_direct_url
        cloud_entries = [e for v in groups['cloud'].values() for e in v]
        sayilar = {
            'iptv': sum(len(v) for v in groups['iptv'].values()) + sum(1 for e in cloud_entries if _hazir(e[1])),
            'cloud': sum(1 for e in cloud_entries if not _hazir(e[1])),
            'radio': sum(len(v) for v in groups['radio'].values()),
        }
        for t in ('iptv','cloud','radio'):
            print(f"   📖 {label_map.get(t, t.upper())} : {sayilar[t]} Kanal")
    return groups['iptv'], groups['cloud'], groups['radio'], order
    if not os.path.exists(file_path):
        print(f"⚠ Dizin.txt bulunamadı")
        return []
    with open(file_path, 'r', encoding='utf-8') as f:
        order = [s for line in f if (s := line.strip()) and not s.startswith('#')]
    print(f"   📋 Dizin.txt: {len(order)} Kanal Sırası")
    return order

# ==================== YOUTUBE ====================
def get_youtube_stream(url, max_retries=2):
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                time.sleep(1.5)
            cmd = ['yt-dlp','--quiet','--user-agent',user_agent,'--format','best[ext=mp4]/best','--no-playlist','--force-ipv4','--extractor-args','youtube:player_client=android,ios;skip=dash','--print','%(url)s|||%(height)s',url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if result.returncode == 0 and result.stdout.strip():
                output = result.stdout.strip()
                if "|||" in output:
                    stream_url, height = output.split("|||", 1)
                    return stream_url.strip(), f"{height.strip()}p"
        except:
            continue
    return None, None
    
# ==================== TMG GRUBU ====================
TMG_MAP = {
    "a2tv": ("https://trkvz.daioncdn.net/a2tv/a2tv.m3u8?ce=3&app=59363a60-be96-4f73-9eff-355d0ff2c758", "https://www.atv.com.tr/a2tv/canli-yayin/"),
    "atv": ("https://trkvz.daioncdn.net/atv/atv.m3u8?ce=3&app=d5eb593f-39d9-4b01-9cfd-4748e8332cf0", "https://www.atv.com.tr/"),
    "ahaber": ("https://trkvz.daioncdn.net/ahaber/ahaber.m3u8?ce=2&app=9aedd495-cc54-4ad3-a01d-9573163f078a", "https://www.ahaber.com.tr/"),    
    "atvavrupa": ("https://trkvz-live.ercdn.net/atvavrupa/atvavrupa.m3u8", "https://www.atvavrupa.tv/"),
    "minikacocuk": ("https://trkvz.daioncdn.net/minikago_cocuk/minikago_cocuk.m3u8?app=mweb&ce=3", "https://www.minikacocuk.com.tr/"),
    "minikago": ("https://trkvz.daioncdn.net/minikago/minikago.m3u8?app=mweb&ce=3", "https://www.minikago.com.tr/"),
    "aspor": ("https://trkvz.daioncdn.net/aspor/aspor.m3u8?ce=3&&app=45f847c4-04e8-419a-a561-2ebf87084765", "https://www.aspor.com.tr/"),
}

def get_best_hls_variant(master_url, referer=None):
    """Master playlistten gerçek varyant URL'sini, query parametreleri korunarak seçer."""
    try:
        headers = {"Referer": referer} if referer else {}
        response = SESSION.get(
            master_url,
            headers=headers,
            timeout=8,
            verify=False
        )
        if response.status_code != 200:
            return master_url

        lines = [line.strip() for line in response.text.splitlines() if line.strip()]
        variants = []
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF:") or i + 1 >= len(lines):
                continue
            variant_line = lines[i + 1]
            if variant_line.startswith("#"):
                continue

            resolution_match = re.search(r"RESOLUTION=\d+x(\d+)", line)
            bandwidth_match = re.search(r"(?:AVERAGE-BANDWIDTH|BANDWIDTH)=(\d+)", line)
            height = int(resolution_match.group(1)) if resolution_match else 0
            bandwidth = int(bandwidth_match.group(1)) if bandwidth_match else 0
            variant_url = urljoin(master_url, variant_line)

            if "?" not in variant_line and "?" in master_url:
                master_query = master_url.split("?", 1)[1]
                variant_url += ("&" if "?" in variant_url else "?") + master_query

            variants.append((height, bandwidth, variant_url))

        if variants:
            return max(variants, key=lambda item: (item[0], item[1]))[2]
    except Exception:
        pass
    return master_url

def get_tmg_stream(hls_url, referer, max_retries=2):
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                time.sleep(0.5)
            response = SESSION.get(
                "https://securevideotoken.tmgrup.com.tr/webtv/secure",
                params={"url": hls_url},
                headers={"Referer": referer},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("Success") and data.get("Url"):
                    tokenli_url = data.get("Url")
                    if "_1080p" not in tokenli_url and "1080p" not in tokenli_url:
                        selected_url = get_best_hls_variant(tokenli_url, referer)
                        if selected_url != tokenli_url:
                            tokenli_url = selected_url
                            quality = "1080p" if "1080p" in tokenli_url else "HD"
                        else:
                            quality = "HD"
                    else:
                        quality = "1080p" if "1080" in tokenli_url else "HD"
                    return tokenli_url, quality
        except:
            continue
    return None, None

# ==================== DOĞUŞ GRUBU ====================
def get_eurostar_stream(url, max_retries=2):
    headers = {"User-Agent": "Mozilla/5.0","Referer": "https://www.eurostartv.com.tr/"}
    for attempt in range(max_retries):
        try:
            if attempt > 0: time.sleep(1)
            response = SESSION.get("https://www.eurostartv.com.tr/canli-izle", headers=headers, timeout=10, verify=False)
            if response.status_code == 200:
                for pattern in [r"(https?://[^\s\"']+1080p[^\s\"']*\.m3u8[^\s\"']*)",r"liveUrl\s*=\s*['\"]([^'\"]+)['\"]",r"file:\s*['\"]([^'\"]+\.m3u8)['\"]"]:
                    match = re.search(pattern, response.text)
                    if match:
                        stream_url = match.group(1)
                        if stream_url.startswith('//'): stream_url = 'https:' + stream_url
                        if '.m3u8' not in stream_url.lower():
                            try:
                                rr = SESSION.get(stream_url, headers=headers, timeout=10, verify=False, allow_redirects=True)
                                if rr.status_code == 200 and '.m3u8' in rr.url.lower():
                                    stream_url = rr.url
                                else:
                                    mm = re.search(r"(https?://[^\s\"']+\.m3u8[^\s\"']*)", rr.text)
                                    if mm: stream_url = mm.group(1)
                            except Exception:
                                pass
                        return stream_url, "HD"
        except: continue
    return None, None

# ==================== SHOW GRUBU ====================
SHOW_MAP = {
    "showtv": ("https://www.showtv.com.tr/canli-yayin", "showtv", "daioncdn"),
    "showturk": ("https://www.showturk.com.tr/canli-yayin", "showturk", "ercdn"),
    "showmax": ("https://www.showmax.com.tr/canliyayin", "showmax", "ercdn"),
}

def get_show_stream(base_url, channel_name, cdn_type="ercdn", max_retries=2):
    headers = {"Referer": base_url}
    for attempt in range(max_retries):
        try:
            if attempt > 0: time.sleep(0.5)
            resp = SESSION.get(base_url, headers=headers, timeout=8, verify=False)
            if resp.status_code == 200:
                html = resp.text
                video_url_match = re.search(r'var\s+videoUrl\s*=\s*"([^"]+)"', html)
                if video_url_match:
                    stream_url = video_url_match.group(1).replace("\\/", "/").replace("&amp;", "&")
                    stream_url = get_best_hls_variant(stream_url, base_url)
                    return stream_url, "1080p" if "1080p" in stream_url else "HD"
                src_match = re.search(r'src\s*:\s*\{\s*hls\s*:\s*"([^"]+)"', html)
                if src_match:
                    stream_url = src_match.group(1).replace("\\/", "/").replace("&amp;", "&")
                    stream_url = get_best_hls_variant(stream_url, base_url)
                    return stream_url, "1080p" if "1080p" in stream_url else "HD"
                token_match = re.search(r'e=([^&\s"\']+)&st=([^&\s"\']+)', html)
                if token_match:
                    base_cdn = "ciner.daioncdn.net" if cdn_type == "daioncdn" else "ciner-live.ercdn.net"
                    return f"https://{base_cdn}/{channel_name}/{channel_name}_1080p.m3u8?e={token_match.group(1)}&st={token_match.group(2)}&tv=1", "1080p"
        except: continue
    return None, None

# ==================== KANAL D ====================
def get_kanald_stream(url, max_retries=2):
    headers = {"Referer": "https://www.kanald.com.tr/canli-yayin"}
    for attempt in range(max_retries):
        try:
            if attempt > 0: time.sleep(0.5)
            response = SESSION.get("https://www.kanald.com.tr/canli-yayin", headers=headers, timeout=8, verify=False)
            if response.status_code != 200: continue
            data_url_match = re.search(r'data-url="([^"]+\.m3u8[^"]*)"', response.text)
            if data_url_match:
                base_url = data_url_match.group(1).replace('&amp;', '&')
                if 'kanald.m3u8' in base_url:
                    stream_url = get_best_hls_variant(base_url, headers["Referer"])
                    return stream_url, "1080p" if "1080p" in stream_url else "HD"
                else:
                    return base_url, "HD"
        except: continue
    return None, None

# ==================== TV8 ====================
def get_tv8_stream(url, max_retries=2):
    headers = {"User-Agent":"Mozilla/5.0","Referer":"https://www.tv8.com.tr/canli-yayin"}
    for attempt in range(max_retries):
        try:
            if attempt > 0: time.sleep(0.5)
            response = SESSION.get("https://www.tv8.com.tr/canli-yayin", headers=headers, timeout=12, verify=False)
            if response.status_code != 200: continue
            html = response.text
            match = re.search(r'(https://tv8\.daioncdn\.net/tv8/tv8_1080p\.m3u8\?[^"\']+)', html)
            if match:
                return match.group(1).replace('&amp;', '&'), "1080p"
            fallback_match = re.search(r'var\s+videoUrl\s*=\s*"([^"]+)"', html)
            if fallback_match:
                base_url = fallback_match.group(1).replace('&amp;', '&')
                if 'tv8.m3u8' in base_url:
                    stream_url = get_best_hls_variant(base_url, headers["Referer"])
                    return stream_url, "1080p" if "1080p" in stream_url else "HD"
                else:
                    return base_url, "HD"
        except: continue
    return None, None   
    
# ==================== NOW TV ====================  
def get_nowtv_stream(url, max_retries=2):
    headers = {"User-Agent":"Mozilla/5.0","Referer":"https://www.nowtv.com.tr/"}
    for attempt in range(max_retries):
        try:
            if attempt > 0: time.sleep(0.5)
            response = SESSION.get("https://www.nowtv.com.tr/canli-yayin", headers=headers, timeout=15)
            if response.status_code == 200:
                for pattern in [r'daionUrl\s*:\s*["\']([^"\']+)["\']',r'daiUrl\s*:\s*["\']([^"\']+)["\']']:
                    match = re.search(pattern, response.text)
                    if match:
                        stream_url = get_best_hls_variant(match.group(1), headers["Referer"])
                        return stream_url, "1080p" if "1080p" in stream_url else "HD"
        except: continue
    return None, None

# ==================== CANLITV.DIY ====================
CANLITV_MAP = {
    "cartoon-network": (11233, "cartoon-network", "yayin2.canlitv.fun/live/cartoon-network.stream/playlist.m3u8"),
    "tivibu-spor": (12843, "tivibu-spor", "yayin2.canlitv.fun/live/tivibuspor.stream/playlist.m3u8"),
    "yaban-tv": (12044, "yaban-tv", "yayin1.canlitv.fun/canlitv/yabantv.stream/playlist.m3u8"),
}

def get_canlitv_generic(slug, max_retries=2):
    if slug not in CANLITV_MAP: return None, None
    pid, _, base_path = CANLITV_MAP[slug]
    headers = {"Referer": f"https://www.canlitv.diy/{slug}"}
    for attempt in range(max_retries):
        try:
            resp = SESSION.get(f"https://www.canlitv.diy/player/index.php?id={pid}", headers=headers, timeout=15)
            if resp.status_code == 200:
                hash_match = re.search(r'[?&]hash=([a-f0-9]{32})', resp.text) or re.search(r'"hash":"([a-f0-9]{32})"', resp.text)
                if hash_match:
                    return f"https://{base_path}?hash={hash_match.group(1)}", "HD"
        except: pass
        time.sleep(0.5)
    return None, None

# ==================== TELEVIZO GRUBU ====================
def get_televizo_stream(url, max_retries=2):
    """televizo.live → cdntvmedia (2 kat) → playerjs → m3u8 zinciri"""
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    origin_televizo = "https://televizo.live"
    origin_cdntv    = "https://cdntvmedia.com"

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                time.sleep(1)

            r1 = SESSION.get(url, headers={
                "User-Agent": ua,
                "Referer": origin_televizo + "/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            }, timeout=15, verify=False)
            if r1.status_code != 200:
                continue

            m1 = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', r1.text, re.IGNORECASE)
            if not m1:
                continue
            iframe1 = m1.group(1)
            if iframe1.startswith("//"):   iframe1 = "https:" + iframe1
            elif iframe1.startswith("/"):  iframe1 = origin_televizo + iframe1

            r2 = SESSION.get(iframe1, headers={
                "User-Agent": ua,
                "Referer": url,
                "Origin":  origin_televizo,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
            }, timeout=15, verify=False)
            if r2.status_code != 200:
                continue

            m2 = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', r2.text, re.IGNORECASE)
            if not m2:
                continue
            iframe2 = m2.group(1)
            if iframe2.startswith("//"):   iframe2 = "https:" + iframe2
            elif iframe2.startswith("/"):  iframe2 = origin_cdntv + iframe2

            r3 = SESSION.get(iframe2, headers={
                "User-Agent": ua,
                "Referer": iframe1,
                "Origin":  origin_cdntv,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
            }, timeout=15, verify=False)
            if r3.status_code != 200:
                continue

            gen = re.search(r'generatedFile\s*=\s*["\']([^"\']+?)["\']', r3.text)
            if not gen:
                continue

            raw = gen.group(1)
            urls = [u.strip().replace("\\/", "/") for u in raw.split(" or ")]
            m3u8_url = urls[0] if urls else None
            if not m3u8_url:
                continue

            quality = "1080p" if "1080" in m3u8_url else ("HD" if "hd" in m3u8_url.lower() else "SD")
            return m3u8_url, quality

        except Exception as e:
            print(f"   ↻ televizo deneme {attempt+1} hatası: {e}")
            continue

    return None, None    
# ==================== STREAM ROUTER - DÜZ SATIR ====================
def get_stream_url(url, max_retries=2):
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower: return get_youtube_stream(url, max_retries)
    if "a2tv" in url_lower: return get_tmg_stream(*TMG_MAP["a2tv"], max_retries)
    elif "atv.com.tr" in url_lower: return get_tmg_stream(*TMG_MAP["atv"], max_retries)
    elif "ahaber.com.tr" in url_lower: return get_tmg_stream(*TMG_MAP["ahaber"], max_retries)    
    elif "atvavrupa" in url_lower: return get_tmg_stream(*TMG_MAP["atvavrupa"], max_retries)
    elif "minikacocuk.com.tr" in url_lower: return get_tmg_stream(*TMG_MAP["minikacocuk"], max_retries)
    elif "minikago.com.tr" in url_lower: return get_tmg_stream(*TMG_MAP["minikago"], max_retries)
    elif "aspor.com.tr" in url_lower: return get_tmg_stream(*TMG_MAP["aspor"], max_retries) 
    elif "eurostartv.com.tr" in url_lower: return get_eurostar_stream(url, max_retries)
    elif "showtv.com.tr" in url_lower or "showtv" in url_lower: return get_show_stream(*SHOW_MAP["showtv"], max_retries)
    elif "showturk.com.tr" in url_lower or "show-turk" in url_lower: return get_show_stream(*SHOW_MAP["showturk"], max_retries)
    elif "showmax.com.tr" in url_lower or "show-max" in url_lower: return get_show_stream(*SHOW_MAP["showmax"], max_retries)
    elif "kanald.com.tr" in url_lower or "kanald" in url_lower: return get_kanald_stream(url, max_retries)
    elif "tv8.com.tr" in url_lower or "tv8" in url_lower: return get_tv8_stream(url, max_retries)
    elif "nowtv.com.tr" in url_lower or "now" in url_lower: return get_nowtv_stream(url, max_retries)
    elif "canlitv.diy" in url_lower and "cartoon-network" in url_lower: return get_canlitv_generic("cartoon-network", max_retries)
    elif "canlitv.diy" in url_lower and "tivibu-spor" in url_lower: return get_canlitv_generic("tivibu-spor", max_retries)
    elif "canlitv.diy" in url_lower and "yaban-tv" in url_lower: return get_canlitv_generic("yaban-tv", max_retries)
    elif "televizo.live" in url_lower: return get_televizo_stream(url, max_retries)
    elif "dsmartgo.com.tr" in url_lower: return get_dsmartgo_stream(url, max_retries)    
    else: return None, None

# ==================== IPTV WEB SAYFASI KONTROLÜ ====================
MEDIA_EXT = ('.m3u8', '.m3u', '.mpd', '.ts', '.mp3', '.aac', '.mp4', '.flv', '.ogg')

def is_direct_url(url):
    """Tek kural (okuma, çözümleme ve istatistik hepsi bunu kullanır): link hazır bir yayın mı?
    Medya uzantısı, içinde m3u8 geçmesi veya ham IP adresi (web sitesi IP ile açılmaz) = hazır yayın."""
    l = url.lower()
    if l.split('?', 1)[0].split('#', 1)[0].endswith(MEDIA_EXT) or 'm3u8' in l: return True
    return re.match(r'^https?://\d{1,3}(\.\d{1,3}){3}(:\d+)?(/|$)', l) is not None

def is_direct_stream(url):
    """Cloud etiketli ama aslında hazır yayın linki olan (etiket unutulmuş) kanalı tespit eder.
    Adres medya uzantılıysa ya da sunucu HTML DIŞI bir cevap veriyorsa True.
    Ulaşılamazsa False (web sayfası yanlışlıkla playlist'e girmesin)."""
    if url.lower().split('?', 1)[0].split('#', 1)[0].endswith(MEDIA_EXT): return True
    try:
        r = SESSION.get(url, timeout=8, stream=True, verify=False)
        ok = r.status_code < 400 and 'text/html' not in r.headers.get('Content-Type', '').lower()
        r.close()
        return ok
    except Exception:
        return False

def resolve_cloud_url(url, max_retries=2):
    """Önce resolver ile çöz. Çözülemezse ve link zaten hazır bir yayınsa olduğu gibi kullan
    (resolution='IPTV' işareti). Web sayfasıysa (None, None) -> fallback."""
    # Adres zaten hazır bir yayınsa (.m3u8 vb.) resolver'a sokma: linkte 'tv8', 'a2tv' gibi
    # kelimeler geçse bile yanlış kanala yönlenmesin. Ağ isteği de gerekmez.
    if is_direct_url(url):
        return url, "IPTV"
    stream_url, resolution = get_stream_url(url, max_retries)
    if stream_url: return stream_url, resolution
    if is_direct_stream(url): return url, "IPTV"
    return None, None

def is_web_page(url):
    """Net kural: sunucu HTML döndürüyorsa bu bir yayın değil, web sayfasıdır.
    Ad/anahtar kelime tahmini yok, listeye gerek yok. Ulaşılamazsa dokunmaz."""
    # Sadece adresin KENDİSİ medya uzantısıyla bitiyorsa atla (sorgu kısmına bakılmaz)
    if url.lower().split('?', 1)[0].split('#', 1)[0].endswith(MEDIA_EXT):
        return False
    try:
        r = SESSION.get(url, timeout=8, stream=True, verify=False)
        ctype = r.headers.get('Content-Type', '').lower()
        r.close()
        return 'text/html' in ctype
    except Exception:
        return False

# ==================== CLOUD İŞLEME ====================

def build_bar(progress, processed, total_cloud, bar_width=50):
    percent_text = f" %{progress:>5.1f} "
    start_pos = (bar_width // 2) - (len(percent_text) // 2)
    filled_chars = int(bar_width * processed / total_cloud) if total_cloud else 0
    bar = ""
    for i in range(bar_width):
        char = percent_text[i - start_pos] if start_pos <= i < start_pos + len(percent_text) else " "
        bar += f"\033[42;30m{char}\033[0m" if i < filled_chars else f"\033[47;30m{char}\033[0m"
    return bar

def _process_cloud_channels(cloud_channels_dict, tv_order_set):
    cloud_results = {}
    tasks = []
    cache = load_cache()

    filtered = {k:v for k,v in cloud_channels_dict.items() if k in tv_order_set} if tv_order_set else cloud_channels_dict

    for name, entries in filtered.items():
        for extinf_line, url, is_youtube, channel_type in entries:
            if url in cache:
                cloud_results.setdefault(name, []).append((len(tasks), (extinf_line, cache[url]['url'], True, channel_type)))
                tasks.append({'seq': len(tasks), 'name': name, 'url': url, 'extinf': extinf_line, 'channel_type': channel_type, 'cached': True, 'cached_url': cache[url]['url']})
            else:
                tasks.append({'seq': len(tasks), 'name': name, 'url': url, 'extinf': extinf_line, 'channel_type': channel_type, 'cached': False})

    total_cloud = len(tasks)
    if total_cloud == 0:
        print("   ℹ İşlenecek Cloud kanalı yok")
        print("=" * 50)
        return cloud_results, []

    processed = 0
    success = 0
    fail = 0
    failed_channels = []
    bar_width = 50
    bar_display = ""

    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    yt_tasks = [t for t in tasks if "youtu" in t['url'].lower() and not t.get('cached')]
    hls_tasks = [t for t in tasks if "youtu" not in t['url'].lower() and not t.get('cached')]
    cached_tasks = [t for t in tasks if t.get('cached')]

    for task in cached_tasks:
        processed += 1
        name = task['name']
        progress = (processed / total_cloud) * 100
        success += 1
        bar_display = build_bar(progress, processed, total_cloud, bar_width)
        sys.stdout.write(f"\r🚀 {name[:20]:<20} | ✅ [Cache]   \033[K\n")
        sys.stdout.write(f"\r{bar_display}\033[K\n")
        sys.stdout.write("\033[2F")
        sys.stdout.flush()

    def run_all(hls_batch, yt_batch):
        nonlocal processed, success, fail, bar_display
        if not hls_batch and not yt_batch:
            return
        groups = {}
        for task in hls_batch + yt_batch:
            groups.setdefault(task['url'], []).append(task)
        with ThreadPoolExecutor(max_workers=HLS_WORKERS) as hls_pool, ThreadPoolExecutor(max_workers=YT_WORKERS) as yt_pool:
            future_to_url = {}
            for url_key in groups:
                pool = yt_pool if "youtu" in url_key.lower() else hls_pool
                future_to_url[pool.submit(resolve_cloud_url, url_key, 2)] = url_key
            for future in as_completed(future_to_url):
                for task in groups[future_to_url[future]]:
                    processed += 1
                    name = task['name']
                    progress = (processed / total_cloud) * 100
                    try:
                        stream_url, resolution = future.result()
                        if not stream_url:
                            fail += 1
                            failed_channels.append(name)
                            status_icon = "❌"
                            res_info = "[FALLBACK]"
                        else:
                            success += 1
                            if resolution == "IPTV":
                                # Etiket CLOUD ama link hazır yayın: IPTV gibi kullan, cache'leme
                                cloud_results.setdefault(name, []).append((task['seq'], (task['extinf'], stream_url, not is_direct_url(task['url']), 'iptv')))
                            else:
                                cloud_results.setdefault(name, []).append((task['seq'], (task['extinf'], stream_url, True, task['channel_type'])))
                                cache[task['url']] = {"url": stream_url, "exp": time.time() + 3000}
                            status_icon = "✅"
                            res_info = f"[{resolution}]"
                        bar_display = build_bar(progress, processed, total_cloud, bar_width)
                        sys.stdout.write(f"\r🚀 {name[:20]:<20} | {status_icon} {res_info:<10}\033[K\n")
                        sys.stdout.write(f"\r{bar_display}\033[K\n")
                        sys.stdout.write("\033[2F")
                        sys.stdout.flush()
                    except:
                        fail += 1
                        failed_channels.append(name)
                        sys.stdout.write(f"\r⚠ Hata: {name[:20]:<20} | ❌ [HATA]\033[K\n\n\033[2F")
                        sys.stdout.flush()

    run_all(hls_tasks, yt_tasks)

    save_cache(cache)
    sys.stdout.write(f"\r{bar_display}\033[K\n")
    sys.stdout.write(f"\r{'=' * 50}\033[K\n")
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()
    cloud_results = {n_: [e for _, e in sorted(v, key=lambda x: x[0])] for n_, v in cloud_results.items()}
    return cloud_results, failed_channels
    
def process_cloud_channels(cloud_channels_dict, tv_order_set):
    try:
        return _process_cloud_channels(cloud_channels_dict, tv_order_set)
    except BaseException:
        sys.stdout.write("\033[?25h"); sys.stdout.flush()
        raise

# ==================== SABİT LİNKLER ====================
def clean_filename(name):
    tr_to_en = {'ı': 'i', 'İ': 'I', 'ğ': 'g', 'Ğ': 'G', 'ü': 'u', 'Ü': 'U', 'ş': 's', 'Ş': 'S', 'ö': 'o', 'Ö': 'O', 'ç': 'c', 'Ç': 'C'}
    for tr, en in tr_to_en.items(): name = name.replace(tr, en)
    name = re.sub(r'[^\w]', '_', name); name = re.sub(r'_+', '_', name)
    return name.strip('_').upper()

def category_priority(channel_type): return {'iptv': 0, 'cloud': 1, 'radio': 2}.get(channel_type, 9)
def category_path(channel_type): return {'iptv': 'tv', 'cloud': 'cloud', 'radio': 'radyo'}.get(channel_type, 'tv')

def create_all_static_links(tv_order, iptv_channels, cloud_processed, radio_channels):
    all_links = {}; link_counter = {}; pool_dict = {}
    for source_dict in (iptv_channels, cloud_processed, radio_channels):
        for name, entries in source_dict.items():
            pool_dict.setdefault(name, deque())
            for idx, entry in enumerate(entries):
                extinf_line, url, is_youtube, channel_type = entry
                pool_dict[name].append({'extinf': extinf_line,'url': url,'is_youtube': is_youtube,'type': channel_type,'index': idx})
    for name, entries in pool_dict.items():
        pool_dict[name] = deque(sorted(entries, key=lambda item: category_priority(item['type'])))
    for channel_name in tv_order:
        base_filename = clean_filename(channel_name)
        if channel_name in pool_dict and pool_dict[channel_name]:
            item = pool_dict[channel_name].popleft()
            counter_key = (channel_name, item['type']); counter = link_counter.get(counter_key, 0) + 1; link_counter[counter_key] = counter
            count_str = f"_{counter}" if counter > 1 else ""; filename = f"{base_filename}{count_str}.m3u8"
            link_key = f"{category_path(item['type'])}/{filename}"
            display_name = f"{channel_name}" if item['is_youtube'] else channel_name
            all_links[link_key] = {'name': display_name,'extinf': item['extinf'],'stream_url': item['url'],'type': item['type'],'filename': filename,'link_key': link_key}
        else:
            filename = f"{base_filename}_FB.m3u8"; link_key = f"tv/{filename}"
            all_links[link_key] = {'name': f"{channel_name} (Fallback)",'extinf': f'#EXTINF:-1 tvg-name="{channel_name}.tr" ,{channel_name}','stream_url': FALLBACK_URL,'type': 'iptv','filename': filename,'link_key': link_key}
    return all_links

# ==================== PLAYLIST OLUŞTURMA ====================
def create_playlist(tv_order, iptv_channels, cloud_processed, radio_channels, failed_cloud_channels=None):
    if failed_cloud_channels is None: failed_cloud_channels = []
    pool_dict = {}
    for source_dict in (iptv_channels, cloud_processed, radio_channels):
        for name, entries in source_dict.items():
            pool_dict.setdefault(name, deque())
            for entry in entries:
                pool_dict[name].append({'data': entry,'is_youtube': entry[2],'type': entry[3]})
    for name, entries in pool_dict.items():
        pool_dict[name] = deque(sorted(entries, key=lambda item: category_priority(item['type'])))
    playlist = ["#EXTM3U refresh=\"300\""]
    iptv_count = cloud_count = radio_count = 0
    fallback_channels = []
    for channel_name in tv_order:
        if channel_name in pool_dict and pool_dict[channel_name]:
            item = pool_dict[channel_name].popleft()
            extinf_line, url, _, channel_type = item['data']
            playlist.append(extinf_line)
            playlist.append(url)
            if channel_type == 'radio': radio_count += 1
            elif item['is_youtube'] or channel_type == 'cloud': cloud_count += 1
            else: iptv_count += 1
        else:
            playlist.append(f'#EXTINF:-1 tvg-name="{channel_name}.tr" ,{channel_name}')
            playlist.append(FALLBACK_URL)
            if channel_name not in fallback_channels: fallback_channels.append(channel_name)
    for fc in failed_cloud_channels:
        if fc not in fallback_channels: fallback_channels.append(fc)
    fallback_count = len(fallback_channels)
    write_text_atomic(OUTPUT_FILE, "\n".join(playlist))
    total_channels = sum(1 for line in playlist if line.startswith('#EXTINF:'))
    print(f"📊 Playlist İstatistikleri:\n" + "-" * 50)
    print(f"   📺 TV Kanalları: {iptv_count}")
    print(f"   🎥 Cloud Kanalları: {cloud_count}")
    print(f"   📻 Radyo Kanalları: {radio_count}")
    print(f"   📊 Toplam Kanal: {total_channels}")
    print(f"   ❌ Fallback Kanal: {fallback_count}")
    if fallback_channels:
        for k_adi in fallback_channels:
            print(f"         • {k_adi}")
    zaman = datetime.now().strftime('%H:%M:%S')
    gelecek_calisma = datetime.now() + timedelta(hours=1)
    mesaj = (
        f"✅ <b>IPTV Listesi Güncellendi!</b>\n"
        f"⏰ Saat: {zaman}\n\n"
        f"📺 IPTV: {iptv_count}\n"
        f"🎥 Cloud: {cloud_count}\n"
        f"📻 Radyo: {radio_count}\n"
        f"📊 Toplam: {total_channels}\n"
        f"❌ Fallback: {fallback_count}"
    )
    if fallback_channels:
        mesaj += "\n\n⚠️ <b>Fallback Kanal Listesi:</b>\n"
        mesaj += "\n".join([f"• {k_adi}" for k_adi in fallback_channels])
    mesaj += f"\n\n📝 Sonraki Güncelleme: {gelecek_calisma.strftime('%H:%M:%S')}"
    threading.Thread(target=telegram_mesaj_gonder, args=(mesaj,), daemon=True).start()
    return {
        'playlist_content': "\n".join(playlist),
        'iptv_count': iptv_count,
        'cloud_count': cloud_count,
        'radio_count': radio_count,
        'total_channels': total_channels,
        'fallback_count': fallback_count,
        'fallback_channels': fallback_channels
    }
# ==================== CLOUDFLARE UPLOAD ====================
def upload_to_cloudflare_with_links(playlist_content, stats, all_links, yedek_content=""):
    cf_config = config['cloudflare']
    account_id = cf_config['account_id']
    worker_name = cf_config['worker_name']
    worker_url = cf_config['worker_url']
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}"
    headers = {'Authorization': f'Bearer {cf_config["api_token"]}', 'Content-Type': 'application/javascript'}
    all_links_js = "const ALL_CHANNEL_LINKS = new Map([\n"
    for filename, data in all_links.items():
        safe_extinf = data['extinf'].replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
        safe_name = data['name'].replace("'", "\\'").replace('"', '\\"')
        all_links_js += f"  ['{filename}', {{name: '{safe_name}', type: '{data['type']}', extinf: `{safe_extinf}`, stream: `{data['stream_url']}`}}],\n"
    all_links_js += "]);"
    html_template = '''<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📺 SekoBES IPTV PANELİ</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: #f0f0f0; line-height: 1.6; min-height: 100vh; padding: 20px; }
        .container { max-width: 1000px; margin: 0 auto; }
        header { text-align: center; margin-bottom: 30px; padding: 30px; background: rgba(255, 255, 255, 0.05); border-radius: 15px; backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1); }
        h1 { color: #fff; font-size: 2.2rem; display: flex; align-items: center; justify-content: center; gap: 15px; }
        .grid-layout { display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; margin-bottom: 30px; }
        .static-links-info { grid-column: 1 / -1; background: rgba(255, 255, 255, 0.05); border-radius: 12px; padding: 15px; text-align: center; border: 1px dashed rgba(76, 201, 240, 0.5); }
        .static-links-info a { color: #4cc9f0; text-decoration: none; font-weight: bold; }
        .card { background: rgba(255, 255, 255, 0.05); border-radius: 15px; padding: 25px; backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1); transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); display: flex; flex-direction: column; justify-content: space-between; }
        .card.blue { border-top: 5px solid #4cc9f0; }
        .card.pink { border-top: 5px solid #f72585; }
        .card.indigo { border-top: 5px solid #4361ee; }
        .card.purple { border-top: 5px solid #9d4edd; }
        .card.green { border-top: 5px solid #4ade80; }
        .card.orange { border-top: 5px solid #f8961e; }
        .card:hover { transform: translateY(-8px); background: rgba(255, 255, 255, 0.08); box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4); }
        .stat-header { display: flex; align-items: center; gap: 15px; margin-bottom: 10px; }
        .stat-value { font-size: 2rem; font-weight: bold; color: #fff; }
        .stat-label { color: #a0a0c0; text-transform: uppercase; font-size: 0.8rem; letter-spacing: 1px; }
        .url-title { font-size: 1.1rem; font-weight: bold; margin-bottom: 12px; }
        .url-code { background: rgba(0, 0, 0, 0.3); padding: 12px; border-radius: 8px; font-family: monospace; font-size: 0.85rem; word-break: break-all; margin-bottom: 15px; color: #a0e0ff; border-left: 3px solid rgba(255,255,255,0.2); }
        .btn-group { display: flex; gap: 10px; }
        .btn { flex: 1; padding: 12px; border-radius: 8px; text-decoration: none; text-align: center; font-weight: 600; cursor: pointer; border: none; display: flex; align-items: center; justify-content: center; gap: 8px; font-size: 0.9rem; transition: 0.2s; }
        .btn-copy { background: rgba(255, 255, 255, 0.1); color: #fff; border: 1px solid rgba(255,255,255,0.2); }
        .btn-download { background: #f8961e; color: #000; }
        .system-footer { grid-column: 1 / -1; display: flex; flex-direction: row; justify-content: space-around; padding: 20px; text-align: center; }
        @media (max-width: 768px) { .grid-layout { grid-template-columns: 1fr; } .system-footer { flex-direction: column; gap: 15px; } }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1><span class="logo">📺</span> SekoBES IPTV PANELİ <span class="logo">⚡</span></h1>
        </header>
        <div class="grid-layout">
                        <div class="static-links-info">
                <p>📝 <strong>Not:</strong> {{STATIC_COUNT}} Adet Kanal ({{IPTV_COUNT}} TV + {{CLOUD_COUNT}} Cloud + {{RADIO_COUNT}} Radyo). Listeler: <a href="{{WORKER_URL}}/playlist.m3u">TV (TV Kanalları)</a>, <a href="/radyolar.m3u">Radyolar</a> ve <a href="/yedek.m3u">Yedek</a> - <a href="/sabit.m3u">Sabit (Tüm)</a></p>
            </div>
                        <a href="/tv" class="card blue" style="text-decoration: none; display: block; cursor: pointer;">
                <div class="stat-header"><span style="font-size: 2.2rem;">📺</span><div><div class="stat-value">TV.m3u</div><div class="stat-label">TV Kanalları</div></div></div>
                <p style="font-size: 0.85rem; color: #888;">Sabit listeden sadece TV Kanalları group-title olanlar (Dizin.txt sıralı)</p>
            </a>
            <a href="/radyolar" class="card green" style="text-decoration: none; display: block; cursor: pointer;">
                <div class="stat-header"><span style="font-size: 2.2rem;">📻</span><div><div class="stat-value">Radyolar.m3u</div><div class="stat-label">Radyo Kanalları</div></div></div>
                <p style="font-size: 0.85rem; color: #888;">Sadece radyo kanalları</p>
            </a>
            <a href="/yedek" class="card pink" style="text-decoration: none; display: block; cursor: pointer;">
                <div class="stat-header"><span style="font-size: 2.2rem;">🔄</span><div><div class="stat-value">Yedek.m3u</div><div class="stat-label">TV Kanalları Yedek Liste</div></div></div>
                <p style="font-size: 0.85rem; color: #888;">Yedek dosyasındaki kanallar</p>
            </a>
            <div class="card indigo">
                <div class="url-title">🔗 Normal Playlist</div>
                <div class="url-code" id="url1">{{WORKER_URL}}/playlist.m3u</div>
                <div class="btn-group"><button class="btn btn-copy" onclick="copy('url1')">📋 Kopyala</button><a href="{{WORKER_URL}}/playlist.m3u" download="sekobes_playlist.m3u" class="btn btn-download">📥 İndir</a></div>
            </div>
            <div class="card purple">
                <div class="url-title">🔄 Sabit Linkli Playlist</div>
                <div class="url-code" id="urlStatic">{{WORKER_URL}}/sabit.m3u</div>
                <div class="btn-group"><button class="btn btn-copy" onclick="copy('urlStatic')">📋 Kopyala</button><a href="{{WORKER_URL}}/sabit.m3u" download="sabit.m3u" class="btn btn-download">📥 İndir</a></div>
            </div>
            <div class="card green">
                <div class="url-title" style="color: white;">🔄 Yedek Playlist</div>
                <div class="url-code" id="url2">{{WORKER_URL}}/yedek.m3u</div>
                <div class="btn-group"><button class="btn btn-copy" onclick="copy('url2')">📋 Kopyala</button><a href="{{WORKER_URL}}/yedek.m3u" download="yedek.m3u" class="btn btn-download">📥 İndir</a></div>
            </div>
            <div class="card orange system-footer">
                <div><strong style="display:block; color:#f8961e; font-size:0.7rem;">SİSTEM DURUMU</strong><span style="color: #4ade80;">● Aktif</span></div>
                <div><strong style="display:block; color:#f8961e; font-size:0.7rem;">SON GÜNCELLEME</strong><span>{{LAST_UPDATE}}</span></div>
                <div><strong style="display:block; color:#f8961e; font-size:0.7rem;">SONRAKİ GÜNCELLEME</strong><span>{{NEXT_UPDATE}}</span></div>
                <div><strong style="display:block; color:#f8961e; font-size:0.7rem;">GÜNCEL SAAT</strong><span id="currentTime">00:00:00</span></div>
            </div>
        </div>
        <footer style="text-align: center; color: #444; font-size: 0.75rem; padding-bottom: 20px;">© 2026 SekoBES IPTV | Panel v2.9.0</footer>
    </div>
    <script>
        function copy(id) {
            const text = document.getElementById(id).innerText;
            navigator.clipboard.writeText(text).then(() => {
                const el = document.getElementById(id);
                const original = el.innerText;
                el.innerText = "✅ Link Kopyalandı!";
                setTimeout(() => { el.innerText = original; }, 1500);
            });
        }
        function updateClock() { document.getElementById('currentTime').innerText = new Date().toLocaleTimeString('tr-TR'); }
        setInterval(updateClock, 1000); updateClock();
    </script>
</body>
</html>'''
    html_content = html_template.replace('{{IPTV_COUNT}}', str(stats['iptv_count'])) \
                                .replace('{{CLOUD_COUNT}}', str(stats['cloud_count'])) \
                                .replace('{{RADIO_COUNT}}', str(stats['radio_count'])) \
                                .replace('{{STATIC_COUNT}}', str(len(all_links))) \
                                .replace('{{WORKER_URL}}', worker_url) \
                                .replace('{{LAST_UPDATE}}', datetime.now().strftime('%H:%M:%S')) \
                                .replace('{{NEXT_UPDATE}}', (datetime.now() + timedelta(hours=1)).strftime('%H:%M:%S'))
    safe_html = html_content.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
    safe_yedek = yedek_content.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
    new_worker_code = f'''
addEventListener('fetch', event => {{ event.respondWith(handleRequest(event.request)) }})
const PLAYLIST_CONTENT = `{playlist_content}`;
const YEDEK_CONTENT = `{safe_yedek}`;
{all_links_js}
const YEDEK_LISTESI = (() => {{
  let lines = [];
  YEDEK_CONTENT.split(/\\r?\\n/).forEach(line => {{
    if(line.startsWith('#EXTINF:')) {{
      let parts = line.split(',');
      let name = parts.length > 1 ? parts[1] : "Bilinmeyen Kanal";
      lines.push({{name: name.trim(), extinf: line}});
    }}
  }});
  return lines;
}})();
function channelPath(d) {{ return d.type === 'iptv' ? 'tv' : d.type === 'cloud' ? 'cloud' : 'radyo'; }}
const ncHeaders = {{ 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0', 'Pragma': 'no-cache', 'Expires': '0' }};
const STATIC_PLAYLIST = (() => {{
  let lines = ['#EXTM3U refresh="2700"'];
  for (const [filename, d] of ALL_CHANNEL_LINKS) {{
    lines.push(d.extinf);
    lines.push(`{worker_url}/${{filename}}`);
  }}
  return lines.join('\\n');
}})();
const RADIO_PLAYLIST = (() => {{
  const lines = ['#EXTM3U refresh="2700"'];
  for (const [filename, d] of ALL_CHANNEL_LINKS) {{
    if (d.type !== 'radio') continue;
    lines.push(d.extinf);
    lines.push(`{worker_url}/${{filename}}`);
  }}
  return lines.join('\\n');
}})();
const TV_PLAYLIST = (() => {{
  const lines = ['#EXTM3U refresh="2700"'];
  for (const [filename, d] of ALL_CHANNEL_LINKS) {{
    // Sadece IPTV (TV Kanallari) tipindeki kanallar - Dizin.txt sirali sabit listeden filtre
    if (d.type !== 'iptv') continue;
    lines.push(d.extinf);
    lines.push(`{worker_url}/${{filename}}`);
  }}
  return lines.join('\\n');
}})();
// CLOUD.M3U KALDIRILDI
async function handleRequest(request) {{
  const url = new URL(request.url);
  if (url.pathname === '/sabit.m3u') return new Response(STATIC_PLAYLIST, {{ headers: {{ ...ncHeaders, 'Content-Type': 'audio/x-mpegurl' }} }});
  if (url.pathname === '/radyolar.m3u') return new Response(RADIO_PLAYLIST, {{ headers: {{ ...ncHeaders, 'Content-Type': 'audio/x-mpegurl' }} }});
  if (url.pathname === '/tv.m3u') return new Response(TV_PLAYLIST, {{ headers: {{ ...ncHeaders, 'Content-Type': 'audio/x-mpegurl' }} }});
  if (url.pathname === '/cloud.m3u') return Response.redirect(`${{url.origin}}/sabit.m3u`, 301);
  if (url.pathname === '/playlist.m3u') return new Response(PLAYLIST_CONTENT, {{ headers: {{ ...ncHeaders, 'Content-Type': 'audio/x-mpegurl' }} }});
  if (url.pathname === '/yedek.m3u') return new Response(YEDEK_CONTENT, {{ headers: {{ ...ncHeaders, 'Content-Type': 'audio/x-mpegurl' }} }});
  if (url.pathname === '/' || url.pathname === '/index.html') return new Response(`{safe_html}`, {{ headers: {{ 'Content-Type': 'text/html; charset=utf-8' }} }});
  if (url.pathname === '/tv' || url.pathname === '/tv/') {{
    let html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>📺 TV Kanalları (Tüm)</title><style>body{{font-family:Arial;margin:20px;background:#1a1a2e;color:white}}.channel{{padding:15px;margin:10px 0;background:rgba(255,255,255,0.05);border-radius:8px}}a{{color:white;text-decoration:none;font-weight:bold}}</style></head><body><h1>📺 TV Kanalları</h1><a href="/" style="display:inline-block; background:#4cc9f0; color:#1a1a2e; padding:8px 15px; border-radius:8px; text-decoration:none; font-weight:bold;">← Ana Sayfaya Dön</a><br><br>`;
    for (const [filename, d] of ALL_CHANNEL_LINKS) {{ if (d.type !== 'radio') html += `<div class="channel"><a href="/${{filename}}">${{d.type === 'cloud' ? '🎥' : '📺'}} ${{d.name}}</a></div>`; }}
    return new Response(html + '</body></html>', {{ headers: {{ 'Content-Type': 'text/html; charset=utf-8' }} }});
  }}
  if (url.pathname === '/radyolar' || url.pathname === '/radyolar/') {{
    let html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>📻 Radyo Kanalları</title><style>body{{font-family:Arial;margin:20px;background:#1a1a2e;color:white}}.channel{{padding:15px;margin:10px 0;background:rgba(255,255,255,0.05);border-radius:8px}}a{{color:white;text-decoration:none;font-weight:bold}}.playlist{{display:inline-block;background:#4ade80;color:#102a19;padding:10px 15px;border-radius:8px;text-decoration:none;margin-bottom:15px}}</style></head><body><h1>📻 Radyo Kanalları</h1><a class="playlist" href="/">← Ana Sayfaya Dön</a><br><br>`;
    for (const [filename, d] of ALL_CHANNEL_LINKS) {{ if (d.type === 'radio') html += `<div class="channel"><a href="/${{filename}}">📻 ${{d.name}}</a></div>`; }}
    return new Response(html + '</body></html>', {{ headers: {{ 'Content-Type': 'text/html; charset=utf-8' }} }});
  }}
  if (url.pathname === '/cloud' || url.pathname === '/cloud/') {{
    let html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>🎥 Cloud Kanalları</title><style>body{{font-family:Arial;margin:20px;background:#1a1a2e;color:white}}.channel{{padding:15px;margin:10px 0;background:rgba(255,255,255,0.05);border-radius:8px}}a{{color:white;text-decoration:none;font-weight:bold}}</style></head><body><h1>🎥 Cloud Kanalları</h1><a href="/" style="display:inline-block; background:#f72585; color:white; padding:8px 15px; border-radius:8px; text-decoration:none; font-weight:bold;">← Ana Sayfaya Dön</a><br><br>`;
    for (const [filename, d] of ALL_CHANNEL_LINKS) {{ if (d.type === 'cloud') html += `<div class="channel"><a href="/${{filename}}">🎥 ${{d.name}}</a></div>`; }}
    return new Response(html + '</body></html>', {{ headers: {{ 'Content-Type': 'text/html; charset=utf-8' }} }});
  }}
  if (url.pathname === '/yedek' || url.pathname === '/yedek/') {{
    let html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>🔄 Yedek Kanallar</title><style>body{{font-family:Arial;margin:20px;background:#1a1a2e;color:white}}.channel{{padding:15px;margin:10px 0;background:rgba(255,255,255,0.05);border-radius:8px}}a{{color:white;text-decoration:none;font-weight:bold;display:block}}</style></head><body><h1>🔄 Yedek Kanallar</h1><a href="/" style="display:inline-block; background:#f8961e; color:#1a1a2e; padding:8px 15px; border-radius:8px; text-decoration:none; font-weight:bold;">← Ana Sayfaya Dön</a><br><br>`;
    let lines = YEDEK_CONTENT.split(/\\r?\\n/);
    for (let i = 0; i < lines.length; i++) {{
      if (lines[i].startsWith('#EXTINF:')) {{
        let name = lines[i].split(',')[1] || "Bilinmeyen Kanal";
        let url = (lines[i + 1] && !lines[i + 1].startsWith('#')) ? lines[i + 1].trim() : "#";
        if (url !== "#") {{
          html += `<div class="channel"><a href="${{url}}">📺 ${{name.trim()}}</a></div>`;
        }}
      }}
    }}
    return new Response(html + '</body></html>', {{ headers: {{ 'Content-Type': 'text/html; charset=utf-8' }} }});
  }}
  const p = url.pathname.split('/');
  if ((p[1] === 'tv' || p[1] === 'cloud' || p[1] === 'radyo') && p[2]) {{
    const d = ALL_CHANNEL_LINKS.get(`${{p[1]}}/${{p[2]}}`);
    if (d) return new Response(null, {{ status: 302, headers: {{ ...ncHeaders, 'Location': d.stream }} }});
  }}
  return new Response("Not Found", {{ status: 404 }});
}}'''
    try:
        r = SESSION.put(url, headers=headers, data=new_worker_code.encode('utf-8'), timeout=20)
        if r.status_code != 200: 
            print(f"   ⚠️ CF Hatası: {r.text[:100]}")
        return r.status_code == 200
    except Exception as e:
        print(f"   ❌ Cloudflare hatası: {e}")
        return False

# ==================== ANA FONKSİYON ====================
def main_process():
    print("=" * 50); print("📺 SekoBES IPTV PANELİ"); print("=" * 50)
    print(f"🚀 Başlama: {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 50)
    iptv_channels, cloud_channels, radio_channels, tv_order = read_all_channels_file(CLOUD_FILE)
    tv_order_set = set(tv_order)
    # IPTV altında HTML (web sayfası) dönen linkler yayın değildir: listeden çıkar, fallback'e düşsün
    _kontrol = [(n, e) for n, es in iptv_channels.items() for e in es]
    with ThreadPoolExecutor(max_workers=8) as _ex:
        _sonuc = list(_ex.map(lambda ne: is_web_page(ne[1][1]), _kontrol))
    web_page_channels = []
    for (_n, _e), _html in zip(_kontrol, _sonuc):
        if _html:
            iptv_channels[_n].remove(_e)
            if not iptv_channels[_n]: del iptv_channels[_n]
            if _n not in web_page_channels: web_page_channels.append(_n)
    yedek_content = ""
    if os.path.exists(YEDEK_FILE):
        with open(YEDEK_FILE, 'r', encoding='utf-8') as f: yedek_content = f.read()
    actual_iptv_count = sum(len(v) for v in iptv_channels.values())
    actual_radio_count = sum(len(v) for v in radio_channels.values())
    actual_cloud_count = sum(len(v) for v in cloud_channels.values())
    toplam = actual_iptv_count + actual_cloud_count + actual_radio_count
    print(f"   📊 TOPLAM: {toplam} Kanal")
    print(f"   📂 Yedek.m3u")
    print("=" * 50)
    print("🔄 Stream URL'leri Alınıyor")
    print("-" * 50)
    cloud_processed, failed_cloud_channels = process_cloud_channels(cloud_channels, tv_order_set)
    failed_cloud_channels = failed_cloud_channels + [n for n in web_page_channels if n not in failed_cloud_channels]
    playlist_result = create_playlist(tv_order, iptv_channels, cloud_processed, radio_channels, failed_cloud_channels)
    all_links = create_all_static_links(tv_order, iptv_channels, cloud_processed, radio_channels)
    stats = {'total_channels': playlist_result['total_channels'],'iptv_count': playlist_result['iptv_count'],'cloud_count': playlist_result['cloud_count'],'radio_count': playlist_result['radio_count'],'fallback_count': playlist_result['fallback_count']}
    cloudflare_success = upload_to_cloudflare_with_links(playlist_result['playlist_content'], stats, all_links, yedek_content)
    if cloudflare_success:
        print("🔐 Cloud Kanallar Güncellendi!\n")
    else:
        print("⚠ Cloudflare güncellemesi yapılamadı - önceki sürüm yayında kalıyor\n")
    worker_playlist_url = config.get('cloudflare', {}).get('worker_url', '').rstrip('/') + '/playlist.m3u'
    print(f"📺 M3U Linki: {worker_playlist_url}"); print("=" * 50)

if __name__ == "__main__":
    if "--once" in sys.argv:
        main_process()
    else:
        while True:
        try:
            main_process()
            toplam_saniye = 3600
            for i in range(toplam_saniye, 0, -1):
                saat = i // 3600; dk = (i % 3600) // 60; sn = i % 60
                print(f"\r⏱ Cloud güncelleme için kalan: {saat:02d}:{dk:02d}:{sn:02d} ", end="", flush=True)
                time.sleep(1)
            print()
        except KeyboardInterrupt:
            print("\n\n👋 Durduruldu!"); sys.exit(0)
        except Exception as e:
            print(f"\n❌ KRİTİK HATA: {e}"); print("🔄 60 saniye sonra tekrar deneniyor..."); time.sleep(60)