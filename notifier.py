"""
SSC Notice Board -> Telegram Auto-Poster
=========================================
Kya karta hai:
1. ssc.gov.in ke Notice Board page ko headless browser (Playwright) se open karta hai
   (SSC ka naya portal Angular/JS pe bana hai, isliye plain requests kaam nahi karta).
2. Saari notices (title + link) nikalta hai.
3. Pichli baar "seen_notices.json" me save kiye gaye notices se compare karta hai.
4. Jo bhi NAYI notice milti hai, use tumhare Telegram channel pe bhej deta hai.
5. seen_notices.json update kar deta hai taaki dobara wahi notice repeat na ho.

Isko GitHub Actions cron job se har 15-20 minute me chalaya jaata hai (bilkul FREE).
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ---------- CONFIG ----------
SSC_NOTICE_URL = "https://ssc.gov.in/home/notice-board"
STATE_FILE = Path("seen_notices.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")  # e.g. "@SSCDIARY"

# CSS selectors - agar site ka layout badal jaaye to sirf yahan change karna hoga.
# Notice board items usually <a> tags hote hain jinke href me "attachment" ya ".pdf" hota hai.
NOTICE_LINK_SELECTOR = "a[href*='attachment'], a[href*='.pdf']"


def load_seen():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_notices():
    """Renders the SSC notice board page and extracts (title, link) pairs."""
    notices = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page.goto(SSC_NOTICE_URL, wait_until="networkidle", timeout=60000)

        # Angular apps render async — give it a moment + wait for at least one link.
        try:
            page.wait_for_selector(NOTICE_LINK_SELECTOR, timeout=20000)
        except Exception:
            print("WARNING: No notice links found with current selector. "
                  "Site layout may have changed — see README to fix.")

        elements = page.query_selector_all(NOTICE_LINK_SELECTOR)
        for el in elements:
            href = el.get_attribute("href") or ""
            text = (el.inner_text() or "").strip()
            if not href or not text:
                continue
            if href.startswith("/"):
                href = "https://ssc.gov.in" + href
            notices.append({"title": text, "link": href})

        browser.close()
    return notices


def build_caption(title, link):
    filename = link.rsplit("/", 1)[-1]
    return (
        f"⚡️〔 <b>SSC ALERT</b> 〕⚡️\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🗂 <b>{title}</b>\n\n"
        f"📄 <code>{filename}</code>\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>Sabse pehle, sabse tez — sirf</i> @SSC_DIARY <i>par</i> 🚀"
    )


def is_file_link(link):
    return link.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")) or "attachment" in link.lower()


def send_text_message(title, link):
    message = (
        f"⚡️〔 <b>SSC ALERT</b> 〕⚡️\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🗂 <b>{title}</b>\n\n"
        f"🔗 {link}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>join the channel for fastest updates</i> @SSC_DIARY <i>par</i> 🚀"
    )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=30)
    if resp.status_code != 200:
        print(f"Telegram sendMessage error: {resp.status_code} {resp.text}")
        return False
    return True


def send_document(title, link):
    """Sends the actual PDF/file into the channel (like SSC4EVER does),
    not just a link. Telegram itself fetches the file from the given URL —
    no bandwidth used on our side. Falls back to downloading + uploading
    manually if Telegram can't fetch the URL directly, and finally falls
    back to a plain text message if the file can't be sent at all."""

    caption = build_caption(title, link)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    # Attempt 1: let Telegram fetch the file itself via URL (fastest, free).
    resp = requests.post(url, data={
        "chat_id": CHANNEL_ID,
        "document": link,
        "caption": caption,
        "parse_mode": "HTML",
    }, timeout=60)

    if resp.status_code == 200:
        return True

    print(f"sendDocument via URL failed ({resp.status_code}): {resp.text}. "
          f"Trying manual download+upload...")

    # Attempt 2: download the file ourselves, then upload as multipart.
    try:
        file_resp = requests.get(link, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        })
        file_resp.raise_for_status()
        filename = link.rsplit("/", 1)[-1] or "notice.pdf"

        resp2 = requests.post(url, data={
            "chat_id": CHANNEL_ID,
            "caption": caption,
            "parse_mode": "HTML",
        }, files={"document": (filename, file_resp.content)}, timeout=60)

        if resp2.status_code == 200:
            return True
        print(f"Manual upload also failed: {resp2.status_code} {resp2.text}")
    except Exception as e:
        print(f"Manual download/upload error: {e}")

    # Attempt 3: fall back to a plain text message with the link.
    print("Falling back to plain text message with link.")
    return send_text_message(title, link)


def send_to_telegram(title, link):
    if not BOT_TOKEN or not CHANNEL_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID not set.")
        return False

    if is_file_link(link):
        return send_document(title, link)
    return send_text_message(title, link)


def main():
    seen = load_seen()
    notices = fetch_notices()

    if not notices:
        print("No notices fetched this run (could be a temporary site issue).")
        sys.exit(0)

    new_count = 0
    for n in notices:
        key = n["link"]  # link as unique id
        if key not in seen:
            print(f"New notice found: {n['title']}")
            ok = send_to_telegram(n["title"], n["link"])
            if ok:
                seen[key] = {"title": n["title"], "notified": True}
                new_count += 1
                time.sleep(2)  # avoid Telegram rate limits

    # Keep state file from growing forever — retain latest 500 entries.
    if len(seen) > 500:
        seen = dict(list(seen.items())[-500:])

    save_seen(seen)
    print(f"Done. {new_count} new notice(s) posted.")


if __name__ == "__main__":
    main()
