import os
import asyncio
import json
import re
import time
import requests
import tempfile
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import imageio_ffmpeg
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from playwright.async_api import async_playwright

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN")
CAPSOLVER_KEY = os.getenv("CAPSOLVER_KEY")

SITE_URL = os.getenv("SITE_URL", "https://www.terabox.com/")
# Older deployments use TERABOX_COOKIE; accept both names during migration.
COOKIE_JSON = os.getenv("COOKIE_JSON") or os.getenv("TERABOX_COOKIE")
TERABOX_BASE = "https://www.1024tera.com"

CAPSOLVER_API = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"

SUPPORTED_HOSTS = (
    "terabox.com",
    "teraboxapp.com",
    "1024tera.com",
    "1024terabox.com",
    "teraboxlink.com",
    "teraboxlinke.com",
    "terasharelink.com",
    "nephobox.com",
    "momerybox.com",
    "gibibox.com",
    "goaibox.com",
    "4funbox.com",
)


class FullVideoVerificationError(RuntimeError):
    """Raised when TeraBox returns a preview instead of the selected video."""


def extract_ndus(cookie_value):
    """Accept a raw ndus token, a Cookie header, or exported cookie JSON."""
    value = (cookie_value or "").strip()
    if not value:
        return None
    if value.startswith("{") or value.startswith("["):
        try:
            data = json.loads(value)
            cookies = data.get("cookies", data) if isinstance(data, dict) else data
            for cookie in cookies:
                if isinstance(cookie, dict) and cookie.get("name") == "ndus":
                    return str(cookie.get("value") or "").strip() or None
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?:^|[;\s])ndus=([^;\s]+)", value, flags=re.IGNORECASE)
    return match.group(1) if match else value


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal endpoint required by Render Web Service port detection."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = os.getenv("PORT")
    if not port:
        return
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), HealthCheckHandler)
    Thread(target=server.serve_forever, daemon=True, name="health-server").start()


async def ensure_chromium_installed():
    """Install Playwright's bundled Chromium if the deployment image lacks it."""
    async with async_playwright() as p:
        chromium_path = Path(p.chromium.executable_path)
    if chromium_path.is_file():
        return

    print("Playwright Chromium is missing; installing it now...")
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
        timeout=600,
    )


def is_supported_terabox_url(value):
    """Allow supported TeraBox share and wrapper hosts, never arbitrary URLs."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and bool(host) and any(
        host == domain or host.endswith(f".{domain}") for domain in SUPPORTED_HOSTS
    )


def extract_share_id(value):
    """Extract the share token used by TeraBox's web download endpoints."""
    match = re.search(r"/s/1([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    match = re.search(r"[?&]surl=1?([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else None


class TeraBoxDirectDownload:
    """Fetch the original via TeraBox's web endpoint, not a simulated UI click."""

    def __init__(self, cookie_value):
        ndus = extract_ndus(cookie_value)
        if not ndus:
            raise RuntimeError("No ndus cookie was supplied.")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{TERABOX_BASE}/",
        })
        for domain in (".1024tera.com", ".terabox.com"):
            self.session.cookies.set("ndus", ndus, domain=domain, path="/")

    def _js_token(self, share_id):
        response = self.session.get(f"{TERABOX_BASE}/wap/share/filelist", params={"surl": share_id, "clearCache": "1"}, timeout=60)
        response.raise_for_status()
        match = re.search(r'fn%28%22([0-9A-Fa-f]+)%22%29', response.text)
        if not match:
            raise RuntimeError("TeraBox did not return a download session token.")
        return match.group(1)

    def _share_info(self, share_id, js_token):
        response = self.session.get(f"{TERABOX_BASE}/api/shorturlinfo", params={
            "app_id": "250528", "shorturl": f"1{share_id}", "root": "1", "web": "1",
            "channel": "dubox", "clienttype": "0", "jsToken": js_token, "t": str(int(time.time())),
        }, timeout=60)
        response.raise_for_status()
        data = response.json()
        if data.get("errno"):
            raise RuntimeError(f"TeraBox share lookup failed (code {data['errno']}).")
        return data

    def first_file(self, share_id):
        response = self.session.get("https://www.terabox.com/share/list", params={
            "app_id": "250528", "web": "1", "channel": "10", "shorturl": share_id, "root": "1",
        }, timeout=60)
        response.raise_for_status()
        files = [item for item in response.json().get("list", []) if str(item.get("isdir")) != "1"]
        if not files:
            raise RuntimeError("No downloadable file was found in this TeraBox share.")
        video_extensions = (".mp4", ".mkv", ".mov", ".avi", ".webm")
        return next((item for item in files if item.get("server_filename", "").lower().endswith(video_extensions)), files[0])

    def download_original(self, share_id, file_id, expected_size, output_path):
        js_token = self._js_token(share_id)
        info = self._share_info(share_id, js_token)
        response = self.session.get(f"{TERABOX_BASE}/share/download", params={
            "app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0", "jsToken": js_token,
            "scene": "purchased_list", "product": "share", "nozip": "0", "shareid": str(info["shareid"]),
            "sign": info["sign"], "timestamp": str(info["timestamp"]), "uk": str(info["uk"]),
            "primaryid": str(info["shareid"]), "fid_list": json.dumps([str(file_id)]),
        }, timeout=60)
        response.raise_for_status()
        data = response.json()
        dlink = data.get("dlink")
        if data.get("errno") or not dlink:
            code = data.get("errno", "missing dlink")
            detail = data.get("errmsg") or data.get("message") or "no additional detail"
            raise RuntimeError(
                f"TeraBox refused the original-file link (code {code}: {detail}). "
                "Sign in to TeraBox and update TERABOX_COOKIE with a fresh ndus cookie."
            )
        written = 0
        with self.session.get(dlink, stream=True, timeout=(60, 600)) as source:
            source.raise_for_status()
            with open(output_path, "wb") as output:
                for chunk in source.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
                        written += len(chunk)
        if expected_size and abs(written - expected_size) > 1024:
            raise FullVideoVerificationError(f"TeraBox returned {written} bytes, but the original is {expected_size} bytes.")
        return written


async def source_video_duration(page):
    """Read the selected video's duration after its metadata becomes available."""
    for _ in range(10):
        durations = await page.locator("video").evaluate_all(
            "videos => videos.map(video => Number(video.duration)).filter(Number.isFinite)"
        )
        valid = [duration for duration in durations if duration > 1]
        if valid:
            return max(valid)
        await page.wait_for_timeout(1000)
    return None


def downloaded_video_duration(path):
    """Return a local media file's duration using the bundled FFmpeg executable."""
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise FullVideoVerificationError("The downloaded file is not a readable video.")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def verify_full_video(path, expected_duration):
    """Reject a short preview before it can be uploaded as a full video."""
    if os.path.getsize(path) <= 1000:
        raise FullVideoVerificationError("The download was empty.")
    actual_duration = downloaded_video_duration(path)
    tolerance = max(5, expected_duration * 0.03)
    if abs(actual_duration - expected_duration) > tolerance:
        raise FullVideoVerificationError(
            "TeraBox returned a preview instead of the full video "
            f"({actual_duration:.0f}s received; {expected_duration:.0f}s expected)."
        )
    return actual_duration


def solve_turnstile(site_url, site_key):
    payload = {
        "clientKey": CAPSOLVER_KEY,
        "task": {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": site_url,
            "websiteKey": site_key,
        },
    }
    r = requests.post(CAPSOLVER_API, json=payload, timeout=30)
    data = r.json()
    if data.get("errorId"):
        raise RuntimeError(f"CapSolver createTask error: {data}")
    task_id = data["taskId"]
    for _ in range(60):
        time.sleep(3)
        res = requests.post(CAPSOLVER_RESULT, json={"clientKey": CAPSOLVER_KEY, "taskId": task_id}, timeout=30)
        rd = res.json()
        if rd.get("status") == "ready":
            return rd["solution"]["token"]
        if rd.get("errorId"):
            raise RuntimeError(f"CapSolver getTaskResult error: {rd}")
    raise TimeoutError("CapSolver timed out")


def find_sitekey(page_content):
    m = re.search(r'data-sitekey=["\']([^"\']+)["\']', page_content)
    if m:
        return m.group(1)
    m = re.search(r'name=["\']cf-turnstile-response["\'][^>]*sitekey=["\']([^"\']+)["\']', page_content)
    if m:
        return m.group(1)
    return None


async def download_full_terabox_video(link, progress_cb, output_path):
    if not COOKIE_JSON:
        return False, "COOKIE_JSON is not configured on Render."
    share_id = extract_share_id(link)
    if not share_id:
        return False, "Couldn't read the TeraBox share ID from this link."
    original_error = None
    try:
        await progress_cb("Resolving the original video...")
        client = TeraBoxDirectDownload(COOKIE_JSON)
        file_info = await asyncio.to_thread(client.first_file, share_id)
        expected_size = int(file_info.get("size") or 0)
        await progress_cb("Downloading the original full video...")
        await asyncio.to_thread(
            client.download_original,
            share_id,
            file_info["fs_id"],
            expected_size,
            output_path,
        )
        return True, "Success"
    except (requests.RequestException, ValueError, KeyError, FullVideoVerificationError, RuntimeError) as error:
        print(f"TeraBox download failed for share {share_id}: {error}")
        original_error = str(error)

    # Some public shares do not authorize the original-file endpoint.  Fall
    # back to the public player stream, and label it honestly as a preview.
    try:
        await progress_cb("Original unavailable; trying the TeraBox preview stream...")
        written = await download_terabox_preview(link, output_path)
        if written <= 1000:
            raise FullVideoVerificationError("The TeraBox preview stream was empty.")
        return True, "Preview"
    except (requests.RequestException, ValueError, FullVideoVerificationError, RuntimeError) as preview_error:
        return False, f"Original: {original_error}. Preview: {preview_error}"


async def download_terabox_preview(link, output_path):
    """Download the public player's direct media stream without bypassing login or challenges."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        )
        page = await context.new_page()
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector("video", timeout=30000)
            sources = await page.locator("video").evaluate_all(
                "videos => videos.map(video => video.currentSrc || video.src).filter(Boolean)"
            )
            source_url = next((url for url in sources if url.startswith(("http://", "https://"))), None)
            if not source_url:
                raise RuntimeError("TeraBox did not expose a downloadable public preview stream.")
            cookies = {cookie["name"]: cookie["value"] for cookie in await context.cookies()}
        finally:
            await browser.close()

    written = 0
    with requests.get(source_url, headers={"Referer": link}, cookies=cookies, stream=True, timeout=(60, 600)) as source:
        source.raise_for_status()
        with open(output_path, "wb") as output:
            for chunk in source.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
                    written += len(chunk)
    return written


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a TeraBox link and I'll download and send you the FULL direct video."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not is_supported_terabox_url(text):
        await update.message.reply_text("Please send a valid link from a supported TeraBox domain.")
        return

    status = await update.message.reply_text("Starting browser...")
    async def progress(msg):
        try:
            await status.edit_text(msg)
        except Exception:
            pass

    file_descriptor, tmp_path = tempfile.mkstemp(suffix=".mp4")
    os.close(file_descriptor)

    try:
        success, result = await download_full_terabox_video(text, progress, tmp_path)
    except Exception as e:
        await progress(f"Browser error: {str(e)[:200]}")
        return

    if not success:
        await progress(f"Couldn't deliver a TeraBox video: {result}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return

    try:
        await progress("Sending preview video..." if result == "Preview" else "Sending full video...")
        file_size = os.path.getsize(tmp_path)
        with open(tmp_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                filename="TeraBox_Full_Video.mp4",
                caption=(f"Here's your TeraBox {'preview' if result == 'Preview' else 'full'} video "
                         f"({file_size // 1024} KB)")
            )
        await status.delete()
    except Exception as e:
        await progress(f"Failed to send video: {str(e)[:200]}")
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def main():
    if not BOT_TOKEN:
        print("Set BOT_TOKEN environment variable")
        return

    start_health_server()

    # Python 3.14 no longer creates a default event loop in the main thread.
    # python-telegram-bot's synchronous run_polling() API still requires one.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("TeraBox Full-Video Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
