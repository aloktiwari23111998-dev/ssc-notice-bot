"""
SSC (ssc.gov.in) Site-Wide -> Telegram Auto-Poster
====================================================
Kya karta hai:
1. SSC ke saare important public pages check karta hai (Notice Board, Results,
   Answer Key, Admit Card, Home) — sirf ek page nahi, poori site.
2. Har page ko headless browser (Playwright) se open karta hai, kyunki SSC ka
   naya portal Angular/JS pe bana hai (plain requests kaam nahi karta).
3. Har page pe jitne bhi "real" links/notices milte hain (chhote nav/menu
   links generic filter se hat jaate hain), unhe collect karta hai.
4. Pichli baar "seen_notices.json" me save kiye gaye se compare karta hai.
5. Jo bhi NAYI cheez milti hai (kisi bhi page pe), Telegram channel pe bhej
   deta hai — file/PDF ho to file bhejta hai, warna text+link bhejta hai.
6. seen_notices.json update kar deta hai taaki dobara repeat na ho.

GitHub Actions cron job se har 5 minute me chalta hai (bilkul FREE).
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ---------- CONFIG ----------

# SSC ke saare important public-facing pages jo monitor karne hain.
# Naya page add karna ho to bas yaha ek aur line daal do.
PAGES_TO_MONITOR = {
    "Notice Board": "https://ssc.gov.in/home/notice-board",
    "Result": "https://ssc.gov.in/home/candidate-result",
    "Answer Key": "https://ssc.gov.in/home/answer-key",
    "Admit Card": "https://ssc.gov.in/home/admit-card",
    "Home": "https://ssc.gov.in/",
}

STATE_FILE = Path("seen_notices.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")  # e.g. "@SSC_DIARY"

# Generic selector — har <a href> pakadta hai. Filtering (nav/menu wale chhote
# links hatana) niche `is_probably_a_notice()` function me hoti hai, taaki
# sirf CSS class/structure pe depend na rehna pade (jo kabhi bhi badal sakta
# hai). Isse system "generic" ban jaata hai — site ka koi bhi naya section
# ho, agar usme <a> tag me thoda lamba text hai, wo pakड़ liya jaayega.
LINK_SELECTOR = "a[href]"

MIN_TITLE_LENGTH = 20  # isse chhoti text wale links (menu items) ignore honge

# Ye words jinke saath link ka text SHURU hota hai wo generic nav/menu/footer
# links hote hain, inhe hamesha ignore karo chahe text lamba bhi ho.
IGNORE_PREFIXES = (
    "home", "login", "register", "contact", "sitemap", "privacy",
    "terms", "disclaimer", "rti", "tender", "organisation", "about",
    "candidate portal", "faq", "helpdesk", "grievance",
)


def load_seen():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def is_probably_a_notice(text, href):
    """Generic filter: decides if a link is likely a real notice/update,
    vs a navigation/menu/footer link. No hardcoded page-specific selectors."""
    if not text or not href:
        return False
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    if len(text) < MIN_TITLE_LENGTH:
        return False
    lowered = text.strip().lower()
    if lowered.startswith(IGNORE_PREFIXES):
        return False
    return True


def fetch_notices_from_page(page, category, url):
    """Renders one SSC page and extracts (title, link) pairs generically."""
    found = []
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        # Angular apps render async — give it a moment.
        try:
            page.wait_for_selector(LINK_SELECTOR, timeout=20000)
        except Exception:
            print(f"WARNING: [{category}] No links found at all — page may "
                  f"have failed to load or layout changed.")
            return found

        elements = page.query_selector_all(LINK_SELECTOR)
        for el in elements:
            href = el.get_attribute("href") or ""
            text = (el.inner_text() or "").strip().replace("\n", " ")
            if not is_probably_a_notice(text, href):
                continue
            if href.startswith("/"):
                href = "https://ssc.gov.in" + href
            found.append({"title": text, "link": href, "category": category})
    except Exception as e:
        print(f"WARNING: [{category}] Failed to load {url}: {e}")

    return found


def fetch_all_notices():
    """Visits every page in PAGES_TO_MONITOR and collects notices from all of them."""
    all_notices = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))

        for category, url in PAGES_TO_MONITOR.items():
            page_notices = fetch_notices_from_page(page, category, url)
            print(f"DEBUG: [{category}] {len(page_notices)} notice-like links found.")
            for i, n in enumerate(page_notices[:10]):
                print(f"DEBUG   [{category}][{i}] {n['title'][:80]} -> {n['link']}")
            all_notices.extend(page_notices)
            time.sleep(1)  # be polite between page loads

        browser.close()

    # De-duplicate by link (same notice can appear on multiple pages, e.g. Home + Notice Board)
    deduped = {}
    for n in all_notices:
        deduped.setdefault(n["link"], n)

    return list(deduped.values())


def build_caption(category, title, link):
    filename = link.rsplit("/", 1)[-1]
    return (
        f"⚡️〔 <b>SSC ALERT</b> 〕⚡️\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>{category}</b>\n\n"
        f"🗂 <b>{title}</b>\n\n"
        f"📄 <code>{filename}</code>\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>Sabse pehle, sabse tez — sirf</i> @SSC_DIARY <i>par</i> 🚀"
    )


def is_file_link(link):
    return link.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")) or "attachment" in link.lower()


def send_text_message(category, title, link):
    message = (
        f"⚡️〔 <b>SSC ALERT</b> 〕⚡️\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>{category}</b>\n\n"
        f"🗂 <b>{title}</b>\n\n"
        f"🔗 {link}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>Sabse pehle, sabse tez — sirf</i> @SSC_DIARY <i>par</i> 🚀"
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


def send_document(category, title, link):
    """Sends the actual PDF/file into the channel, not just a link."""
    caption = build_caption(category, title, link)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

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

    print("Falling back to plain text message with link.")
    return send_text_message(category, title, link)


def send_to_telegram(category, title, link):
    if not BOT_TOKEN or not CHANNEL_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID not set.")
        return False

    if is_file_link(link):
        return send_document(category, title, link)
    return send_text_message(category, title, link)


def main():
    seen = load_seen()
    print(f"DEBUG: {len(seen)} notices already marked as seen from previous runs.")

    notices = fetch_all_notices()
    print(f"DEBUG: Total unique notice-like links across all pages = {len(notices)}")

    if not notices:
        print("No notices fetched this run (could be a temporary site issue).")
        sys.exit(0)

    new_count = 0
    for n in notices:
        key = n["link"]
        if key not in seen:
            print(f"New notice found [{n['category']}]: {n['title']}")
            ok = send_to_telegram(n["category"], n["title"], n["link"])
            if ok:
                seen[key] = {"title": n["title"], "category": n["category"], "notified": True}
                new_count += 1
                time.sleep(2)  # avoid Telegram rate limits

    if len(seen) > 800:
        seen = dict(list(seen.items())[-800:])

    save_seen(seen)
    print(f"Done. {new_count} new notice(s) posted.")


if __name__ == "__main__":
    main()
