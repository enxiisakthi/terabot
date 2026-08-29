import os
import re
import sys
import time
import asyncio
import logging
import threading
import subprocess
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode
from telethon import TelegramClient, events

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# =========================================================================
# YOUR DETAILS
# =========================================================================
API_ID = 37757044
API_HASH = "414c5699e4129ee3bd3aa9fe800d35ee"
BOT_TOKEN = "8857970216:AAG1e35bYykHU3sQwMcagpYcyhRAT-vD6lQ"
# =========================================================================

BASE = "https://www.1024tera.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MAX_FILES = 10

bot = TelegramClient('sakthi_bot_session', API_ID, API_HASH)


# =========================================================================
# RENDER KEEP-ALIVE — Free server sleep ஆகாம இருக்க இது தேவை
# =========================================================================
class _KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"alive")

    def log_message(self, *args):
        pass


def start_keepalive():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _KeepAlive)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"✅ Keep-alive server started on port {port}")

    threading.Thread(target=_self_ping, daemon=True).start()
    print("✅ Self-ping started (every 5 min)")


SELF_URL = "https://terabot-uuii.onrender.com"

def _self_ping():
    while True:
        time.sleep(300)
        try:
            requests.get(SELF_URL, timeout=30)
            logging.info("Self-ping OK")
        except Exception as e:
            logging.warning(f"Self-ping failed: {e}")
# =========================================================================


async def delete_later(msg, delay=300):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


def load_ndus():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("COOKIE_JSON="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(".env file with COOKIE_JSON=<ndus> not found")


def clean_name(name: str) -> str:
    name = "".join(x for x in name if x.isalnum() or x in "._- ").strip()
    return name or "video"


def extract_surl(url: str):
    m = re.search(r'/s/1([A-Za-z0-9_-]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'surl=1?([A-Za-z0-9_-]+)', url)
    if m:
        return m.group(1)
    return None


class TeraBox:
    def __init__(self, ndus: str):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        for dom in (".1024tera.com", ".terabox.com"):
            self.s.cookies.set("ndus", ndus, domain=dom, path="/")

    def _jstoken(self, surl: str) -> str:
        html = self.s.get(
            f"{BASE}/wap/share/filelist?surl={surl}&clearCache=1", timeout=60).text
        m = re.search(r'fn%28%22([0-9A-Fa-f]+)%22%29', html)
        if not m:
            raise ValueError("jsToken கிடைக்கல — லிங்க் invalid/expired.")
        return m.group(1)

    def _share_info(self, surl: str, js: str) -> dict:
        data = self.s.get(f"{BASE}/api/shorturlinfo", params={
            "app_id": "250528", "shorturl": "1" + surl, "root": "1", "web": "1",
            "channel": "dubox", "clienttype": "0", "jsToken": js,
            "t": str(int(time.time()))}, timeout=30).json()
        if data.get("errno"):
            raise ValueError(f"Share info error (errno {data.get('errno')}).")
        return data

    def list_files(self, surl: str):
        """Returns list of (fs_id, filename, size) for all files in the share."""
        out = []

        def walk(dirpath=None, depth=0):
            p = {"app_id": "250528", "web": "1", "channel": "10",
                 "shorturl": surl, "root": "0" if dirpath else "1"}
            if dirpath:
                p["dir"] = dirpath
            d = self.s.get("https://www.terabox.com/share/list",
                           params=p, timeout=30).json()
            for e in d.get("list", []):
                if str(e.get("isdir")) == "1":
                    if depth < 3:
                        walk(e["path"], depth + 1)
                else:
                    out.append((e["fs_id"], e["server_filename"],
                                int(e.get("size", 0))))
        walk()
        return out

    def _segments(self, surl: str, fid) -> list:
        js = self._jstoken(surl)
        info = self._share_info(surl, js)
        for quality in ("M3U8_AUTO_1080", "M3U8_AUTO_720", "M3U8_AUTO_480"):
            u = f"{BASE}/share/streaming?" + urlencode({
                "uk": str(info["uk"]), "shareid": str(info["shareid"]),
                "type": quality, "fid": str(fid), "sign": info["sign"],
                "timestamp": str(info["timestamp"]), "jsToken": js, "esl": "1",
                "isplayer": "1", "ehps": "1", "clienttype": "0",
                "app_id": "250528", "web": "1", "channel": "dubox"})
            r = self.s.get(u, timeout=60)
            if r.text.startswith("#EXTM3U"):
                return [l.strip() for l in r.text.split("\n")
                        if l.strip() and not l.startswith("#")]
        raise ValueError("இந்த file-க்கு stream கிடைக்கல (videos மட்டும் supported).")

    def download(self, surl: str, fid, dest_mp4: str, status=None) -> str:
        """Download video via HLS segments; returns final file path."""
        segs = self._segments(surl, fid)
        ts_path = dest_mp4 + ".ts"
        with open(ts_path, "wb") as f:
            for i, u in enumerate(segs):
                for attempt in range(4):
                    try:
                        r = self.s.get(u, stream=True, timeout=120)
                        r.raise_for_status()
                        for chunk in r.iter_content(262144):
                            if chunk:
                                f.write(chunk)
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        time.sleep(2 ** attempt)
                if status:
                    status(i + 1, len(segs))
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run([exe, "-y", "-i", ts_path, "-c", "copy", dest_mp4],
                           check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            os.remove(ts_path)
            return dest_mp4
        except Exception:
            os.replace(ts_path, dest_mp4)
            return dest_mp4


TB = TeraBox(load_ndus())


@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.respond(
        "👋 Welcome Sakthi!\n\nenaku **TeraBox Link** அனுப்புங்க, "
        "நான் அதை உங்களுக்கு **Direct Video File**-ஆ மாத்தி தர்றேன்!"
    )


@bot.on(events.NewMessage)
async def terabox_handler(event):
    if event.text.startswith('/start'):
        return

    url = event.text.strip()
    if not any(k in url for k in ("terabox", "1024tera", "terashare", "terafile",
                                  "nephobox", "teraboxapp")):
        await event.respond("⚠️ தயவுசெய்து சரியான **TeraBox லிங்க்** மட்டும் அனுப்பவும்!")
        return

    surl = extract_surl(url)
    if not surl:
        await event.respond("⚠️ லிங்க்கில் share id கிடைக்கல. மறுபடி சரிபாருங்க.")
        return

    status_msg = await event.respond("🔄 **Processing your link... Please wait...**")

    try:
        files = TB.list_files(surl)
        if not files:
            await status_msg.edit("❌ இந்த லிங்க்கில் files எதுவும் இல்லை.")
            return

        if len(files) > MAX_FILES:
            await status_msg.edit(
                f"📁 {len(files)} files உள்ளன — முதல் {MAX_FILES} மட்டும் அனுப்பறேன்.")
            files = files[:MAX_FILES]

        for idx, (fid, name, size) in enumerate(files, 1):
            out_name = clean_name(name)
            if not out_name.lower().endswith((".mp4", ".ts", ".mkv", ".avi")):
                out_name += ".mp4"
            await status_msg.edit(
                f"📥 **Downloading [{idx}/{len(files)}]** {out_name} ...")

            def status(a, b, idx=idx):
                logging.info(f"file {idx} segment {a}/{b}")

            path = TB.download(surl, fid, out_name, status=status)

            await status_msg.edit(f"📤 **Uploading [{idx}/{len(files)}]** ...")
            sent = await bot.send_file(
                event.chat_id, path,
                caption=f"🎬 **TeraBox Video** ({idx}/{len(files)})\n\n📝 {out_name}"
                        f"\n\n⏳ 5 நிமிடத்தில் இந்த file auto-delete ஆகும்!")
            asyncio.create_task(delete_later(sent))
            os.remove(path)

        await status_msg.delete()

    except Exception as e:
        logging.error(f"Error occurred: {e}")
        await status_msg.edit(f"❌ **Error:** {e}")


def main():
    print("⚡ TeraBox Downloader Bot ready-ஆ இருக்கு...")
    start_keepalive()
    bot.start(bot_token=BOT_TOKEN)
    bot.run_until_disconnected()


if __name__ == '__main__':
    main()
