import os
import asyncio
import re
import io
import time
import requests
import tempfile
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
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
            locale="en-US",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()

        await progress_cb("Opening TeraBox link...")
        await page.goto(link, timeout=90000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        for attempt in range(3):
            content = await page.content()
            if "moment" in (await page.title()).lower() or "challenge" in content.lower():
                site_key = find_sitekey(content)
                if not site_key:
                    await progress_cb("Looking for challenge widget...")
                    await page.wait_for_timeout(3000)
                    continue
                await progress_cb("Solving Cloudflare challenge...")
                token = solve_turnstile(page.url, site_key)
                await page.evaluate("""(token) => {
                    const el = document.querySelector('[name="cf-turnstile-response"]');
                    if (el) { el.value = token; el.dispatchEvent(new Event('input', {bubbles:true})); }
                    const forms = document.querySelectorAll('form');
                    forms.forEach(f => { if (f.checkValidity) f.requestSubmit(); });
                }""", token)
                await page.wait_for_timeout(5000)
                if "moment" not in (await page.title()).lower():
                    break
            else:
                break

        await page.wait_for_timeout(3000)
        await progress_cb("Finding download button for full video...")

        downloaded = False
        for _ in range(30):
            await page.wait_for_timeout(2000)
            try:
                dl_btn = page.locator('a:has-text("Download"), button:has-text("Download"), a[download], button[class*="download"], span:has-text("Download")').first
                if await dl_btn.count() > 0 and await dl_btn.is_visible():
                    async with page.expect_download(timeout=60000) as download_info:
                        await dl_btn.click()
                    download = await download_info.value
                    await download.save_as(output_path)
                    downloaded = True
                    break
            except Exception:
                pass

        await browser.close()
        if downloaded and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return True, "Success"
        return False, "Failed to download full video file."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a TeraBox link and I'll download and send you the FULL direct video."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("http"):
        await update.message.reply_text("Please send a valid TeraBox link starting with http/https.")
        return

    status = await update.message.reply_text("Starting browser...")
    async def progress(msg):
        try:
            await status.edit_text(msg)
        except Exception:
            pass

    tmp_path = tempfile.mktemp(suffix=".mp4")

    try:
        success, err = await download_full_terabox_video(text, progress, tmp_path)
    except Exception as e:
        await progress(f"Browser error: {str(e)[:200]}")
        return

    if not success:
        await progress("Couldn't download the full video. The link may be expired or protected.")
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
