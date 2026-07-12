"""
SSC (ssc.gov.in) + 9 SSC Regional Offices + DSSSB (dsssb.delhi.gov.in)
-> Telegram Auto-Poster
=======================================================================

SSC MAIN SECTION -- PRODUCTION CODE, behavior 100% unchanged from before.
(Schema-based headline+attachments[] JSON API extraction via Playwright,
DOM-scan safety net, dropdown-triggering for Result/Answer Key pages.)

DSSSB SECTION -- unchanged from before. Pure HTTP-request proxy-chain
fetch (Jina reader / allorigins / codetabs / corsproxy / optional
ScraperAPI), since DSSSB's NIC-hosted site blocks direct requests from
GitHub Actions' datacenter IPs.

SSC REGIONAL OFFICES SECTION (NEW):
SSC has 9 separate regional office websites, each on its own domain, each
independently run (some .nic.in, some .org, some .gov.in):
    NR   - https://sscnr.nic.in/          (Delhi, Rajasthan, Uttarakhand)
    NWR  - https://sscnwr.org/            (Chandigarh, Haryana, HP, J&K, Punjab)
    CR   - https://ssc-cr.org/            (UP, Bihar)
    ER   - https://sscer.org/             (West Bengal, Odisha, Jharkhand, etc.)
    NER  - https://sscner.org.in/         (Assam, Arunachal, Manipur, etc.)
    WR   - https://sscwr.net/             (Maharashtra, Gujarat, Goa)
    MPR  - https://sscmpr.org/            (Madhya Pradesh, Chhattisgarh)
    SR   - https://sscsr.gov.in/          (Andhra Pradesh, TN, Telangana, Puducherry)
    KKR  - https://ssckkr.kar.nic.in/     (Karnataka, Kerala, Lakshadweep)

These are government sites on varied, unverified-live HTML layouts (this
script's regional URLs/regions were confirmed via search, not a live
browser session), so -- same reasoning as DSSSB -- each region's HOMEPAGE
is fetched through the same proxy-fallback chain (Jina/allorigins/
codetabs/corsproxy, or ScraperAPI if configured) rather than direct
Playwright navigation, since .nic.in/.gov.in regional sites are just as
likely to block GitHub Actions' datacenter IPs as DSSSB was. Only the
homepage of each region is monitored for now, since regional sites
typically show their latest notices/results directly on the homepage. If
DEBUG logs show a region isn't picking up real notices, that region's
homepage structure differs from what was assumed -- check the logs and
adjust, or add that region's actual notice-board sub-page URL once known.

FIRST-RUN BASELINE SEEDING (NEW, applies to every source):
Adding 9 brand-new regional sites means, on their very first run, this
script would otherwise find EVERY notice already sitting on each site and
blast all of them to Telegram at once -- since none of them exist yet in
seen_notices.json. To prevent that: the very first time a given SOURCE
(e.g. "SSC-NR", "SSC-KKR") is seen, every notice found for it this run is
silently recorded into seen_notices.json as already-seen -- nothing is
sent to Telegram. That source is then marked "seeded" (seen_notices.json
key "_seeded_sources"). From the NEXT run onwards, only genuinely NEW
notices for that source (published after this baseline) are sent, same
as SSC main and DSSSB already do today (which are auto-marked "seeded" on
upgrade, since they already have real history in seen_notices.json --
their existing behavior is 100% unaffected).

Everything else (single shared requests.Session, single shared Chromium
instance for SSC-main, time budgets so a slow/blocked site can't hang the
whole run, GitHub Actions cron via cron-job.org workflow_dispatch) is
unchanged from the working version.
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

# Ek hi requests Session -- SSC downloads, DSSSB/regional downloads, aur
# Telegram API calls sab isi se jaate hain (connection pooling / thoda fast).
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
    identically to before. DSSSB and each regional site pass their own
    base_domain explicitly."""
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

    `shared_page` -- OPTIONAL. If None (default), opens its own Chromium
    browser, uses it, closes it (standalone behavior, unchanged). If a
    Playwright `page` object is passed in (from main(), sharing ONE
    browser across the whole run), this function uses that page directly
    and does NOT open or close any browser itself.
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
# DSSSB (dsssb.delhi.gov.in) + SSC REGIONAL OFFICES
# — shared HTTP-proxy-chain module (no browser needed)
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

# NEW: SSC's 9 regional office sites. Each is a separate domain/site run
# independently of ssc.gov.in. URLs confirmed via search (not a live
# browser check), so only the homepage of each is monitored for now --
# regional sites typically list their latest notices/results right on
# the homepage. Category is always "Home" per region; add a dedicated
# sub-page URL here later if a region turns out to have one worth
# tracking separately.
REGIONAL_SSC_SITES = {
    "SSC-NR":  {"url": "https://sscnr.nic.in/",      "base": "https://sscnr.nic.in"},
    "SSC-NWR": {"url": "https://sscnwr.org/",         "base": "https://sscnwr.org"},
    "SSC-CR":  {"url": "https://ssccr.gov.in/",       "base": "https://ssccr.gov.in"},
    "SSC-ER":  {"url": "https://sscer.org/",          "base": "https://sscer.org"},
    "SSC-NER": {"url": "https://sscner.org.in/",      "base": "https://sscner.org.in"},
    "SSC-WR":  {"url": "https://sscwr.net/",          "base": "https://sscwr.net"},
    "SSC-MPR": {"url": "https://sscmpr.org/",         "base": "https://sscmpr.org"},
    "SSC-SR":  {"url": "https://sscsr.gov.in/",       "base": "https://sscsr.gov.in"},
    "SSC-KKR": {"url": "https://ssckkr.kar.nic.in/",  "base": "https://ssckkr.kar.nic.in"},
}

# Poori DSSSB phase (saare pages + fallback attempts milaake) is se zyada
# time kabhi nahi legi. Regional phase gets its own separate budget so
# the two don't compete for time within the same run.
DSSSB_TIME_BUDGET_SECONDS = 150
REGIONAL_TIME_BUDGET_SECONDS = 450

# Agar SCRAPER_API_KEY set hai, to har run DSSSB/regional check karne se
# free monthly credits (1000/month) jaldi khatam ho sakte hain agar cron
# bahut frequent hai. In env vars se control kar sakte ho.
DSSSB_CHECK_EVERY_N_RUNS = max(1, int(os.environ.get("DSSSB_CHECK_EVERY_N_RUNS", "1")))
REGIONAL_CHECK_EVERY_N_RUNS = max(1, int(os.environ.get("REGIONAL_CHECK_EVERY_N_RUNS", "1")))

# DSSSB rows show "Date: dd-mm-yyyy" as ONE combined line, plus a
# Filter/Reset search widget and a "(Ex: 2025)" placeholder hint. Reused
# as a general-purpose extra-noise filter for regional sites too.
DSSSB_SKIP_LINE_PATTERNS = SKIP_LINE_PATTERNS + [
    r"^date\s*:.*$",
    r"^filter$",
    r"^reset$",
    r"^\(ex:\s*\d{4}\)$",
]


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


import urllib.parse
import html as html_module


def _strip_html_tags(html_fragment):
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


TRAILING_DATE_SIZE_PATTERN = re.compile(
    r"\s*Date\s*:\s*\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s*\|\s*[\d.]+\s*(KB|MB)\s*$",
    re.IGNORECASE,
)


def _extract_title_from_window(text):
    """The raw window often contains the TAIL of the previous row (its
    own Date/Size/'Download' text) before the current row's real title
    begins. Fix: find the LAST 'Download'/'View'/'Preview' word in the
    window (that's the previous row's action link) and keep only what
    comes after it -- that's the current row's actual title start."""
    matches = list(re.finditer(r"\b(download|view|preview)\b", text, re.IGNORECASE))
    if matches:
        text = text[matches[-1].end():]
    text = text.strip()
    text = TRAILING_DATE_SIZE_PATTERN.sub("", text)
    return text.strip()


def _clean_window_start(text):
    """The raw window sometimes starts mid-attribute or mid-word. Trim
    everything before the first real word (a capital letter followed by
    2+ more letters) to drop that garbage prefix."""
    m = re.search(r"[A-Z][A-Za-z]{2,}", text)
    if m:
        return text[m.start():]
    return text


def _proxy_download_candidates(full_link):
    """Ordered list of URLs to try when downloading a file (DSSSB or
    regional) for Telegram upload. When ScraperAPI is configured, its
    proxied URL goes FIRST, since a direct .nic.in/.gov.in URL download
    is just as likely to be blocked from GitHub Actions as the page
    listing fetch itself was."""
    if SCRAPER_API_KEY:
        proxied = ("https://api.scraperapi.com/?api_key=" + SCRAPER_API_KEY +
                   "&url=" + urllib.parse.quote(full_link, safe=""))
        return [proxied, full_link]
    return [full_link]


def _looks_like_document_href(href_lower):
    """Broader check than a plain file-extension test. Some govt sites
    (e.g. SSC-SR) serve real PDFs through a dynamic query-string endpoint
    like 'indexes/pdf_view?file=...&token=...' instead of a link that
    ends in '.pdf' -- this catches those too."""
    if href_lower.endswith(FILE_EXTENSIONS):
        return True
    if "attachment" in href_lower:
        return True
    if "pdf_view" in href_lower or "file_view" in href_lower or "doc_view" in href_lower:
        return True
    if re.search(r"[?&](file|filename|doc|document)=", href_lower):
        return True
    return False


def _extract_generic_links_from_html(html, category, base_domain=DSSSB_BASE, source_label="DSSSB"):
    """Shared HTML-parsing logic for every raw-HTML proxy fallback below.
    Works for DSSSB and for any SSC regional site: regex-scans for
    document links, using the HTML text immediately BEFORE each link as
    the title source (same idea as DOM-climbing, done on raw markup)."""
    found = []
    anchor_pattern = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\']', re.IGNORECASE)
    total_anchors = 0
    for m in anchor_pattern.finditer(html):
        total_anchors += 1
        href = m.group(1)
        href_lower = href.lower()
        if not _looks_like_document_href(href_lower):
            continue
        full_link = absolutize(href, base_domain=base_domain)
        if not full_link:
            continue

        # Bigger window (1200 chars) so it reliably contains the previous
        # row's own "Download"/"View" marker, which _extract_title_from_window
        # uses as the real split point.
        window_start = max(0, m.start() - 1200)
        raw_window = _strip_html_tags(html[window_start:m.start()])
        title = _extract_title_from_window(raw_window)
        title = _clean_window_start(title)
        title = re.sub(r"^(download|view|preview)\s+", "", title, flags=re.IGNORECASE)
        if len(title) > 200:
            title = title[-200:]
        title = clean_dsssb_title(title)
        if len(title) < 8:
            continue

        found.append({
            "title": title, "link": full_link, "category": category,
            "file_name": full_link.rsplit("/", 1)[-1],
            "download_candidates": _proxy_download_candidates(full_link),
            "source": source_label,
        })

    if not found:
        # Diagnostic only -- helps tell apart "page had zero <a> tags at
        # all" (blocked/empty/error page slipped past the length check)
        # from "page had plenty of links, none matched our document
        # pattern" (this site's real download links use a URL shape we
        # haven't seen yet -- check a few sample hrefs below to add a
        # new pattern to _looks_like_document_href).
        sample_hrefs = []
        for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\']', html, re.IGNORECASE):
            sample_hrefs.append(m.group(1))
            if len(sample_hrefs) >= 8:
                break
        print(f"DEBUG: [{source_label}/{category}] 0 document links matched out of "
              f"{total_anchors} total <a> tag(s) on page. Sample hrefs seen: {sample_hrefs}")

    return found


def extract_records_via_reader(url, category, base_domain=DSSSB_BASE, source_label="DSSSB"):
    """FALLBACK #1: Jina Reader proxy (https://r.jina.ai/). Fetches the
    page server-side on Jina's infrastructure and converts it to Markdown.
    Single attempt only -- Jina's own infra appears unable to reach these
    India-govt sites reliably either, so retries here don't help."""
    found = []
    headers = {"X-Engine": "direct", "X-Wait-For-Selector": "a", "X-Timeout": "8"}
    try:
        resp = SESSION.get(f"https://r.jina.ai/{url}", headers=headers, timeout=12)
        if not (resp.status_code == 200 and resp.text and len(resp.text) > 200):
            print(f"DEBUG: [{source_label}/{category}] Jina reader failed: "
                  f"HTTP {resp.status_code} — {resp.text[:150]!r}")
            return found
        text = resp.text
    except Exception as e:
        print(f"DEBUG: [{source_label}/{category}] Jina reader error: {e}")
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
                "title": title, "link": link_url, "category": category,
                "file_name": link_url.rsplit("/", 1)[-1],
                "download_candidates": _proxy_download_candidates(link_url),
                "source": source_label,
            })

    print(f"DEBUG: [{source_label}/{category}] Jina reader found {len(found)} document link(s).")
    return found


def extract_records_via_allorigins(url, category, base_domain=DSSSB_BASE, source_label="DSSSB"):
    """FALLBACK #2 (free, no signup): allorigins.win -- plain server-side
    HTTP fetch, different provider/IP range than Jina."""
    try:
        proxied = "https://api.allorigins.win/raw?url=" + urllib.parse.quote(url, safe="")
        resp = SESSION.get(proxied, timeout=12)
        if not (resp.status_code == 200 and resp.text and len(resp.text) > 200):
            print(f"DEBUG: [{source_label}/{category}] allorigins failed: HTTP {resp.status_code}")
            return []
        html = resp.text
    except Exception as e:
        print(f"DEBUG: [{source_label}/{category}] allorigins error: {e}")
        return []

    found = _extract_generic_links_from_html(html, category, base_domain=base_domain, source_label=source_label)
    print(f"DEBUG: [{source_label}/{category}] allorigins found {len(found)} document link(s).")
    return found


def extract_records_via_codetabs(url, category, base_domain=DSSSB_BASE, source_label="DSSSB"):
    """FALLBACK #3 (free, no signup): codetabs.com's public CORS proxy --
    yet another independent provider/IP range."""
    try:
        proxied = "https://api.codetabs.com/v1/proxy?quest=" + urllib.parse.quote(url, safe="")
        resp = SESSION.get(proxied, timeout=12)
        if not (resp.status_code == 200 and resp.text and len(resp.text) > 200):
            print(f"DEBUG: [{source_label}/{category}] codetabs failed: HTTP {resp.status_code}")
            return []
        html = resp.text
    except Exception as e:
        print(f"DEBUG: [{source_label}/{category}] codetabs error: {e}")
        return []

    found = _extract_generic_links_from_html(html, category, base_domain=base_domain, source_label=source_label)
    print(f"DEBUG: [{source_label}/{category}] codetabs found {len(found)} document link(s).")
    return found


def extract_records_via_corsproxy(url, category, base_domain=DSSSB_BASE, source_label="DSSSB"):
    """FALLBACK #4 (free, no signup): corsproxy.io -- another independent
    free proxy, no API key needed."""
    try:
        proxied = "https://corsproxy.io/?url=" + urllib.parse.quote(url, safe="")
        resp = SESSION.get(proxied, timeout=12)
        if not (resp.status_code == 200 and resp.text and len(resp.text) > 200):
            print(f"DEBUG: [{source_label}/{category}] corsproxy.io failed: HTTP {resp.status_code}")
            return []
        html = resp.text
    except Exception as e:
        print(f"DEBUG: [{source_label}/{category}] corsproxy.io error: {e}")
        return []

    found = _extract_generic_links_from_html(html, category, base_domain=base_domain, source_label=source_label)
    print(f"DEBUG: [{source_label}/{category}] corsproxy.io found {len(found)} document link(s).")
    return found


SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()


def extract_records_via_scraperapi(url, category, base_domain=DSSSB_BASE, source_label="DSSSB"):
    """FALLBACK -- OPTIONAL, opt-in only (requires SCRAPER_API_KEY).
    ScraperAPI (and equivalents) offer real rotating proxy pools built for
    exactly this "govt site blocks datacenter IPs" scenario. Free tier:
    5,000 one-time + 1,000/month recurring credits.

    Does NOTHING unless SCRAPER_API_KEY is set (get a free key at
    https://www.scraperapi.com/) -- the system stays 100% free by
    default, this is purely an optional upgrade path.
    """
    found = []
    if not SCRAPER_API_KEY:
        return found

    try:
        resp = SESSION.get(
            "https://api.scraperapi.com/",
            params={"api_key": SCRAPER_API_KEY, "url": url},
            timeout=25,
        )
        if not (resp.status_code == 200 and resp.text and len(resp.text) > 200):
            print(f"DEBUG: [{source_label}/{category}] ScraperAPI failed: HTTP {resp.status_code}")
            return found
        html = resp.text
    except Exception as e:
        print(f"DEBUG: [{source_label}/{category}] ScraperAPI error: {e}")
        return found

    found = _extract_generic_links_from_html(html, category, base_domain=base_domain, source_label=source_label)
    print(f"DEBUG: [{source_label}/{category}] ScraperAPI found {len(found)} document link(s).")
    return found


def fetch_via_fallback_chain(url, category, base_domain=DSSSB_BASE, source_label="DSSSB"):
    """If SCRAPER_API_KEY is set, tries it FIRST (up to 3 attempts) --
    empirically the free anonymous proxies rarely succeed against these
    India-govt sites, while ScraperAPI does succeed most of the time. If
    no key is configured, falls back to the free chain (still worth
    trying, just less likely to work)."""
    if SCRAPER_API_KEY:
        for attempt in (1, 2, 3):
            records = extract_records_via_scraperapi(url, category, base_domain=base_domain, source_label=source_label)
            if records:
                return records
            if attempt < 3:
                print(f"DEBUG: [{source_label}/{category}] ScraperAPI attempt {attempt} empty, retrying...")
        print(f"DEBUG: [{source_label}/{category}] ScraperAPI failed 3 times, trying free proxies as last resort...")

    for fn in (
        extract_records_via_reader,
        extract_records_via_allorigins,
        extract_records_via_codetabs,
        extract_records_via_corsproxy,
    ):
        records = fn(url, category, base_domain=base_domain, source_label=source_label)
        if records:
            return records

    print(f"DEBUG: [{source_label}/{category}] All fallback proxies failed — "
          f"this page will be retried on the next run.")
    return []


def fetch_generic_page(category, url, base_domain=DSSSB_BASE, source_label="DSSSB"):
    """Fetches one DSSSB or regional page purely via HTTP-request-based
    proxies -- no browser needed (these are plain server-rendered HTML,
    unlike SSC main's Angular app)."""
    result = fetch_via_fallback_chain(url, category, base_domain=base_domain, source_label=source_label)
    print(f"DEBUG: [{source_label}/{category}] {len(result)} unique notice-like item(s) found.")
    for i, n in enumerate(result[:10]):
        print(f"DEBUG [{source_label}/{category}][{i}] title='{n['title'][:80]}' -> {n['link']}")
    return result


def fetch_dsssb():
    """Visits every page in DSSSB_PAGES and collects notices from all of
    them. Pure HTTP-request based -- no Playwright/browser needed.

    HARD TIME BUDGET: DSSSB_TIME_BUDGET_SECONDS caps the ENTIRE DSSSB
    phase. If the budget runs out partway through, remaining pages are
    simply skipped for THIS run -- they get checked again next run.
    """
    start_time = time.monotonic()

    all_notices = []
    for category, url in DSSSB_PAGES.items():
        elapsed = time.monotonic() - start_time
        if elapsed > DSSSB_TIME_BUDGET_SECONDS:
            print(f"DEBUG: [DSSSB] Time budget ({DSSSB_TIME_BUDGET_SECONDS}s) reached "
                  f"after {elapsed:.0f}s -- skipping remaining page(s) this run: "
                  f"'{category}' onwards. They'll be checked again next run.")
            break
        print(f"DEBUG: ---- Checking [DSSSB/{category}] -> {url} ----")
        page_notices = fetch_generic_page(category, url, base_domain=DSSSB_BASE, source_label="DSSSB")
        all_notices.extend(page_notices)
        time.sleep(0.3)

    deduped = {}
    for n in all_notices:
        deduped.setdefault(n["link"], n)
    final = list(deduped.values())
    total_elapsed = time.monotonic() - start_time
    print(f"DEBUG: [DSSSB] Grand total: {len(all_notices)} raw, "
          f"{len(final)} unique after link-based de-dup. "
          f"(DSSSB phase took {total_elapsed:.0f}s)")
    return final


def fetch_regional():
    """Checks the homepage of each of SSC's 9 regional office websites
    (NR/NWR/CR/ER/NER/WR/MPR/SR/KKR), through the same proxy-fallback
    chain used for DSSSB (see module docstring for why: these are
    separate India-govt-hosted domains, likely to block GitHub Actions'
    datacenter IPs the same way DSSSB did).

    HARD TIME BUDGET: REGIONAL_TIME_BUDGET_SECONDS caps this whole phase;
    any regions not reached this run get picked up again next run.
    """
    start_time = time.monotonic()
    all_notices = []
    for source_label, info in REGIONAL_SSC_SITES.items():
        elapsed = time.monotonic() - start_time
        if elapsed > REGIONAL_TIME_BUDGET_SECONDS:
            print(f"DEBUG: [Regional] Time budget ({REGIONAL_TIME_BUDGET_SECONDS}s) reached "
                  f"after {elapsed:.0f}s -- skipping remaining region(s) this run: "
                  f"'{source_label}' onwards. They'll be checked again next run.")
            break
        print(f"DEBUG: ---- Checking [{source_label}/Home] -> {info['url']} ----")
        page_notices = fetch_generic_page("Home", info["url"], base_domain=info["base"], source_label=source_label)
        all_notices.extend(page_notices)
        time.sleep(0.3)

    deduped = {}
    for n in all_notices:
        deduped.setdefault(f"{n['source']}:{n['link']}", n)
    final = list(deduped.values())
    total_elapsed = time.monotonic() - start_time
    print(f"DEBUG: [Regional] Grand total: {len(all_notices)} raw, "
          f"{len(final)} unique after link-based de-dup. "
          f"(Regional phase took {total_elapsed:.0f}s)")
    return final


def merge_notices(*notice_lists):
    """Combines notices from any number of sources, tagging every item
    with its source (defaulting to 'SSC' if missing, for the main-SSC
    list which doesn't set one explicitly) and de-duplicating across all
    of them by a composite 'source:link' key."""
    combined = []
    for lst in notice_lists:
        for n in lst:
            n.setdefault("source", "SSC")
            combined.append(n)

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

SEEDED_SOURCES_KEY = "_seeded_sources"
DSSSB_RUN_COUNTER_KEY = "_dsssb_run_counter"
RESERVED_KEYS = (SEEDED_SOURCES_KEY, DSSSB_RUN_COUNTER_KEY)


def main():
    seen = load_seen()

    # One-time, automatic migration: old keys were plain links (SSC-only
    # era). Prefix them with "SSC:" so they're never re-treated as new.
    if seen and not all(k.startswith(("SSC:", "DSSSB:", "_")) for k in seen):
        seen = {(k if k.startswith(("SSC:", "DSSSB:", "_")) else f"SSC:{k}"): v
                for k, v in seen.items()}

    print(f"DEBUG: {len(seen)} notices already marked as seen from previous runs.")

    seen_signatures = set()
    for entry in seen.values():
        if isinstance(entry, dict) and "title" in entry and "category" in entry:
            seen_signatures.add(normalize_signature(entry["title"], entry["category"]))

    # ---- Which sources already have real history? Anything with existing
    # "<source>:<link>" keys in seen_notices.json was already being
    # tracked before this baseline-seeding feature existed, so it's
    # auto-marked "seeded" -- its genuinely-new notices keep flowing to
    # Telegram exactly as before, completely unaffected. ----
    seeded_entry = seen.get(SEEDED_SOURCES_KEY, {})
    seeded_sources = set(seeded_entry.get("list", [])) if isinstance(seeded_entry, dict) else set()
    for existing_key in seen:
        if existing_key.startswith(RESERVED_KEYS) or existing_key.startswith("_"):
            continue
        seeded_sources.add(existing_key.split(":", 1)[0])

    # ---- Chromium instance -- SSC main only (DSSSB/regional are pure
    # HTTP requests, no browser needed) ----
    print("DEBUG: ==== Launching Chromium for SSC main ====")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        print("DEBUG: ==== Fetching SSC main ====")
        ssc_notices = fetch_ssc(page)

        browser.close()
    print("DEBUG: ==== Chromium closed ====")

    # Throttled DSSSB check -- default (=1) checks every run, unchanged.
    counter_entry = seen.get(DSSSB_RUN_COUNTER_KEY, {})
    run_number = counter_entry.get("count", 0) if isinstance(counter_entry, dict) else 0
    seen[DSSSB_RUN_COUNTER_KEY] = {"count": run_number + 1}

    if run_number % DSSSB_CHECK_EVERY_N_RUNS == 0:
        print("DEBUG: ==== Fetching DSSSB ====")
        dsssb_notices = fetch_dsssb()
    else:
        print(f"DEBUG: ==== Skipping DSSSB this run (run #{run_number}, "
              f"checking every {DSSSB_CHECK_EVERY_N_RUNS} runs) ====")
        dsssb_notices = []

    if run_number % REGIONAL_CHECK_EVERY_N_RUNS == 0:
        print("DEBUG: ==== Fetching SSC Regional Offices ====")
        regional_notices = fetch_regional()
    else:
        print(f"DEBUG: ==== Skipping Regional this run (run #{run_number}, "
              f"checking every {REGIONAL_CHECK_EVERY_N_RUNS} runs) ====")
        regional_notices = []

    notices = merge_notices(ssc_notices, dsssb_notices, regional_notices)
    print(f"DEBUG: Total unique notice-like links across SSC + DSSSB + Regional = {len(notices)}")

    if not notices:
        print("No notices fetched this run (could be a temporary site issue).")
        sys.exit(0)

    new_count = 0
    seeded_this_run = set()
    seeded_counts = {}

    for n in notices:
        source = n.get("source", "SSC")
        key = f"{source}:{n['link']}"
        sig = normalize_signature(n["title"], n["category"])

        if key in seen:
            continue

        if source not in seeded_sources:
            # First time this source has EVER been seen -- this is the
            # pre-existing backlog already on the site, not something
            # newly published. Record it silently; do NOT alert Telegram.
            seen[key] = {"title": n["title"], "category": n["category"], "notified": True}
            seen_signatures.add(sig)
            seeded_this_run.add(source)
            seeded_counts[source] = seeded_counts.get(source, 0) + 1
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

    for source in seeded_this_run:
        seeded_sources.add(source)
        print(f"DEBUG: [{source}] First run for this source -- "
              f"{seeded_counts.get(source, 0)} existing notice(s) recorded as baseline, "
              f"none sent to Telegram. Future new notices from this source will be "
              f"sent instantly from the next run onwards.")

    seen[SEEDED_SOURCES_KEY] = {"list": sorted(seeded_sources)}

    if len(seen) > 1500:
        # Never drop the reserved bookkeeping keys during trim.
        reserved = {k: seen[k] for k in RESERVED_KEYS if k in seen}
        rest = {k: v for k, v in seen.items() if k not in RESERVED_KEYS}
        rest = dict(list(rest.items())[-1500:])
        seen = {**rest, **reserved}

    save_seen(seen)
    print(f"Done. {new_count} new notice(s) posted. "
          f"{sum(seeded_counts.values())} notice(s) silently baselined this run.")


if __name__ == "__main__":
    main()
