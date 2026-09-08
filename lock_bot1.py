import os
import asyncio
import re
import time
import requests
import tempfile
import subprocess
from urllib.parse import urlsplit

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


def is_supported_terabox_url(value):
    """Allow supported TeraBox share and wrapper hosts, never arbitrary URLs."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and bool(host) and any(
        host == domain or host.endswith(f".{domain}") for domain in SUPPORTED_HOSTS
    )


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
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = None
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080}, accept_downloads=True, locale="en-US",
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            """)
            page = await context.new_page()
            await progress_cb("Opening TeraBox link...")
            await page.goto(link, timeout=90000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            if not is_supported_terabox_url(page.url):
                return False, "The link redirected outside supported TeraBox domains."

            for _ in range(3):
                content = await page.content()
                if "moment" not in (await page.title()).lower() and "challenge" not in content.lower():
                    break
                site_key = find_sitekey(content)
                if not site_key:
                    await progress_cb("Waiting for the TeraBox page...")
                    await page.wait_for_timeout(3000)
                    continue
                if not CAPSOLVER_KEY:
                    return False, "TeraBox requires verification, but CAPSOLVER_KEY is not configured."
                await progress_cb("Completing TeraBox verification...")
                token = await asyncio.to_thread(solve_turnstile, page.url, site_key)
                await page.evaluate("""(token) => {
                    const el = document.querySelector('[name="cf-turnstile-response"]');
                    if (el) { el.value = token; el.dispatchEvent(new Event('input', {bubbles:true})); }
                    const forms = document.querySelectorAll('form');
                    forms.forEach(f => { if (f.checkValidity) f.requestSubmit(); });
                }""", token)
                await page.wait_for_timeout(5000)

            await progress_cb("Reading full-video duration...")
            expected_duration = await source_video_duration(page)
            if expected_duration is None:
                return False, "Couldn't verify the source video's duration, so no file was sent."
            await progress_cb("Downloading the verified full video...")
            download_button = page.get_by_role("button", name=re.compile(r"^Download$", re.I))
            if await download_button.count() == 0:
                download_button = page.get_by_role("link", name=re.compile(r"^Download$", re.I))
            if await download_button.count() == 0 or not await download_button.first.is_visible():
                return False, "The full-video Download button was not available."
            async with page.expect_download(timeout=60000) as download_info:
                await download_button.first.click()
            download = await download_info.value
            await download.save_as(output_path)
            await progress_cb("Verifying downloaded video length...")
            await asyncio.to_thread(verify_full_video, output_path, expected_duration)
            return True, "Success"
        except FullVideoVerificationError as error:
            return False, str(error)
        finally:
            if context:
                await context.close()
            await browser.close()


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
        success, err = await download_full_terabox_video(text, progress, tmp_path)
    except Exception as e:
        await progress(f"Browser error: {str(e)[:200]}")
        return

    if not success:
        await progress(f"Couldn't deliver a verified full video: {err}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return

    try:
        await progress("Sending full video...")
        file_size = os.path.getsize(tmp_path)
        with open(tmp_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                filename="TeraBox_Full_Video.mp4",
                caption=f"Here's your full TeraBox video ({file_size // 1024} KB)"
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

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("TeraBox Full-Video Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
