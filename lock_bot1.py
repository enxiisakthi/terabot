import os
import re
import sys
import json
import time
import asyncio
import logging
import threading
import subprocess
import requests
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urljoin
from telethon import TelegramClient, events

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("TeraBoxBot")

# =========================================================================
# CONFIG — Environment variables first, fallback to defaults
# =========================================================================
API_ID = int(os.environ.get("API_ID", 37757044))
API_HASH = os.environ.get("API_HASH", "414c5699e4129ee3bd3aa9fe800d35ee")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8857970216:AAG1e35bYykHU3sQwMcagpYcyhRAT-vD6lQ")

BASE = "https://www.1024tera.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MAX_FILES = int(os.environ.get("MAX_FILES", 10))
SELF_URL = os.environ.get("SELF_URL", "https://terabot-uuii.onrender.com")
PING_INTERVAL = int(os.environ.get("PING_INTERVAL", 300))
AUTO_DELETE_SECONDS = int(os.environ.get("AUTO_DELETE_SECONDS", 300))
USER_MSG_DELETE_DELAY = int(os.environ.get("USER_MSG_DELETE_DELAY", 10))
MAX_BYTES = 2_000_000_000

STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")

bot = TelegramClient('sakthi_bot_session', API_ID, API_HASH)


# =========================================================================
# RENDER KEEP-ALIVE + SELF-PING
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
    logger.info(f"Keep-alive server started on port {port}")

    threading.Thread(target=_self_ping, daemon=True).start()
    logger.info(f"Self-ping started (every {PING_INTERVAL}s)")


def _self_ping():
    while True:
        time.sleep(PING_INTERVAL)
        try:
            requests.get(SELF_URL, timeout=30)
            logger.info("Self-ping OK")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")
# =========================================================================


# =========================================================================
# STATS MANAGEMENT
# =========================================================================
def load_stats():
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"delivered": 0, "users": {}, "started": datetime.now(timezone.utc).isoformat()}


def save_stats(stats):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass


def record_delivery(user_id, user_name, file_name, size):
    stats = load_stats()
    stats["delivered"] = stats.get("delivered", 0) + 1
    if "users" not in stats:
        stats["users"] = {}
    key = str(user_id)
    if key not in stats["users"]:
        stats["users"][key] = {"name": user_name, "count": 0, "bytes": 0}
    stats["users"][key]["count"] += 1
    stats["users"][key]["bytes"] = stats["users"][key].get("bytes", 0) + size
    save_stats(stats)


def human_readable_size(size_bytes):
    if size_bytes is None or size_bytes == 0:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"
# =========================================================================


async def delete_later(msg, delay=AUTO_DELETE_SECONDS):
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
    ndus_env = os.environ.get("COOKIE_JSON")
    if ndus_env:
        return ndus_env
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


# =========================================================================
# WRAPPER / SHORT-LINK RESOLVER MODULE
# Handles links like https://teraboxlinke.com/v/... that redirect (via
# HTTP 30x, meta-refresh or JS) to the real TeraBox share page.
# =========================================================================
WRAPPER_DOMAINS = (
    "teraboxlinke.com", "teraboxlink.com", "teraboxdownloader",
    "teraurl.com", "terabox.app/", "cybernewhub.com",
    "t.ly", "bit.ly", "tinyurl.com",
    "shorturl.at", "cutt.ly", "is.gd", "rb.gy",
)

TERABOX_HOST_RE = re.compile(
    r'https?://(?:www\.)?(?:1024tera|1024terabox|terabox|teraboxapp|teraboxlink'
    r'|terasharelink|teraboxshare|teraboxurl|freeterabox|nephobox|mirrobox'
    r'|momerybox|gibibox|goaibox|4funbox|terafileshare)[^\s"\'<>]*'
    r'(?:/s/1[A-Za-z0-9_-]+|surl=1?[A-Za-z0-9_-]+)[^\s"\'<>]*'
)


def is_wrapper_link(url: str) -> bool:
    u = url.lower()
    if extract_surl(url):
        return False
    return any(d in u for d in WRAPPER_DOMAINS)


def _clean_candidate(cand: str, base_url: str) -> str:
    cand = cand.strip().strip("'\"")
    cand = cand.replace("\\u002F", "/").replace("&amp;", "&")
    if not cand.startswith("http"):
        cand = urljoin(base_url, cand)
    return cand


def _last_path_segment(url: str):
    from urllib.parse import urlparse
    segs = [s for s in urlparse(url).path.split("/") if s]
    return segs[-1] if segs else None


def _next_hop_candidates(html: str, current_url: str) -> list:
    """Extract possible next URLs from a wrapper page's HTML/JS."""
    cands = []
    last_seg = _last_path_segment(current_url)

    m = re.search(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+url=([^"\'>\s]+)',
                  html, re.I)
    if m:
        cands.append(m.group(1))

    for m in re.finditer(
            r'(?:location\.href\s*=\s*|location\.replace\(|window\.location\s*=\s*)["\']?(https?://[^"\'`)\s;]+)',
            html, re.I):
        cands.append(m.group(1))

    # JS template literals: const target = `https://host/v/${linkId}`
    for m in re.finditer(r'`?(https?://[^"\'`\s]+?\$\{[^}]+\}[^"\'`\s]*?)`?', html):
        if last_seg:
            cands.append(re.sub(r'\$\{[^}]+\}', last_seg, m.group(1)))

    # JS string concat: 'https://1024terabox.com/s/' + encodeURIComponent(videoID)
    for m in re.finditer(
            r'["\'](https?://[^"\']+?/s/)["\']\s*\+\s*(?:encodeURIComponent\s*\(\s*)?\w+',
            html, re.I):
        if last_seg:
            cands.append(m.group(1) + last_seg)

    m = TERABOX_HOST_RE.search(html)
    if m:
        cands.append(m.group(0))

    return cands


def _expand_url_sync(url: str):
    """Follow HTTP redirects and embedded HTML/JS redirects, hop by hop.

    Returns the resolved TeraBox share URL, or None if not found.
    """
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

        current = url
        for _hop in range(5):
            resp = session.get(current, allow_redirects=True, timeout=30)
            final_url = resp.url

            if extract_surl(final_url):
                logger.info(f"Wrapper resolved via HTTP redirect: {final_url}")
                return final_url

            html = resp.text or ""
            for cand in _next_hop_candidates(html, final_url):
                cand = _clean_candidate(cand, final_url)
                surl = extract_surl(cand)
                if surl:
                    logger.info(f"Wrapper resolved via page JS: {cand}")
                    return cand
                if cand != current:
                    current = cand
                    break
            else:
                logger.warning(f"Wrapper could not be resolved: {url}")
                return None

        logger.warning(f"Wrapper resolution exceeded max hops: {url}")
        return None
    except Exception as e:
        logger.warning(f"expand_url failed for {url}: {e}")
        return None


async def expand_wrapper_url(url: str):
    """Async wrapper — runs the blocking resolver in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _expand_url_sync, url)
# =========================================================================


class TeraBox:
    def __init__(self, ndus: str):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                               "Referer": BASE + "/"})
        for dom in (".1024tera.com", ".terabox.com"):
            self.s.cookies.set("ndus", ndus, domain=dom, path="/")

    def _jstoken(self, surl: str) -> str:
        html = self.s.get(
            f"{BASE}/wap/share/filelist?surl={surl}&clearCache=1", timeout=60).text
        m = re.search(r'fn%28%22([0-9A-Fa-f]+)%22%29', html)
        if not m:
            raise ValueError("jsToken not found — link may be invalid or expired.")
        return m.group(1)

    def _share_info(self, surl: str, js: str) -> dict:
        data = self.s.get(f"{BASE}/api/shorturlinfo", params={
            "app_id": "250528", "shorturl": "1" + surl, "root": "1", "web": "1",
            "channel": "dubox", "clienttype": "0", "jsToken": js,
            "t": str(int(time.time()))}, timeout=30).json()
        if data.get("errno"):
            raise ValueError(f"Share info error (errno {data.get('errno')}).")
        return data

    def dlink(self, surl: str, fid) -> str:
        """Resolve the original full-file download URL (official web-app flow)."""
        js = self._jstoken(surl)
        info = self._share_info(surl, js)
        data = self.s.get(f"{BASE}/share/download", params={
            "app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0",
            "jsToken": js, "scene": "purchased_list", "product": "share",
            "nozip": "0", "shareid": str(info["shareid"]), "sign": info["sign"],
            "timestamp": str(info["timestamp"]), "uk": str(info["uk"]),
            "primaryid": str(info["shareid"]),
            "fid_list": json.dumps([str(fid)])}, timeout=30).json()
        if data.get("errno") or not data.get("dlink"):
            raise ValueError(
                f"dlink unavailable (errno {data.get('errno')}) — "
                "login cookie may be expired.")
        return data["dlink"]

    def download_full(self, url: str, dest: str, expect_size: int,
                      status=None) -> int:
        """Stream the original file in 1MB chunks; verify size before accept."""
        written = 0
        r = self.s.get(url, stream=True, timeout=(30, 300))
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
                    if status:
                        status(written, expect_size)
        if expect_size and abs(written - expect_size) > 1024:
            if os.path.exists(dest):
                os.remove(dest)
            raise ValueError(
                f"Size mismatch: got {human_readable_size(written)}, "
                f"expected {human_readable_size(expect_size)}.")
        return written

    def list_files(self, surl: str):
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
        raise ValueError("No stream available for this file (videos only).")

    def download(self, surl: str, fid, dest_mp4: str, status=None) -> str:
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
            import shutil
            exe = shutil.which("ffmpeg")
            if not exe:
                import imageio_ffmpeg
                exe = imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run([exe, "-y", "-i", ts_path, "-c", "copy", dest_mp4],
                           check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            os.remove(ts_path)
            return dest_mp4
        except Exception as e:
            logger.error(f"ffmpeg conversion failed: {e}")
            os.replace(ts_path, dest_mp4)
            return dest_mp4


TB = TeraBox(load_ndus())


# =========================================================================
# BOT COMMAND HANDLERS
# =========================================================================
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user_name = event.sender.first_name or "User"
    await event.respond(
        f"👋 Welcome **{user_name}**!\n\n"
        "Send me any **TeraBox link**, I'll convert it to a direct video file!\n\n"
        "📎 Supports multiple files per share link\n"
        "🎬 Downloads via HLS streaming (best quality)\n"
        "⚠️ Max 2GB per file (Telegram limit)\n\n"
        "**Commands:**\n"
        "/help — Usage guide\n"
        "/stats — Delivery statistics\n"
        "/ping — Check if I'm alive"
    )


@bot.on(events.NewMessage(pattern='/ping'))
async def ping_handler(event):
    start = time.time()
    msg = await event.respond("🏓 Pong!")
    elapsed = round((time.time() - start) * 1000)
    await msg.edit(
        f"🏓 **Pong!** ({elapsed}ms)\n"
        f"✅ Bot is alive and running!\n"
        f"🕐 Uptime since: {load_stats().get('started', 'unknown')}"
    )


@bot.on(events.NewMessage(pattern='/help'))
async def help_handler(event):
    stats = load_stats()
    await event.respond(
        "📖 **Help Guide**\n\n"
        "**How to use:**\n"
        "1️⃣ Send a TeraBox share link (or a shortened wrapper link)\n"
        "2️⃣ I'll resolve it & download the video via HLS streaming\n"
        "3️⃣ Files are sent directly to this chat\n\n"
        "**Supported domains:**\n"
        "terabox.com, 1024tera.com, teraboxapp, terasharelink, "
        "terafile, nephobox, freeterabox & more!\n"
        "Short links like teraboxlinke.com/v/... are auto-resolved.\n\n"
        "**Limits:**\n"
        f"• Max {MAX_FILES} files per share\n"
        "• Max 2GB per file (Telegram limit)\n"
        f"• Files auto-delete after {AUTO_DELETE_SECONDS // 60} min\n"
        f"• Your link message auto-deletes after delivery\n\n"
        "**Commands:**\n"
        "/start — Welcome message\n"
        "/help — This guide\n"
        "/stats — Delivery statistics\n"
        "/ping — Health check\n\n"
        f"📊 Total files delivered: **{stats.get('delivered', 0)}**"
    )


@bot.on(events.NewMessage(pattern='/stats'))
async def stats_handler(event):
    stats = load_stats()
    user_id = str(event.sender_id)
    user_stats = stats.get("users", {}).get(user_id, {})
    user_count = user_stats.get("count", 0)
    user_bytes = user_stats.get("bytes", 0)
    total = stats.get("delivered", 0)
    await event.respond(
        f"📊 **Statistics**\n\n"
        f"🌐 Total delivered: **{total}** files\n"
        f"👤 Your deliveries: **{user_count}** files\n"
        f"📦 Your data: **{human_readable_size(user_bytes)}**"
    )


# =========================================================================
# MAIN TERABOX HANDLER
# =========================================================================
@bot.on(events.NewMessage)
async def terabox_handler(event):
    if not event.text or event.text.startswith('/'):
        return

    url = event.text.strip()
    link_match = re.search(r"https?://\S+", url)
    if link_match:
        url = link_match.group(0).rstrip(".,;!?")

    supported = ("terabox", "1024tera", "terashare", "terafile",
                 "nephobox", "teraboxapp", "momerybox", "gibibox",
                 "goaibox", "4funbox", "mirrobox", "teraboxlink",
                 "t.ly", "bit.ly", "tinyurl")
    if not any(k in url.lower() for k in supported):
        await event.respond(
            "⚠️ Please send a valid **TeraBox link** only!\n\n"
            "Example: `https://terabox.com/s/xxxxx`"
        )
        return

    user_name = event.sender.first_name or "User"
    status_msg = await event.respond("🔄 **Processing your link... Please wait...**")

    # Resolve wrapper / shortened links to the real TeraBox share URL
    if is_wrapper_link(url):
        await status_msg.edit("🔗 **Resolving shortened link...**\n\n⏳ Following redirects...")
        resolved = await expand_wrapper_url(url)
        if not resolved:
            await status_msg.edit(
                "❌ Couldn't resolve this shortened link.\n\n"
                "It may be expired or unsupported.\n"
                "Try sending the direct TeraBox link instead."
            )
            return
        logger.info(f"Wrapper link resolved to: {resolved}")
        url = resolved

    surl = extract_surl(url)
    if not surl:
        await status_msg.edit("⚠️ Couldn't find a share ID in the link. Please check the URL format.")
        return

    try:
        files = TB.list_files(surl)
        if not files:
            await status_msg.edit("❌ No files found in this share link.")
            return

        total_files = len(files)
        total_size = sum(s for _, _, s in files)

        if total_files > MAX_FILES:
            await status_msg.edit(
                f"📁 **{total_files} files found** ({human_readable_size(total_size)})\n"
                f"Sending first {MAX_FILES} files only."
            )
            files = files[:MAX_FILES]
        else:
            await status_msg.edit(
                f"📁 **{total_files} file(s) found** ({human_readable_size(total_size)})\n"
                f"⏳ Starting download..."
            )

        for idx, (fid, name, size) in enumerate(files, 1):
            out_name = clean_name(name)
            if not out_name.lower().endswith((".mp4", ".ts", ".mkv", ".avi", ".mov")):
                out_name += ".mp4"

            if size > MAX_BYTES:
                await event.respond(
                    f"⚠️ **({idx}/{len(files)})** `{out_name}` is "
                    f"**{human_readable_size(size)}** — exceeds 2GB limit. Skipping."
                )
                continue

            await status_msg.edit(
                f"📥 **Downloading [{idx}/{len(files)}]**\n\n"
                f"📁 {out_name}\n"
                f"📦 Size: {human_readable_size(size)}\n"
                f"🔐 Resolving original file link..."
            )

            def dlink_download(idx=idx, name=out_name, size=size):
                url = TB.dlink(surl, fid)

                def prog(done, total):
                    pct = round(done / total * 100) if total else 0
                    logger.info(f"[{idx}] {name}: {human_readable_size(done)} ({pct}%)")

                TB.download_full(url, out_name, size, status=prog)
                return out_name

            try:
                loop = asyncio.get_event_loop()
                path = await loop.run_in_executor(None, dlink_download)
            except Exception as e:
                logger.warning(f"Full-file download failed, HLS fallback: {e}")
                await status_msg.edit(
                    f"📥 **Downloading [{idx}/{len(files)}]**\n\n"
                    f"📁 {out_name}\n"
                    f"⚠️ Original link unavailable — stream fallback.\n"
                    f"⏳ Fetching segments..."
                )

                def status(a, b, idx=idx, name=out_name, size=size):
                    pct = round(a / b * 100)
                    logger.info(f"[{idx}] {name}: segment {a}/{b} ({pct}%)")

                path = TB.download(surl, fid, out_name, status=status)

            actual_size = os.path.getsize(path) if os.path.exists(path) else size

            await status_msg.edit(
                f"📤 **Uploading [{idx}/{len(files)}]**\n\n"
                f"📁 {out_name}\n"
                f"📦 {human_readable_size(actual_size)}"
            )

            sent = await bot.send_file(
                event.chat_id, path,
                caption=f"🎬 **TeraBox Video** ({idx}/{len(files)})\n\n"
                        f"📁 {out_name}\n"
                        f"📦 {human_readable_size(actual_size)}\n\n"
                        f"⏳ Auto-deletes in {AUTO_DELETE_SECONDS // 60} min"
            )
            asyncio.create_task(delete_later(sent))
            os.remove(path)

            record_delivery(event.sender_id, user_name, out_name, actual_size)
            logger.info(f"Delivered: {out_name} ({human_readable_size(actual_size)})")

        await status_msg.edit("✅ **All files delivered successfully!**")
        asyncio.create_task(delete_later(status_msg, delay=60))

        # Auto-delete the user's original link message
        asyncio.create_task(delete_later(event.message, delay=USER_MSG_DELETE_DELAY))

    except Exception as e:
        logger.error(f"Error occurred: {e}")
        await status_msg.edit(f"❌ **Error:** {str(e)[:200]}")


# =========================================================================
# MAIN
# =========================================================================
def main():
    logger.info("⚡ TeraBox Downloader Bot is starting...")
    logger.info(f"Max files per share: {MAX_FILES}")
    logger.info(f"Self-ping URL: {SELF_URL}")
    logger.info(f"Auto-delete: {AUTO_DELETE_SECONDS}s")
    start_keepalive()
    bot.start(bot_token=BOT_TOKEN)
    logger.info("✅ Bot is running and listening for messages!")
    bot.run_until_disconnected()


if __name__ == '__main__':
    main()
