"""
SSC (ssc.gov.in) Site-Wide -> Telegram Auto-Poster
====================================================

Kya karta hai:
1. SSC ke saare important public pages check karta hai (Notice Board, Results,
   Answer Key, Admit Card, Home) — sirf ek page nahi, poori site.
2. Har page ko headless browser (Playwright) se open karta hai, kyunki SSC ka
   naya portal Angular/JS pe bana hai (plain requests kaam nahi karta).
3. NAYA (root-cause fix): SSC ka Angular app apna data XHR/fetch calls se JSON
   ke roop me laata hai (na ki plain HTML se). Ye script ab har page load ke
   dauraan saari network responses "sunta" hai, unme se JSON body nikaalta
   hai, aur usme se title+link jaisi cheezein generically extract karta hai.
   Isse Result/Answer Key jaise pages bhi sahi se detect hote hain, jinki
   asli list ek API call ke baad aati hai — na ki seedha DOM me.
4. Result/Answer Key jaise pages pe pehle ek <select> dropdown me se exam
   choose karna padta hai tabhi list load hoti hai. Script ab automatically
   har dropdown option try karta hai taaki wo API call trigger ho aur data
   mil sake (isi wajah se JE Result jaisi cheezein pehle miss ho rahi thi).
5. DOM-based generic <a href> scanning bhi bas ek SAFETY NET ke roop me rakha
   gaya hai — agar kisi page pe XHR intercept na ho paaye to purana tareeka
   bhi chalta rahega. Dono sources ko merge karke duplicate hata diya jaata
   hai.
6. Pichli baar "seen_notices.json" me save kiye gaye se compare karta hai
   (link ke saath-saath normalized title+category signature se bhi, taaki
   ek hi notice do alag URL se do baar post na ho).
7. Jo bhi NAYI cheez milti hai (kisi bhi page pe), Telegram channel pe bhej
   deta hai — file/PDF ho to file bhejta hai, warna text+link bhejta hai.
8. seen_notices.json update kar deta hai taaki dobara repeat na ho.

GitHub Actions cron job se chalta hai (bilkul FREE). Env vars, Telegram
functions, seen_notices.json format, aur message format — sab pehle jaisa
hi hai, sirf fetching logic naye sirey se likhi gayi hai.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ================================================================
# CONFIG
# ================================================================

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

# Generic anchor selector — used only as the DOM fallback scan.
LINK_SELECTOR = "a[href]"

# SIRF real document/notice links pakadte hain — PDF, Word, Excel, ya SSC ke
# apne "attachment" API se aane wale links. Ye same filter API-derived aur
# DOM-derived, dono tarah ke links pe lagta hai.
FILE_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx")

# SSC ke alag-alag modules (Notice Board / Result / Answer Key / Admit Card)
# apne JSON responses me thoda alag field-naming use karte hain. Isliye hum
# ek broad set of "possible" title/link keys check karte hain — jo bhi record
# me in me se ek title-key aur ek link-key dono mil jaayein, use hum ek
# candidate-notice maan lete hain.
TITLE_KEYS = (
    "title", "name", "heading", "subject", "noticeTitle", "resultTitle",
    "examName", "fileName", "description", "displayName", "docTitle",
)
LINK_KEYS = (
    "link", "url", "file", "filePath", "fileUrl", "path", "attachment",
    "pdf", "pdfUrl", "documentPath", "docPath", "href",
)

SKIP_LINE_PATTERNS = [
    r"^new$",
    r"^\(\d+(\.\d+)?\s*(KB|MB)\)$",
    r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)$",
    r"^\d{1,2}$",
    r"^\d{4}$",
    r"^pdf$",
    r"^view$",
    r"^preview$",
    r"^download$",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ================================================================
# SMALL HELPERS (link filtering, title cleanup, url normalization)
# ================================================================

def is_probably_document_link(href):
    """Checks if a link points to an actual document (PDF/Word/Excel) or the
    SSC attachment/API path. Works for both DOM hrefs and links pulled out
    of JSON API responses."""
    if not href:
        return False
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    href_lower = href.lower()
    return (
        href_lower.endswith(FILE_EXTENSIONS)
        or "attachment" in href_lower
        or "/api/" in href_lower
    )


def absolutize(link):
    """Turns a relative API/DOM link into a full https://ssc.gov.in/... URL."""
    if not link:
        return None
    link = link.strip()
    if not link:
        return None
    if link.startswith("//"):
        return "https:" + link
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if link.startswith("/"):
        return "https://ssc.gov.in" + link
    return "https://ssc.gov.in/" + link.lstrip("./")


def clean_title(raw_text):
    """SSC's DOM notice title often sits as plain text next to a PDF icon,
    not inside the <a> tag itself. This strips out 'New' badges, date
    fragments, and file-size text, leaving just the actual notice title.
    Also used to lightly clean titles coming from JSON API responses."""
    if not raw_text:
        return ""
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    kept = []
    for line in lines:
        if any(re.match(p, line, re.IGNORECASE) for p in SKIP_LINE_PATTERNS):
            continue
        kept.append(line)
    return " ".join(kept).strip()


def normalize_signature(title, category):
    """A loose fingerprint of a notice, used ONLY as an extra duplicate
    check in-memory (does not change seen_notices.json's schema). Helps
    catch the same notice appearing under two slightly different URLs
    (e.g. once from the API, once from a DOM anchor)."""
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return f"{category.lower()}::{t}"


# ================================================================
# JSON API extraction (the actual root-cause fix)
# ================================================================

def looks_like_api_json_response(url, content_type):
    if content_type and "json" in content_type.lower():
        return True
    lowered = url.lower()
    if "/api/" in lowered and not any(lowered.endswith(ext) for ext in FILE_EXTENSIONS):
        return True
    return False


def extract_records_from_json(node, found, source_url):
    """Recursively walks ANY parsed JSON structure (dict/list, whatever
    shape SSC's API happens to return) and pulls out anything that looks
    like a notice/result/notification record: something with a title-like
    field AND a link-like field. This is intentionally generic so it keeps
    working even if SSC changes field names slightly."""
    if isinstance(node, dict):
        title_val = None
        link_val = None
        for k in TITLE_KEYS:
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                title_val = v.strip()
                break
        for k in LINK_KEYS:
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                link_val = v.strip()
                break
        if title_val and link_val:
            found.append({"title": title_val, "link": link_val, "_source": source_url})
        for v in node.values():
            if isinstance(v, (dict, list)):
                extract_records_from_json(v, found, source_url)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                extract_records_from_json(item, found, source_url)


def try_trigger_dropdowns(page, category):
    """Result / Answer Key pages typically need an exam selected from a
    <select> dropdown before the real document list (and its backing API
    call) fires. This is the concrete reason things like 'JE Result' were
    being missed. We try every real option (skipping placeholders like
    'Select Exam') and give the page time to fire its XHR after each pick."""
    try:
        selects = page.query_selector_all("select")
    except Exception as e:
        print(f"DEBUG: [{category}] could not query <select> elements: {e}")
        return

    if not selects:
        return

    print(f"DEBUG: [{category}] Found {len(selects)} dropdown(s), trying options...")
    for sel_index, sel in enumerate(selects):
        try:
            options = sel.query_selector_all("option")
        except Exception:
            continue

        option_values = []
        for opt in options:
            try:
                val = opt.get_attribute("value")
                text = (opt.inner_text() or "").strip()
            except Exception:
                continue
            if not val or not text:
                continue
            if re.search(r"select|choose|--|please", text, re.IGNORECASE):
                continue
            option_values.append(val)

        # Safety cap so one page can't blow up run time / rate limits.
        for val in option_values[:12]:
            try:
                sel.select_option(value=val)
                page.wait_for_timeout(1200)
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as e:
                print(f"DEBUG: [{category}] dropdown[{sel_index}] option '{val}' "
                      f"trigger skipped: {e}")
                continue


def scan_dom_links(page, category):
    """Original generic DOM anchor scan — kept as a fallback safety net in
    case a document is server-rendered and never shows up in an XHR/JSON
    response the interceptor sees."""
    found = []
    try:
        elements = page.query_selector_all(LINK_SELECTOR)
    except Exception as e:
        print(f"DEBUG: [{category}] DOM scan failed: {e}")
        return found

    for el in elements:
        try:
            href = el.get_attribute("href") or ""
        except Exception:
            continue
        if not is_probably_document_link(href):
            continue
        href = absolutize(href)
        if not href:
            continue

        try:
            raw_row_text = el.evaluate("""
                (node) => {
                    let el = node;
                    for (let i = 0; i < 5; i++) {
                        if (!el.parentElement) break;
                        el = el.parentElement;
                        const t = el.innerText ? el.innerText.trim() : '';
                        if (t.length > 25) return t;
                    }
                    return el && el.innerText ? el.innerText.trim() : '';
                }
            """)
        except Exception:
            raw_row_text = ""

        title = clean_title(raw_row_text or "")
        if len(title) < 8:
            continue
        found.append({"title": title, "link": href, "category": category})

    return found


def fetch_notices_from_page(page, category, url):
    """Renders one SSC page. Primary source: intercepted XHR/fetch JSON
    responses (this is the real API data SSC's Angular app uses). Secondary
    source: generic DOM anchor scan, merged in as a fallback."""
    found_from_api = []
    discovered_api_urls = set()

    def handle_response(response):
        try:
            req_url = response.url
            ctype = ""
            try:
                ctype = response.headers.get("content-type", "")
            except Exception:
                pass
            if not looks_like_api_json_response(req_url, ctype):
                return
            try:
                data = response.json()
            except Exception:
                try:
                    data = json.loads(response.text())
                except Exception:
                    return
            discovered_api_urls.add(req_url)
            extract_records_from_json(data, found_from_api, req_url)
        except Exception as e:
            print(f"DEBUG: [{category}] response-handler error: {e}")

    page.on("response", handle_response)
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)  # let any late XHRs settle

        try_trigger_dropdowns(page, category)

    except Exception as e:
        print(f"WARNING: [{category}] Failed to load {url}: {e}")
    finally:
        try:
            page.remove_listener("response", handle_response)
        except Exception:
            pass

    if discovered_api_urls:
        print(f"DEBUG: [{category}] Discovered {len(discovered_api_urls)} JSON API "
              f"endpoint(s) while loading this page:")
        for u in sorted(discovered_api_urls):
            print(f"DEBUG: [{category}]   API -> {u}")
    else:
        print(f"DEBUG: [{category}] No JSON API responses observed — "
              f"relying on DOM scan only for this page.")

    # ---- Normalize + filter records coming from the JSON responses ----
    api_notices = []
    for rec in found_from_api:
        link = absolutize(rec.get("link"))
        if not link or not is_probably_document_link(link):
            continue
        title = clean_title(rec.get("title", ""))
        if len(title) < 4:
            continue
        api_notices.append({"title": title, "link": link, "category": category})

    # ---- DOM fallback ----
    dom_notices = scan_dom_links(page, category)

    combined = {}
    for n in api_notices + dom_notices:
        combined.setdefault(n["link"], n)
    result = list(combined.values())

    print(f"DEBUG: [{category}] {len(api_notices)} from JSON API + "
          f"{len(dom_notices)} from DOM scan = {len(result)} unique notice-like item(s).")
    for i, n in enumerate(result[:10]):
        print(f"DEBUG [{category}][{i}] {n['title'][:80]} -> {n['link']}")

    return result


def fetch_all_notices():
    """Visits every page in PAGES_TO_MONITOR and collects notices from all of them."""
    all_notices = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        for category, url in PAGES_TO_MONITOR.items():
            print(f"DEBUG: ---- Checking [{category}] -> {url} ----")
            page_notices = fetch_notices_from_page(page, category, url)
            all_notices.extend(page_notices)
            time.sleep(1)  # be polite between page loads

        browser.close()

    # De-duplicate by link (same notice can appear on multiple pages, e.g. Home + Notice Board)
    deduped = {}
    for n in all_notices:
        deduped.setdefault(n["link"], n)
    final = list(deduped.values())
    print(f"DEBUG: Grand total across all pages: {len(all_notices)} raw, "
          f"{len(final)} unique after link-based de-dup.")
    return final


# ================================================================
# STATE (seen_notices.json) — schema unchanged
# ================================================================

def load_seen():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


# ================================================================
# TELEGRAM POSTING — UNCHANGED
# ================================================================

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
            "User-Agent": USER_AGENT
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


# ================================================================
# MAIN
# ================================================================

def main():
    seen = load_seen()
    print(f"DEBUG: {len(seen)} notices already marked as seen from previous runs.")

    # Extra in-memory duplicate guard (does not change seen_notices.json's
    # schema) — catches the same notice reachable via two different URLs
    # (e.g. once via the JSON API, once via a DOM anchor).
    seen_signatures = set()
    for entry in seen.values():
        if isinstance(entry, dict) and "title" in entry and "category" in entry:
            seen_signatures.add(normalize_signature(entry["title"], entry["category"]))

    notices = fetch_all_notices()
    print(f"DEBUG: Total unique notice-like links across all pages = {len(notices)}")

    if not notices:
        print("No notices fetched this run (could be a temporary site issue).")
        sys.exit(0)

    new_count = 0
    for n in notices:
        key = n["link"]
        sig = normalize_signature(n["title"], n["category"])

        if key in seen:
            continue
        if sig in seen_signatures:
            print(f"DEBUG: Skipping likely duplicate (same title/category, "
                  f"different link) [{n['category']}]: {n['title']}")
            seen[key] = {"title": n["title"], "category": n["category"], "notified": True}
            continue

        print(f"New notice found [{n['category']}]: {n['title']} -> {n['link']}")
        ok = send_to_telegram(n["category"], n["title"], n["link"])
        if ok:
            seen[key] = {"title": n["title"], "category": n["category"], "notified": True}
            seen_signatures.add(sig)
            new_count += 1
        else:
            print(f"WARNING: Failed to send notice [{n['category']}]: {n['title']}")
        time.sleep(2)  # avoid Telegram rate limits

    if len(seen) > 800:
        seen = dict(list(seen.items())[-800:])

    save_seen(seen)
    print(f"Done. {new_count} new notice(s) posted.")


if __name__ == "__main__":
    main()
