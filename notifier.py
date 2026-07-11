"""
SSC (ssc.gov.in) + DSSSB (dsssb.delhi.gov.in) -> Telegram Auto-Poster
====================================================

SSC SECTION IS PRODUCTION CODE -- behavior 100% unchanged from the version
supplied. Kya karta hai:
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
5. `attachments[].path` khud ek public download URL NAHI hai -- confirmed
   real pattern `https://ssc.gov.in/api/attachment/<path>` hai, jise
   primary candidate banaya gaya hai, aur ek chhoti fallback candidate list
   bhi try hoti hai.
6. Telegram pe upload karne se PEHLE file ko khud download karke validate
   kiya jaata hai (status 200, Content-Type, "%PDF" magic bytes).
7. Result/Answer Key jaise pages pe <select> dropdown se exam choose karna
   padta hai -- script automatically har option try karta hai.
8. DOM-based generic <a href> scanning ek SAFETY NET ke roop me rakha hai.
9. seen_notices.json se compare, jo bhi NAYA milta hai Telegram pe jaata hai.

DSSSB SECTION (independent module):
DSSSB ek server-rendered (Drupal) site hai. Iske liye:
  - Generic JSON/XHR auto-detect (non-schema-locked -- future-proofing ke
    liye, abhi tak koi API observed nahi hui, isliye zyaadatar DOM scan
    hi chalega).
  - DOM-row extraction primary/reliable path hai.
  - Home-page nav-menu se naye sections auto-discover karne ki koshish
    (best-effort, non-fatal -- confirmed pages hamesha check hote hain
    chahe discovery fail ho jaaye).
  - `domcontentloaded` wait strategy (NOT `networkidle`) -- DSSSB pe
    background analytics/tracker requests hamesha chalte rehte hain jisse
    "networkidle" state kabhi aata hi nahi aur page.goto() 60s timeout
    tak hang ho jaata tha. domcontentloaded turant fire hota hai jaise hi
    HTML mil jaaye, isliye fast aur reliable hai.
  - Agar page load hi fail ho jaaye, DOM scan bilkul skip ho jaata hai
    (pehle wala bug: goto fail hone ke baad bhi query_selector_all() call
    ho raha tha, jisse "Execution context was destroyed" wali cascading
    error aati thi).

PERFORMANCE (cron ko jitna jaldi ho sake dobara chalne dene ke liye):
  - EK HI Chromium browser instance -- SSC aur DSSSB dono isi ko reuse
    karte hain (naya launch nahi hota beech me).
  - EK HI requests.Session() -- saare downloads aur Telegram API calls
    connection pooling ke saath jaate hain.
  - DSSSB ke timeouts chhote aur bounded hain (25s/page max, domcontentloaded)
    taaki agar DSSSB down bhi ho, poora run 1-2 minute se zyada na atke.
  - Auto-discovery sirf DSSSB home page pe ek chhota (20s) check hai, aur
    max 5 extra pages hi add karta hai -- unbounded growth se bachne ke
    liye.

GitHub Actions cron-job.org se trigger hota hai (workflow_dispatch), 
concurrency queueing ("cancel-in-progress: false") ke saath, taaki koi
bhi trigger cycle beech me cancel na ho aur agla run turant queue se
shuru ho jaaye jaise hi purana khatam ho.
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
# CONFIG -- SSC (unchanged)
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

# Ek hi requests Session -- SSC downloads, DSSSB downloads, aur Telegram
# API calls sab isi se jaate hain (connection pooling / thoda fast).
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
    (which calls absolutize(href) with just one argument) behaves 100%
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
    """A loose fingerprint of a notice, used ONLY as an extra duplicate
    check in-memory (does not change seen_notices.json's schema)."""
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return f"{category.lower()}::{t}"


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
    """Schema-exact extraction for SSC's real API shape:
        { "headline": "...", "attachments": [
              {"fileName": ..., "path": ..., "type": ..., "documentType": ...},
        ] }
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
    """Generic DOM anchor scan — fallback safety net."""
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
    responses, schema-matched. Secondary: generic DOM scan."""
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

    `shared_page` -- NEW, OPTIONAL. If None (default), behaves EXACTLY as
    before: opens its own Chromium browser, uses it, closes it. This is
    the standalone/original behavior, byte-for-byte unchanged.

    If a Playwright `page` object is passed in (from main(), sharing ONE
    browser across SSC + DSSSB), this function uses that page directly and
    does NOT open or close any browser itself -- the caller owns that
    lifecycle. This is the only way single-browser-instance sharing works
    without editing a single line of the actual SSC scraping logic above.
    """
    def _run(page):
        all_notices = []
        for category, url in PAGES_TO_MONITOR.items():
            print(f"DEBUG: ---- Checking [{category}] -> {url} ----")
            page_notices = fetch_notices_from_page(page, category, url)
            all_notices.extend(page_notices)
            time.sleep(1)
        return all_notices

    if shared_page is not None:
        all_notices = _run(shared_page)
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=USER_AGENT)
            all_notices = _run(page)
            browser.close()

    deduped = {}
    for n in all_notices:
        deduped.setdefault(n["link"], n)
    final = list(deduped.values())
    print(f"DEBUG: Grand total across all SSC pages: {len(all_notices)} raw, "
          f"{len(final)} unique after link-based de-dup.")
    return final


def fetch_ssc(page=None):
    """SSC entry point used by main(). Delegates entirely to the untouched
    fetch_all_notices() -- passes the shared browser page through when
    available so only ONE Chromium instance is ever open."""
    return fetch_all_notices(shared_page=page)


# ================================================================
# DSSSB (dsssb.delhi.gov.in) — INDEPENDENT MODULE
# ================================================================

DSSSB_BASE = "https://dsssb.delhi.gov.in"

# Confirmed public listing pages (verified on the live site). Always
# checked, regardless of whether auto-discovery below succeeds or fails.
#
# NOTE: DSSSB's "Admit Card" download requires the candidate's own login
# (Application Number + Password) -- an individual/private portal, not a
# publicly listed page, so it cannot be generically monitored.
DSSSB_PAGES = {
    "Notice Board": f"{DSSSB_BASE}/notifications",
    "Result": f"{DSSSB_BASE}/results",
    "Notice of Exam / Circulars": f"{DSSSB_BASE}/notice-of-exam",
    "Latest Updates": f"{DSSSB_BASE}/dsssb/latest-updates",
    "Vacancy / Advertisement": f"{DSSSB_BASE}/dsssb-vacancies",
    "Recruitment": f"{DSSSB_BASE}/recruitment",
    "Home": f"{DSSSB_BASE}/",
}

MAX_AUTO_DISCOVERED_PAGES = 5  # unbounded growth se bachne ke liye cap

# DSSSB rows show "Date: dd-mm-yyyy" as ONE combined line, plus a
# Filter/Reset search widget and a "(Ex: 2025)" placeholder hint.
DSSSB_SKIP_LINE_PATTERNS = SKIP_LINE_PATTERNS + [
    r"^date\s*:.*$",
    r"^filter$",
    r"^reset$",
    r"^\(ex:\s*\d{4}\)$",
]

DSSSB_NAV_IGNORE_TEXT = re.compile(
    r"login|register|sitemap|privacy|contact|^home$|rti|tender|feedback|"
    r"accessibility|disclaimer|help|faq",
    re.IGNORECASE,
)


def is_probably_dsssb_document_link(href):
    """Same document-detection rules as SSC's version, duplicated here to
    keep the DSSSB module fully independent."""
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
    if not raw_text:
        return ""
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    kept = []
    for line in lines:
        if any(re.match(p, line, re.IGNORECASE) for p in DSSSB_SKIP_LINE_PATTERNS):
            continue
        kept.append(line)
    return " ".join(kept).strip()


def looks_like_generic_json_record(node):
    """Very loose, non-schema-locked heuristic: does this dict have
    something that looks like a title AND something that looks like a
    document link? Used only as a DSSSB future-proofing layer, since no
    such API is currently known to exist on DSSSB's site."""
    if not isinstance(node, dict):
        return None, None
    title = None
    for key in ("headline", "title", "name", "subject", "heading"):
        v = node.get(key)
        if isinstance(v, str) and v.strip():
            title = v.strip()
            break
    file_link = None
    for key in ("path", "url", "file", "fileUrl", "attachment", "pdf", "link", "href"):
        v = node.get(key)
        if isinstance(v, str) and v.strip():
            if v.lower().endswith(FILE_EXTENSIONS) or "attachment" in v.lower():
                file_link = v.strip()
                break
    return title, file_link


def extract_dsssb_generic_json(node, found, source_url, category):
    """Generic JSON record walker for DSSSB -- future-proofing in case
    DSSSB ever adds a JSON/XHR API. Currently expected to find nothing."""
    if isinstance(node, dict):
        title, file_link = looks_like_generic_json_record(node)
        if title and file_link:
            found.append({
                "title": title,
                "raw_link": file_link,
                "category": category,
                "_source": source_url,
            })
        for v in node.values():
            if isinstance(v, (dict, list)):
                extract_dsssb_generic_json(v, found, source_url, category)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                extract_dsssb_generic_json(item, found, source_url, category)


def extract_dsssb_records(page, category):
    """DOM-row extraction for one already-loaded DSSSB page. A DSSSB row
    looks like:
        <title text>
        Date: dd-mm-yyyy
        View   (this is the actual <a href=".../something.pdf">)
    Title is plain text next to the "View" link, not inside the <a> tag --
    so we climb up the DOM from the document link to the row container and
    grab its text, then strip date/badge/action-word lines."""
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


def extract_dsssb_records_via_reader(url, category):
    """FALLBACK for when direct browser navigation to DSSSB fails.

    Confirmed root cause: DSSSB's server appears to block connections from
    GitHub Actions' datacenter IP ranges (net::ERR_CONNECTION_TIMED_OUT) --
    a common practice on Indian government sites -- even though the site
    is fully reachable from normal/residential networks. No amount of
    Playwright timeout/wait-strategy tuning fixes a blocked TCP connection.

    Workaround: fetch the page through the free Jina Reader proxy
    (https://r.jina.ai/<url>), which renders the page server-side on
    Jina's own infrastructure (a different IP range, not blocked) and
    returns clean Markdown. Jina Reader converts HTML to Markdown via
    Readability + Turndown, so every document link comes back in standard
    "[link text](https://...)" syntax -- we parse that directly with
    plain `requests`, no browser needed for this path at all.
    """
    found = []
    try:
        resp = SESSION.get(f"https://r.jina.ai/{url}", timeout=30)
        if resp.status_code != 200 or not resp.text:
            print(f"DEBUG: [DSSSB/{category}] Reader-proxy fallback failed: "
                  f"HTTP {resp.status_code}")
            return found
        text = resp.text
    except Exception as e:
        print(f"DEBUG: [DSSSB/{category}] Reader-proxy fallback error: {e}")
        return found

    link_pattern = re.compile(r"\[([^\]]*)\]\((https?://[^\s\)]+)\)")
    for line in text.split("\n"):
        for m in link_pattern.finditer(line):
            link_text, link_url = m.group(1).strip(), m.group(2).strip()
            link_url_lower = link_url.lower()
            if not (link_url_lower.endswith(FILE_EXTENSIONS) or "attachment" in link_url_lower):
                continue

            rest_of_line = link_pattern.sub(" ", line).strip()
            title_source = rest_of_line if len(rest_of_line) >= 8 else link_text
            title = clean_dsssb_title(title_source)
            if len(title) < 8:
                continue

            found.append({
                "title": title,
                "link": link_url,
                "category": category,
                "file_name": link_url.rsplit("/", 1)[-1],
                "download_candidates": [link_url],
                "source": "DSSSB",
            })

    print(f"DEBUG: [DSSSB/{category}] Reader-proxy fallback found {len(found)} document link(s).")
    return found


def fetch_dsssb_page(page, category, url):
    """Loads one DSSSB page and extracts its notices.

    FIX: uses `domcontentloaded` instead of `networkidle`. DSSSB's site
    keeps background analytics/tracker requests running continuously, so
    "networkidle" (which needs 500ms of zero network activity) never
    actually fires -- that was the root cause of the 60s timeouts.
    `domcontentloaded` fires as soon as the initial HTML is parsed, which
    is all we need since DSSSB is server-rendered (no client-side JS
    building the notice list).

    FIX: if the page fails to load at all, we return immediately WITHOUT
    attempting a DOM scan -- attempting query_selector_all() on a page
    that failed navigation throws "Execution context was destroyed",
    which was cascading into a second, confusing error before.
    """
    found_from_api = []
    discovered_api_urls = set()

    def handle_response(response):
        try:
            req_url = response.url
            if DSSSB_BASE not in req_url:
                return
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
                return
            discovered_api_urls.add(req_url)
            extract_dsssb_generic_json(data, found_from_api, req_url, category)
        except Exception:
            pass

    page.on("response", handle_response)
    page_loaded = True
    try:
        # Timeout chhota rakha (12s, pehle 25s tha) -- GitHub Actions se
        # DSSSB block hone ki wajah se ye almost hamesha fail hoga, isliye
        # jaldi fail karke reader-proxy fallback pe switch karna behtar
        # hai, poora 25s waste karne se.
        page.goto(url, wait_until="domcontentloaded", timeout=12000)
        page.wait_for_timeout(1000)
    except Exception as e:
        print(f"WARNING: [DSSSB/{category}] Direct browser load failed ({e}). "
              f"Falling back to Jina Reader proxy...")
        page_loaded = False
    finally:
        try:
            page.remove_listener("response", handle_response)
        except Exception:
            pass

    if discovered_api_urls:
        print(f"DEBUG: [DSSSB/{category}] Discovered {len(discovered_api_urls)} JSON API "
              f"endpoint(s):")
        for u in sorted(discovered_api_urls):
            print(f"DEBUG: [DSSSB/{category}]   API -> {u}")
    else:
        print(f"DEBUG: [DSSSB/{category}] No JSON API responses observed — "
              f"relying on DOM scan only for this page.")

    api_notices = []
    for rec in found_from_api:
        full_link = absolutize(rec["raw_link"], base_domain=DSSSB_BASE)
        if not full_link:
            continue
        api_notices.append({
            "title": rec["title"],
            "link": full_link,
            "category": category,
            "file_name": full_link.rsplit("/", 1)[-1],
            "download_candidates": [full_link],
            "source": "DSSSB",
        })

    dom_notices = []
    if page_loaded:
        dom_notices = extract_dsssb_records(page, category)
    else:
        dom_notices = extract_dsssb_records_via_reader(url, category)

    combined = {}
    for n in api_notices + dom_notices:
        combined.setdefault(n["link"], n)
    result = list(combined.values())

    print(f"DEBUG: [DSSSB/{category}] {len(api_notices)} from JSON API (generic) + "
          f"{len(dom_notices)} from DOM scan = {len(result)} unique notice-like item(s).")
    for i, n in enumerate(result[:10]):
        print(f"DEBUG [DSSSB/{category}][{i}] title='{n['title'][:80]}' -> {n['link']}")

    return result


def discover_dsssb_pages(page):
    """Best-effort: auto-discovers additional DSSSB listing pages from the
    home page's navigation menu, so future new sections get picked up
    automatically. Non-fatal if it fails -- DSSSB_PAGES is always checked
    regardless. Capped at MAX_AUTO_DISCOVERED_PAGES to avoid runaway
    growth if the nav menu is large or noisy."""
    discovered = {}
    try:
        page.goto(DSSSB_BASE + "/", wait_until="domcontentloaded", timeout=12000)
        page.wait_for_timeout(600)
        nav_links = page.query_selector_all(
            "nav a[href], header a[href], .menu a[href], .navbar a[href], .main-menu a[href]"
        )
        for el in nav_links:
            if len(discovered) >= MAX_AUTO_DISCOVERED_PAGES:
                break
            try:
                href = el.get_attribute("href") or ""
                text = (el.inner_text() or "").strip()
            except Exception:
                continue
            if not href or not text or len(text) < 3 or len(text) > 60:
                continue
            if href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            href_lower = href.lower()
            if href_lower.endswith(FILE_EXTENSIONS):
                continue
            if DSSSB_NAV_IGNORE_TEXT.search(text):
                continue
            full = absolutize(href, base_domain=DSSSB_BASE)
            if not full or not full.startswith(DSSSB_BASE):
                continue
            if full in DSSSB_PAGES.values():
                continue
            discovered[text] = full
    except Exception as e:
        print(f"DEBUG: [DSSSB] Auto-discovery failed to load home page: {e}")

    return discovered


def fetch_dsssb(page):
    """Visits every page in DSSSB_PAGES (plus any auto-discovered ones)
    and collects notices from all of them. Uses the SAME shared browser
    `page` passed in from main() -- no separate Chromium instance."""
    pages_to_check = dict(DSSSB_PAGES)

    discovered = discover_dsssb_pages(page)
    if discovered:
        print(f"DEBUG: [DSSSB] Auto-discovered {len(discovered)} additional page(s) "
              f"from nav menu: {list(discovered.keys())}")
        pages_to_check.update(discovered)

    all_notices = []
    for category, url in pages_to_check.items():
        print(f"DEBUG: ---- Checking [DSSSB/{category}] -> {url} ----")
        page_notices = fetch_dsssb_page(page, category, url)
        all_notices.extend(page_notices)
        time.sleep(0.5)

    deduped = {}
    for n in all_notices:
        deduped.setdefault(n["link"], n)
    final = list(deduped.values())
    print(f"DEBUG: [DSSSB] Grand total: {len(all_notices)} raw, "
          f"{len(final)} unique after link-based de-dup.")
    return final


def merge_notices(ssc_notices, dsssb_notices):
    """Combines SSC + DSSSB notices, tagging every item with its source
    and de-duplicating across both by a composite 'source:link' key."""
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
                  f"bytes (got {content_start!r}) — looks like an HTML/error page.")
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

    # One-time, automatic migration: old keys were plain links (SSC-only
    # era). Prefix them with "SSC:" so they're never re-treated as new.
    if seen and not all(k.startswith("SSC:") or k.startswith("DSSSB:") for k in seen):
        seen = {(k if (k.startswith("SSC:") or k.startswith("DSSSB:")) else f"SSC:{k}"): v
                for k, v in seen.items()}

    print(f"DEBUG: {len(seen)} notices already marked as seen from previous runs.")

    seen_signatures = set()
    for entry in seen.values():
        if isinstance(entry, dict) and "title" in entry and "category" in entry:
            seen_signatures.add(normalize_signature(entry["title"], entry["category"]))

    # ---- SINGLE shared Chromium instance for SSC + DSSSB ----
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
        sig = normalize_signature(n["title"], n["category"])

        if key in seen:
            continue
        if sig in seen_signatures:
            print(f"DEBUG: Skipping likely duplicate (same headline/category, "
                  f"different link) [{source}/{n['category']}]: {n['title']}")
            seen[key] = {"title": n["title"], "category": n["category"], "notified": True}
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
            seen[key] = {"title": n["title"], "category": n["category"], "notified": True}
            seen_signatures.add(sig)
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
