bash
cat > /home/claude/notifier_new.py << 'PYEOF'
"""
SSC (ssc.gov.in) + DSSSB (dsssb.delhi.gov.in) -> Telegram Auto-Poster
====================================================

SSC SECTION -- PRODUCTION CODE, LOGIC UNCHANGED FROM THE VERSION SUPPLIED.
Kya karta hai:
1. SSC ke saare important public pages check karta hai (Notice Board, Results,
   Answer Key, Admit Card, Home) — sirf ek page nahi, poori site.
2. Har page ko headless browser (Playwright) se open karta hai, kyunki SSC ka
   naya portal Angular/JS pe bana hai (plain requests kaam nahi karta).
3. SSC ka Angular app apna data XHR/fetch calls se JSON ke roop me laata hai.
   Ye script har page load ke dauraan saari network responses "sunta" hai,
   unme se JSON body nikaalta hai.
4. SCHEMA-BASED PARSING: SSC ke actual API records ka real shape ye hai:
       { "headline": "...", "attachments": [
             {"fileName": "...", "path": "...", "type": "...", "documentType": "..."},
             ...
       ] }
   `headline` hamesha Telegram title banta hai. Har `attachments[]` entry se
   ek alag notice banti hai.
5. `attachments[].path` khud ek public download URL NAHI hai — candidate URLs
   ki ek chhoti list banayi jaati hai, aur send-time pe verify hoti hai.
6. Telegram pe upload karne se PEHLE file ko khud download karke validate
   kiya jaata hai (status 200, Content-Type, "%PDF" magic bytes).
7. Result/Answer Key jaise pages pe dropdown se exam choose karna padta hai
   -- script automatically har dropdown option try karta hai.
8. DOM-based generic <a href> scanning ek SAFETY NET ke roop me rakha gaya
   hai.
9. Pichli baar "seen_notices.json" me save kiye gaye se compare karta hai.
10. Jo bhi NAYI cheez milti hai, Telegram channel pe bhej deta hai.

DSSSB SECTION (naya):
- API-first, DOM-fallback (SSC jaisa hi philosophy) -- agar DSSSB kabhi
  JSON API expose kare to wo generically detect ho jaayegi, warna DOM-row
  extraction chalta hai.
- Auto-discovery: DSSSB ke home page ke internal links scan karke naye
  listing-pages (jo DSSSB_PAGES me hardcoded nahi hain) khud dhoondh leta
  hai -- isse site me naya section add hone par bhi code change ki
  zaroorat kam padti hai.
- Single shared Chromium: main() ab EK HI browser+page banata hai jo SSC
  aur DSSSB dono use karte hain -- do alag Chromium processes kabhi nahi
  chalte.
- Ek shared `requests.Session()` (SESSION) Telegram calls + file downloads
  dono ke liye reuse hoti hai (connection pooling).
- Duplicate detection ab do signals use karta hai: (1) title+category
  fingerprint (pehle jaisa), (2) file_name+category+source fingerprint
  (naya) -- isse agar same file do baar alag URL se dikhe to bhi dobara
  post nahi hoga.

Playwright browser CACHING (taaki har run pe Chromium dobara download na
ho) infra-level pe handled hai: GitHub Actions workflow me
`actions/cache@v4` (~/.cache/ms-playwright) already configured hai, aur
Oracle VM pe ye cache disk pe persist karta hai (VM ephemeral nahi hoti),
isliye notifier.py ke andar kuch alag se karne ki zaroorat nahi hai.

GitHub Actions cron job se chalta hai (bilkul FREE). Env vars, Telegram
functions, seen_notices.json format (values), aur message format — sab
pehle jaisa hi hai.
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

# Ek hi requests.Session() SSC + DSSSB dono ke document-downloads aur
# Telegram API calls ke liye reuse hoti hai (connection pooling, thoda
# fast + kam overhead). Behavior requests.get/post jaisa hi hai.
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


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


def absolutize(link, base_domain="https://ssc.gov.in"):
    """Turns a relative DOM href into a full https://<base_domain>/... URL.
    Also normalizes Windows-style backslashes to forward slashes.

    `base_domain` defaults to SSC's domain so every EXISTING SSC call site
    (calling absolutize(href) with just one argument) behaves 100%
    identically to before. DSSSB's code passes base_domain=DSSSB_BASE."""
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
        return base_domain + link
    return base_domain + "/" + link.lstrip("./")


def build_candidate_download_urls(path):
    """Builds an ordered list of candidate public download URLs from an
    attachment's raw storage `path`."""
    if not path:
        return []

    normalized = path.replace("\\", "/").strip()
    normalized = normalized.lstrip("/")

    if normalized.lower().startswith("http://") or normalized.lower().startswith("https://"):
        return [normalized]

    candidates = []
    candidates.append(ATTACHMENT_BASE_URL + normalized)
    if not normalized.lower().startswith("api/attachment/"):
        candidates.append("https://ssc.gov.in/" + normalized)
    else:
        candidates.append("https://ssc.gov.in/" + normalized)

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
    """A loose fingerprint of a notice (title+category), used as an extra
    duplicate check in-memory."""
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return f"{category.lower()}::{t}"


def normalize_filename_signature(source, category, file_name):
    """Second duplicate-check signal: same file (by name) under the same
    source+category, even if it showed up at a different URL. Catches
    cases the link-based and title-based checks might both miss (e.g. a
    site re-uploading the identical PDF under a new path)."""
    if not file_name:
        return None
    fn = file_name.strip().lower()
    if not fn:
        return None
    return f"{source}::{category.lower()}::{fn}"


# ================================================================
# JSON API extraction — SCHEMA-BASED (headline + attachments[]) -- SSC
# ================================================================

def looks_like_api_json_response(url, content_type):
    if content_type and "json" in content_type.lower():
        return True
    lowered = url.lower()
    if "/api/" in lowered and not any(lowered.endswith(ext) for ext in FILE_EXTENSIONS):
        return True
    return False


def extract_notice_records(node, found, source_url):
    """Schema-exact extraction for SSC's { headline, attachments[] } shape."""
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
    <select> dropdown before the real document list fires."""
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
    """Generic DOM anchor scan — fallback safety net for SSC."""
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
    """Renders one SSC page. Primary: intercepted XHR/fetch JSON responses
    (schema-based). Secondary: generic DOM anchor scan, merged as fallback."""
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

    api_notices = []
    for rec in found_from_api:
        candidates = build_candidate_download_urls(rec["path"])
        if not candidates:
            print(f"DEBUG: [{category}] Could not build any download URL from "
                  f"path='{rec['path']}' (headline='{rec['headline'][:60]}') — skipping.")
            continue
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


def fetch_all_notices(shared_page=None):
    """Visits every page in PAGES_TO_MONITOR and collects notices.

    `shared_page` lets the caller (main()) pass in an already-open
    Playwright page so SSC and DSSSB share ONE Chromium instance instead
    of each opening their own. When called with no argument (the original
    signature), behavior is 100% identical to before -- it opens and
    closes its own browser, exactly as it always did."""
    all_notices = []

    def _run(page):
        for category, url in PAGES_TO_MONITOR.items():
            print(f"DEBUG: ---- Checking [{category}] -> {url} ----")
            page_notices = fetch_notices_from_page(page, category, url)
            all_notices.extend(page_notices)
            time.sleep(1)

    if shared_page is not None:
        _run(shared_page)
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=USER_AGENT)
            _run(page)
            browser.close()

    deduped = {}
    for n in all_notices:
        deduped.setdefault(n["link"], n)
    final = list(deduped.values())
    print(f"DEBUG: Grand total across all SSC pages: {len(all_notices)} raw, "
          f"{len(final)} unique after link-based de-dup.")
    return final


def fetch_ssc(page):
    """SSC entry point for the merged main() flow. Delegates entirely to
    the original, logic-untouched fetch_all_notices(), just handing it the
    SHARED browser page so only one Chromium instance is ever open."""
    return fetch_all_notices(shared_page=page)


# ================================================================
# DSSSB (dsssb.delhi.gov.in) — INDEPENDENT MODULE
# ================================================================
#
# Philosophy same as SSC: API-first (agar JSON response mile to schema
# generically parse hoti hai), DOM extraction hamesha safety-net/fallback
# ke roop me chalta hai aur API results ke saath merge hota hai.

DSSSB_BASE = "https://dsssb.delhi.gov.in"

# Confirmed public listing pages (live site pe verify kiya gaya hai).
# NOTE: DSSSB ka "Admit Card" sirf candidate ke apne login (Application
# Number + Password) se milta hai -- individual/private portal hai,
# publicly listed page nahi, isliye generic monitoring me include nahi
# kiya ja sakta.
DSSSB_PAGES = {
    "Notice Board": f"{DSSSB_BASE}/notifications",
    "Result": f"{DSSSB_BASE}/results",
    "Notice of Exam / Circulars": f"{DSSSB_BASE}/notice-of-exam",
    "Latest Updates": f"{DSSSB_BASE}/dsssb/latest-updates",
    "Vacancy / Advertisement": f"{DSSSB_BASE}/dsssb-vacancies",
    "Recruitment": f"{DSSSB_BASE}/recruitment",
    "Home": f"{DSSSB_BASE}/",
}

# Auto-discovery keywords -- home page ke internal links me se agar href
# ya link-text me in words ka koi bhi part mile, wo page bhi automatically
# check-list me add ho jaata hai (bina DSSSB_PAGES manually edit kiye).
DSSSB_DISCOVERY_KEYWORDS = (
    "notice", "notif", "result", "recruit", "vacan", "circular",
    "advertis", "update", "exam", "answer", "admit", "press",
    "corrigendum", "order", "public-notice", "oars",
)

# DSSSB rows show "Date: dd-mm-yyyy" as ONE combined line (unlike SSC's
# split day/month/year lines), plus a "Filter"/"Reset" widget. Extends
# (does not modify) the original SKIP_LINE_PATTERNS list.
DSSSB_SKIP_LINE_PATTERNS = SKIP_LINE_PATTERNS + [
    r"^date\s*:.*$",
    r"^filter$",
    r"^reset$",
    r"^\(ex:\s*\d{4}\)$",
]

# Generic JSON-record field-name hints, used ONLY if DSSSB ever starts
# returning a JSON/XHR API (none observed as of writing -- this makes the
# system future-proof without hardcoding an unconfirmed schema).
DSSSB_TITLE_KEYS = ("title", "headline", "name", "subject", "heading")
DSSSB_FILE_KEYS = ("file", "url", "link", "path", "attachment", "document", "pdf", "href")


def is_probably_dsssb_document_link(href):
    """Same document-detection rules as SSC's is_probably_document_link,
    duplicated here (not shared) to keep the DSSSB module independent."""
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


def clean_dsssb_title(raw_text):
    """DSSSB-specific title cleaner (own SKIP pattern list)."""
    if not raw_text:
        return ""
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    kept = []
    for line in lines:
        if any(re.match(p, line, re.IGNORECASE) for p in DSSSB_SKIP_LINE_PATTERNS):
            continue
        kept.append(line)
    return " ".join(kept).strip()


def looks_like_dsssb_json_response(url, content_type):
    """Same idea as SSC's looks_like_api_json_response, generalized to also
    catch common Drupal JSON endpoints (/jsonapi/, /rest/) in case DSSSB
    ever exposes one."""
    if content_type and "json" in content_type.lower():
        return True
    lowered = url.lower()
    if any(seg in lowered for seg in ("/api/", "/jsonapi/", "/rest/")) and not any(
        lowered.endswith(ext) for ext in FILE_EXTENSIONS
    ):
        return True
    return False


def extract_generic_json_records(node, found, category):
    """DSSSB ka JSON schema abhi tak confirm nahi hua hai (koi API observed
    nahi hui), isliye ye ek GENERIC extractor hai: kisi dict me agar ek
    'title-jaisa' string field aur ek 'file-jaisa' string field (jo
    document extension ya attachment/api pattern se match kare) dono
    milte hain, to use ek notice maana jaata hai. Isse agar DSSSB kabhi
    JSON API add kare, to bina notifier.py badle bhi kaam chal sakta hai."""
    if isinstance(node, dict):
        title_val = None
        file_val = None
        for k, v in node.items():
            if isinstance(v, str):
                kl = k.lower()
                if title_val is None and any(tk in kl for tk in DSSSB_TITLE_KEYS) and len(v.strip()) > 8:
                    title_val = v.strip()
                if file_val is None and any(fk in kl for fk in DSSSB_FILE_KEYS):
                    if v.lower().endswith(FILE_EXTENSIONS) or "attachment" in v.lower():
                        file_val = v.strip()
        if title_val and file_val:
            href = file_val if file_val.lower().startswith("http") else absolutize(file_val, base_domain=DSSSB_BASE)
            if href:
                found.append({
                    "title": clean_dsssb_title(title_val) or title_val,
                    "link": href,
                    "category": category,
                    "file_name": href.rsplit("/", 1)[-1],
                    "download_candidates": [href],
                    "source": "DSSSB",
                })
        for v in node.values():
            if isinstance(v, (dict, list)):
                extract_generic_json_records(v, found, category)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                extract_generic_json_records(item, found, category)


def extract_dsssb_records(page, category):
    """DOM-row extraction (fallback / primary-when-no-API) for one
    already-loaded DSSSB page. Title is plain text next to a "View"/
    "Download" link, not inside the <a> tag -- same DOM-climb technique as
    SSC's safety-net."""
    found = []
    try:
        elements = page.query_selector_all(LINK_SELECTOR)
    except Exception as e:
        print(f"DEBUG: [DSSSB/{category}] DOM scan failed: {e}")
        return found

    for el in elements:
        try:
            href = el.get_attribute("href") or ""
        except Exception:
            continue
        if not is_probably_dsssb_document_link(href):
            continue
        href = absolutize(href, base_domain=DSSSB_BASE)
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

        title = clean_dsssb_title(raw_row_text or "")
        if len(title) < 8:
            continue

        found.append({
            "title": title,
            "link": href,
            "category": category,
            "file_name": href.rsplit("/", 1)[-1],
            "download_candidates": [href],
            "source": "DSSSB",
        })

    return found


def fetch_dsssb_page(page, category, url):
    """Loads one DSSSB page. Primary: intercepted JSON responses (generic
    schema-guess). Secondary: DOM row extraction, merged in as fallback --
    exactly the same API-then-DOM pattern SSC uses."""
    found_from_api = []
    discovered_api_urls = set()

    def handle_response(response):
        try:
            req_url = response.url
            try:
                ctype = response.headers.get("content-type", "")
            except Exception:
                ctype = ""
            if not looks_like_dsssb_json_response(req_url, ctype):
                return
            try:
                data = response.json()
            except Exception:
                try:
                    data = json.loads(response.text())
                except Exception:
                    return
            discovered_api_urls.add(req_url)
            extract_generic_json_records(data, found_from_api, category)
        except Exception as e:
            print(f"DEBUG: [DSSSB/{category}] response-handler error: {e}")

    page.on("response", handle_response)
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
    except Exception as e:
        print(f"WARNING: [DSSSB/{category}] Failed to load {url}: {e}")
    finally:
        try:
            page.remove_listener("response", handle_response)
        except Exception:
            pass

    if discovered_api_urls:
        print(f"DEBUG: [DSSSB/{category}] Discovered {len(discovered_api_urls)} JSON API "
              f"endpoint(s): {sorted(discovered_api_urls)}")
    else:
        print(f"DEBUG: [DSSSB/{category}] No JSON API responses observed — "
              f"relying on DOM scan only for this page.")

    dom_notices = extract_dsssb_records(page, category)

    combined = {}
    for n in found_from_api + dom_notices:
        combined.setdefault(n["link"], n)
    result = list(combined.values())

    print(f"DEBUG: [DSSSB/{category}] {len(found_from_api)} from JSON API (generic) + "
          f"{len(dom_notices)} from DOM scan = {len(result)} unique notice-like item(s).")
    for i, n in enumerate(result[:10]):
        print(f"DEBUG [DSSSB/{category}][{i}] title='{n['title'][:80]}' -> {n['link']}")

    return result


def discover_dsssb_pages(page):
    """DSSSB home page ke internal nav links scan karke naye potential
    listing-pages dhoondta hai jo DSSSB_PAGES dict me hardcoded nahi hain.
    Isse system future-proof banta hai -- naya section (jaise 'Press
    Releases' ya 'OARS Notices') site pe add hone par bhi agli run me
    automatically pick ho sakta hai."""
    discovered = {}
    try:
        page.goto(f"{DSSSB_BASE}/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1000)
        anchors = page.query_selector_all("a[href]")
    except Exception as e:
        print(f"DEBUG: [DSSSB] Auto-discovery failed to load home page: {e}")
        return discovered

    known_urls = {u.rstrip("/") for u in DSSSB_PAGES.values()}

    for el in anchors:
        try:
            href = el.get_attribute("href") or ""
            text = (el.inner_text() or "").strip()
        except Exception:
            continue
        if not href or not text:
            continue

        href = absolutize(href, base_domain=DSSSB_BASE)
        if not href or not href.startswith(DSSSB_BASE):
            continue
        if is_probably_dsssb_document_link(href):
            continue  # ye ek file hai, listing page nahi
        if href.rstrip("/") in known_urls:
            continue

        haystack = (href + " " + text).lower()
        if any(kw in haystack for kw in DSSSB_DISCOVERY_KEYWORDS):
            label = f"(Auto) {text[:40] if text else href}"
            discovered[label] = href
            known_urls.add(href.rstrip("/"))

    if discovered:
        print(f"DEBUG: [DSSSB] Auto-discovered {len(discovered)} additional page(s): "
              f"{list(discovered.keys())}")
    else:
        print("DEBUG: [DSSSB] Auto-discovery found no additional pages this run.")

    return discovered


def fetch_dsssb(page):
    """DSSSB entry point. Uses the SHARED browser page (no separate
    Chromium instance). Combines the hardcoded DSSSB_PAGES with any
    auto-discovered pages for this run, then checks every one of them."""
    discovered = discover_dsssb_pages(page)
    pages_to_check = dict(DSSSB_PAGES)
    pages_to_check.update(discovered)

    all_notices = []
    for category, url in pages_to_check.items():
        print(f"DEBUG: ---- Checking [DSSSB/{category}] -> {url} ----")
        page_notices = fetch_dsssb_page(page, category, url)
        all_notices.extend(page_notices)
        time.sleep(1)

    deduped = {}
    for n in all_notices:
        deduped.setdefault(n["link"], n)
    final = list(deduped.values())
    print(f"DEBUG: [DSSSB] Grand total: {len(all_notices)} raw, "
          f"{len(final)} unique after link-based de-dup.")
    return final


def merge_notices(ssc_notices, dsssb_notices):
    """Combines SSC + DSSSB notices into one list, tagging every item with
    its source and de-duplicating across both by a composite 'source:link'
    key so the two sites can never collide."""
    for n in ssc_notices:
        n.setdefault("source", "SSC")
    for n in dsssb_notices:
        n.setdefault("source", "DSSSB")

    combined = list(ssc_notices) + list(dsssb_notices)

    deduped = {}
    for n in combined:
        key = f"{n['source']}:{n['link']}"
        deduped.setdefault(key, n)

    return list(deduped.values())


# ================================================================
# STATE (seen_notices.json)
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

def build_caption(category, title, file_name, source="SSC"):
    return (
        f"⚡️〔 <b>{source} ALERT</b> 〕⚡️\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>{category}</b>\n\n"
        f"🗂 <b>{title}</b>\n\n"
        f"📄 <code>{file_name}</code>\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>Sabse pehle, sabse tez — sirf</i> @SSC_DIARY <i>par</i> 🚀"
    )


def is_file_link(link):
    return link.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")) or "attachment" in link.lower()


def send_text_message(category, title, link, source="SSC"):
    message = (
        f"⚡️〔 <b>{source} ALERT</b> 〕⚡️\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>{category}</b>\n\n"
        f"🗂 <b>{title}</b>\n\n"
        f"🔗 {link}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>Join-</i> @SSC_DIARY <i>par</i> 🚀"
    )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = SESSION.post(url, data={
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
    error/login page disguised behind a 200 status."""
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


def send_document(category, title, link, file_name=None, download_candidates=None, source="SSC"):
    """Downloads the actual file ourselves FIRST, validates it, and only
    then uploads it to Telegram."""
    candidates = download_candidates or [link]
    file_name = file_name or link.rsplit("/", 1)[-1] or "notice.pdf"
    caption = build_caption(category, title, file_name, source=source)
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    for candidate_url in candidates:
        print(f"DEBUG: [{source}/{category}] Trying download candidate: {candidate_url}")
        try:
            file_resp = SESSION.get(candidate_url, timeout=60)
        except Exception as e:
            print(f"DEBUG: [{source}/{category}] Download error for {candidate_url}: {e}")
            continue

        if not validate_downloaded_file(file_resp, candidate_url, file_name):
            print(f"DEBUG: [{source}/{category}] Candidate failed validation, trying next "
                  f"if available: {candidate_url}")
            continue

        try:
            resp = SESSION.post(telegram_url, data={
                "chat_id": CHANNEL_ID,
                "caption": caption,
                "parse_mode": "HTML",
            }, files={"document": (file_name, file_resp.content)}, timeout=60)
            if resp.status_code == 200:
                print(f"DEBUG: [{source}/{category}] Successfully uploaded '{file_name}' "
                      f"from {candidate_url}")
                return True
            print(f"Telegram upload failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"Telegram upload error: {e}")

    print(f"WARNING: [{source}/{category}] All download candidates failed for headline "
          f"'{title}' (fileName='{file_name}'). Falling back to plain text message.")
    return send_text_message(category, title, link, source=source)


def send_to_telegram(category, title, link, file_name=None, download_candidates=None, source="SSC"):
    if not BOT_TOKEN or not CHANNEL_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID not set.")
        return False
    if is_file_link(link):
        return send_document(category, title, link, file_name=file_name,
                              download_candidates=download_candidates, source=source)
    return send_text_message(category, title, link, source=source)


# ================================================================
# MAIN
# ================================================================

def main():
    seen = load_seen()

    # One-time, automatic migration: purani (prefix-less) SSC keys ko
    # "SSC:" prefix diya jaata hai, taaki purani notified entries dobara
    # "new" maan kar repost na ho jaayein.
    if seen and not all(k.startswith("SSC:") or k.startswith("DSSSB:") for k in seen):
        seen = {(k if (k.startswith("SSC:") or k.startswith("DSSSB:")) else f"SSC:{k}"): v
                for k, v in seen.items()}

    print(f"DEBUG: {len(seen)} notices already marked as seen from previous runs.")

    seen_signatures = set()
    seen_filename_keys = set()
    for entry in seen.values():
        if not isinstance(entry, dict):
            continue
        if "title" in entry and "category" in entry:
            seen_signatures.add(normalize_signature(entry["title"], entry["category"]))
        fn_sig = normalize_filename_signature(
            entry.get("source", "SSC"), entry.get("category", ""), entry.get("file_name")
        )
        if fn_sig:
            seen_filename_keys.add(fn_sig)

    # ---- Single shared Chromium instance for BOTH SSC and DSSSB ----
    print("DEBUG: ==== Launching single shared Chromium instance ====")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        print("DEBUG: ==== Fetching SSC ====")
        ssc_notices = fetch_ssc(page)

        print("DEBUG: ==== Fetching DSSSB ====")
        dsssb_notices = fetch_dsssb(page)

        browser.close()

    notices = merge_notices(ssc_notices, dsssb_notices)
    print(f"DEBUG: Total unique notice-like links across SSC + DSSSB = {len(notices)}")

    if not notices:
        print("No notices fetched this run (could be a temporary site issue).")
        sys.exit(0)

    new_count = 0
    for n in notices:
        source = n.get("source", "SSC")
        key = f"{source}:{n['link']}"
        title_sig = normalize_signature(n["title"], n["category"])
        fn_sig = normalize_filename_signature(source, n["category"], n.get("file_name"))

        if key in seen:
            continue

        if title_sig in seen_signatures:
            print(f"DEBUG: Skipping likely duplicate (same headline/category, "
                  f"different link) [{source}/{n['category']}]: {n['title']}")
            seen[key] = {
                "title": n["title"], "category": n["category"], "notified": True,
                "file_name": n.get("file_name"), "source": source,
            }
            continue

        if fn_sig and fn_sig in seen_filename_keys:
            print(f"DEBUG: Skipping likely duplicate (same file_name/category, "
                  f"different URL) [{source}/{n['category']}]: {n.get('file_name')}")
            seen[key] = {
                "title": n["title"], "category": n["category"], "notified": True,
                "file_name": n.get("file_name"), "source": source,
            }
            continue

        print(f"New notice found [{source}/{n['category']}]: {n['title']} "
              f"(fileName={n.get('file_name')}) -> {n['link']}")
        ok = send_to_telegram(
            n["category"], n["title"], n["link"],
            file_name=n.get("file_name"),
            download_candidates=n.get("download_candidates"),
            source=source,
        )
        if ok:
            seen[key] = {
                "title": n["title"], "category": n["category"], "notified": True,
                "file_name": n.get("file_name"), "source": source,
            }
            seen_signatures.add(title_sig)
            if fn_sig:
                seen_filename_keys.add(fn_sig)
            new_count += 1
        else:
            print(f"WARNING: Failed to send notice [{source}/{n['category']}]: {n['title']}")
        time.sleep(2)

    if len(seen) > 1500:
        seen = dict(list(seen.items())[-1500:])

    save_seen(seen)
    print(f"Done. {new_count} new notice(s) posted.")


if __name__ == "__main__":
    main()
