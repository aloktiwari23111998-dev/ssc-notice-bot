"""
SSC (ssc.gov.in) Site-Wide -> Telegram Auto-Poster
====================================================

Kya karta hai:
1. SSC ke saare important public pages check karta hai (Notice Board, Results,
   Answer Key, Admit Card, Home) — sirf ek page nahi, poori site.
2. Har page ko headless browser (Playwright) se open karta hai, kyunki SSC ka
   naya portal Angular/JS pe bana hai (plain requests kaam nahi karta).
3. SSC ka Angular app apna data XHR/fetch calls se JSON ke roop me laata hai.
   Ye script har page load ke dauraan saari network responses "sunta" hai,
   unme se JSON body nikaalta hai.
4. SCHEMA-BASED PARSING (no more generic guessing): SSC ke actual API records
   ka real shape ye hai:
       { "headline": "...", "attachments": [
             {"fileName": "...", "path": "...", "type": "...", "documentType": "..."},
             ...
       ] }
   `headline` hamesha Telegram title banta hai. Har `attachments[]` entry se
   ek alag notice banti hai (ek headline ke neeche multiple PDFs ho sakte
   hain, jaise Result + Answer Key dono ek hi record me).
5. `attachments[].path` khud ek public download URL NAHI hai — ye server ka
   internal storage path hai (aur kabhi Windows-style backslashes ke saath
   aata hai). Real SSC notification links (jaise
   ssc.gov.in/api/attachment/uploads/masterData/NoticeBoards/<file>.pdf) se
   confirm hota hai ki asli public endpoint `/api/attachment/<path>` hai —
   is base ko primary candidate banaya gaya hai. Robustness ke liye ek chhoti
   si candidate-URL list try ki jaati hai jab tak koi asli PDF download na
   ho jaaye.
6. Telegram pe upload karne se PEHLE file ko khud download karke validate
   kiya jaata hai (status 200, Content-Type, "%PDF" magic bytes) — koi bhi
   URL jo HTML/error page return kare, wo use nahi hoti.
7. Result/Answer Key jaise pages pe pehle ek <select> dropdown me se exam
   choose karna padta hai tabhi list load hoti hai — script automatically
   har dropdown option try karta hai.
8. DOM-based generic <a href> scanning ek SAFETY NET ke roop me rakha gaya
   hai (JSON schema match na ho paaye tab ke liye).
9. Pichli baar "seen_notices.json" me save kiye gaye se compare karta hai.
10. Jo bhi NAYI cheez milti hai, Telegram channel pe bhej deta hai.

GitHub Actions cron job se chalta hai (bilkul FREE). Env vars, Telegram
functions, seen_notices.json format, aur message format — sab pehle jaisa
hi hai.
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

LINK_SELECTOR = "a[href]"

FILE_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx")

# Confirmed (not guessed) from real SSC published notification links, e.g.
# https://ssc.gov.in/api/attachment/uploads/masterData/NoticeBoards/<file>.pdf
ATTACHMENT_BASE_URL = "https://ssc.gov.in/api/attachment/"

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
    SSC attachment/API path. Used only for the DOM-scan safety net."""
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
    """Turns a relative DOM href into a full https://ssc.gov.in/... URL.
    Also normalizes Windows-style backslashes to forward slashes, since SSC
    occasionally emits them and a literal "\\" in a URL path never resolves."""
    if not link:
        return None
    link = link.strip()
    if not link:
        return None

    link = link.replace("\\", "/")

    if link.startswith("//"):
        return "https:" + link
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if link.startswith("/"):
        return "https://ssc.gov.in" + link
    return "https://ssc.gov.in/" + link.lstrip("./")


def build_candidate_download_urls(path):
    """Builds an ordered list of candidate public download URLs from an
    attachment's raw storage `path`. `path` is NOT itself a public URL, and
    it sometimes uses Windows-style backslashes — both are handled here.

    We don't assume a single fixed prefix; instead we try, in order, the
    forms that are actually known/likely to work, and let the caller
    (send_document) confirm which one really downloads a valid PDF before
    anything gets uploaded to Telegram.
    """
    if not path:
        return []

    normalized = path.replace("\\", "/").strip()
    normalized = normalized.lstrip("/")

    if normalized.lower().startswith("http://") or normalized.lower().startswith("https://"):
        return [normalized]

    candidates = []

    # Candidate 1 (primary): confirmed real-world SSC pattern —
    # https://ssc.gov.in/api/attachment/<path>
    candidates.append(ATTACHMENT_BASE_URL + normalized)

    # Candidate 2: in case `path` already includes an "api/attachment/" or
    # similar prefix segment, or the file is actually served directly off
    # the domain root without going through the attachment API.
    if not normalized.lower().startswith("api/attachment/"):
        candidates.append("https://ssc.gov.in/" + normalized)
    else:
        candidates.append("https://ssc.gov.in/" + normalized)

    # De-duplicate while preserving order.
    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def clean_title(raw_text):
    """Used for DOM-scan titles and to lightly clean the API `headline`."""
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
    check in-memory (does not change seen_notices.json's schema)."""
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return f"{category.lower()}::{t}"


# ================================================================
# JSON API extraction — SCHEMA-BASED (headline + attachments[])
# ================================================================

def looks_like_api_json_response(url, content_type):
    if content_type and "json" in content_type.lower():
        return True
    lowered = url.lower()
    if "/api/" in lowered and not any(lowered.endswith(ext) for ext in FILE_EXTENSIONS):
        return True
    return False


def extract_notice_records(node, found, source_url):
    """Schema-exact extraction. SSC's real API shape is:

        { "headline": "...", "attachments": [
              {"fileName": ..., "path": ..., "type": ..., "documentType": ...},
              ...
        ] }

    We only pull out `headline` and each attachment's `fileName`/`path`/
    `type`/`documentType` — no guessing across alternate field names. We
    still walk the tree (list/dict wrappers like {"data": [...]} are common)
    purely to LOCATE objects matching this exact shape, not to guess field
    names within them.
    """
    if isinstance(node, dict):
        headline = node.get("headline")
        attachments = node.get("attachments")
        if isinstance(headline, str) and headline.strip() and isinstance(attachments, list) and attachments:
            clean_headline = headline.strip()
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                file_name = att.get("fileName")
                path = att.get("path")
                doc_type = att.get("type")
                document_type = att.get("documentType")
                if not file_name or not path:
                    print(f"DEBUG: Skipping attachment with missing fileName/path "
                          f"under headline '{clean_headline[:60]}' "
                          f"(source: {source_url}): {att}")
                    continue
                found.append({
                    "headline": clean_headline,
                    "fileName": file_name,
                    "path": path,
                    "type": doc_type,
                    "documentType": document_type,
                    "_source": source_url,
                })
        for v in node.values():
            if isinstance(v, (dict, list)):
                extract_notice_records(v, found, source_url)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                extract_notice_records(item, found, source_url)


def try_trigger_dropdowns(page, category):
    """Result / Answer Key pages typically need an exam selected from a
    <select> dropdown before the real document list (and its backing API
    call) fires."""
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
    """Generic DOM anchor scan — fallback safety net for when a page's data
    doesn't show up as a headline/attachments JSON record."""
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
        found.append({
            "title": title,
            "link": href,
            "category": category,
            "file_name": href.rsplit("/", 1)[-1],
        })

    return found


def fetch_notices_from_page(page, category, url):
    """Renders one SSC page. Primary source: intercepted XHR/fetch JSON
    responses, parsed via the exact headline+attachments schema. Secondary
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
            extract_notice_records(data, found_from_api, req_url)
        except Exception as e:
            print(f"DEBUG: [{category}] response-handler error: {e}")

    page.on("response", handle_response)
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

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

    # ---- One notice per attachment, title = headline, filename = fileName ----
    api_notices = []
    for rec in found_from_api:
        candidates = build_candidate_download_urls(rec["path"])
        if not candidates:
            print(f"DEBUG: [{category}] Could not build any download URL from "
                  f"path='{rec['path']}' (headline='{rec['headline'][:60]}') — skipping.")
            continue
        # We key/track this notice by its primary candidate URL; the actual
        # working URL is re-confirmed at send time in send_document().
        primary_link = candidates[0]
        api_notices.append({
            "title": rec["headline"],
            "link": primary_link,
            "download_candidates": candidates,
            "category": category,
            "file_name": rec["fileName"],
            "doc_type": rec.get("type"),
            "document_type": rec.get("documentType"),
        })

    dom_notices = scan_dom_links(page, category)
    for n in dom_notices:
        n.setdefault("download_candidates", [n["link"]])

    combined = {}
    for n in api_notices + dom_notices:
        combined.setdefault(n["link"], n)
    result = list(combined.values())

    print(f"DEBUG: [{category}] {len(api_notices)} from JSON API (schema-matched) + "
          f"{len(dom_notices)} from DOM scan = {len(result)} unique notice-like item(s).")
    for i, n in enumerate(result[:10]):
        print(f"DEBUG [{category}][{i}] headline='{n['title'][:80]}' "
              f"fileName='{n.get('file_name')}' -> {n['link']}")

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
            time.sleep(1)

        browser.close()

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
# TELEGRAM POSTING
# ================================================================

def build_caption(category, title, file_name):
    return (
        f"⚡️〔 <b>SSC ALERT</b> 〕⚡️\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>{category}</b>\n\n"
        f"🗂 <b>{title}</b>\n\n"
        f"📄 <code>{file_name}</code>\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>JOIN — </i> @SSC_DIARY <i>par</i> 🚀"
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
        f"<i>Join-</i> @SSC_DIARY <i>par</i> 🚀"
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


def validate_downloaded_file(file_resp, link, file_name):
    """Confirms a downloaded file is a real document and not an HTML
    error/login page disguised behind a 200 status. Checks:
      1. HTTP status == 200
      2. Content-Type == application/pdf (only enforced when the file is
         expected to be a PDF, per its extension)
      3. First bytes == "%PDF" (only enforced for expected PDFs)
    Logs the invalid response body (truncated) whenever a check fails.
    """
    if file_resp.status_code != 200:
        print(f"VALIDATION FAILED: [{link}] HTTP status = {file_resp.status_code} (expected 200).")
        _log_invalid_body(file_resp, link)
        return False

    expected_pdf = link.lower().endswith(".pdf") or (file_name or "").lower().endswith(".pdf")
    if expected_pdf:
        content_type = file_resp.headers.get("Content-Type", "")
        if "application/pdf" not in content_type.lower():
            print(f"VALIDATION FAILED: [{link}] Content-Type = '{content_type}' "
                  f"(expected 'application/pdf').")
            _log_invalid_body(file_resp, link)
            return False

        content_start = file_resp.content[:4]
        if content_start != b"%PDF":
            print(f"VALIDATION FAILED: [{link}] File does not start with '%PDF' magic "
                  f"bytes (got {content_start!r}) — looks like an HTML/error page, "
                  f"not a real PDF.")
            _log_invalid_body(file_resp, link)
            return False

    return True


def _log_invalid_body(file_resp, link):
    try:
        body_preview = file_resp.text[:500]
    except Exception:
        body_preview = repr(file_resp.content[:500])
    print(f"VALIDATION DEBUG: [{link}] response body (truncated) -> {body_preview!r}")


def send_document(category, title, link, file_name=None, download_candidates=None):
    """Downloads the actual file ourselves FIRST, validates it, and only
    then uploads it to Telegram. We never hand SSC's raw path/URL straight
    to Telegram's sendDocument-by-URL, since an unresolved/incorrect
    download URL would silently upload an HTML error page as if it were
    the PDF.

    `download_candidates` is an ordered list of URLs to try (built from the
    attachment's schema `path`); the first one that downloads a validated
    real file wins.
    """
    candidates = download_candidates or [link]
    file_name = file_name or link.rsplit("/", 1)[-1] or "notice.pdf"
    caption = build_caption(category, title, file_name)
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    for candidate_url in candidates:
        print(f"DEBUG: [{category}] Trying download candidate: {candidate_url}")
        try:
            file_resp = requests.get(candidate_url, timeout=60, headers={
                "User-Agent": USER_AGENT
            })
        except Exception as e:
            print(f"DEBUG: [{category}] Download error for {candidate_url}: {e}")
            continue

        if not validate_downloaded_file(file_resp, candidate_url, file_name):
            print(f"DEBUG: [{category}] Candidate failed validation, trying next "
                  f"if available: {candidate_url}")
            continue

        # Got a real, validated file — upload it with the schema's fileName.
        try:
            resp = requests.post(telegram_url, data={
                "chat_id": CHANNEL_ID,
                "caption": caption,
                "parse_mode": "HTML",
            }, files={"document": (file_name, file_resp.content)}, timeout=60)
            if resp.status_code == 200:
                print(f"DEBUG: [{category}] Successfully uploaded '{file_name}' "
                      f"from {candidate_url}")
                return True
            print(f"Telegram upload failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"Telegram upload error: {e}")

    print(f"WARNING: [{category}] All download candidates failed for headline "
          f"'{title}' (fileName='{file_name}'). Falling back to plain text message.")
    return send_text_message(category, title, link)


def send_to_telegram(category, title, link, file_name=None, download_candidates=None):
    if not BOT_TOKEN or not CHANNEL_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID not set.")
        return False
    if is_file_link(link):
        return send_document(category, title, link, file_name=file_name,
                              download_candidates=download_candidates)
    return send_text_message(category, title, link)


# ================================================================
# MAIN
# ================================================================

def main():
    seen = load_seen()
    print(f"DEBUG: {len(seen)} notices already marked as seen from previous runs.")

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
            print(f"DEBUG: Skipping likely duplicate (same headline/category, "
                  f"different link) [{n['category']}]: {n['title']}")
            seen[key] = {"title": n["title"], "category": n["category"], "notified": True}
            continue

        print(f"New notice found [{n['category']}]: {n['title']} "
              f"(fileName={n.get('file_name')}) -> {n['link']}")
        ok = send_to_telegram(
            n["category"], n["title"], n["link"],
            file_name=n.get("file_name"),
            download_candidates=n.get("download_candidates"),
        )
        if ok:
            seen[key] = {"title": n["title"], "category": n["category"], "notified": True}
            seen_signatures.add(sig)
            new_count += 1
        else:
            print(f"WARNING: Failed to send notice [{n['category']}]: {n['title']}")
        time.sleep(2)

    if len(seen) > 800:
        seen = dict(list(seen.items())[-800:])

    save_seen(seen)
    print(f"Done. {new_count} new notice(s) posted.")


if __name__ == "__main__":
    main()
