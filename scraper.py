#!/usr/bin/env python3
"""
Gujarat Job Digest Scraper — Scrapling edition
================================================
Pulls new job postings from several sites into ONE HTML digest (+ optional
Telegram push). Runs hourly (see .github/workflows/hourly-digest.yml).

WHY SCRAPLING (https://github.com/D4Vinci/Scrapling) INSTEAD OF requests+bs4:
Scrapling's fetchers send browser-realistic TLS fingerprints/headers (fewer
403s than plain `requests`), and its parser can *adaptively relocate*
elements: the first time we successfully find something (say, the results
table), we save a fingerprint of it. If a later run's selector/heuristic
finds nothing -- because the site redesigned -- Scrapling searches the new
page for the element that's most structurally/textually similar to that
saved fingerprint, instead of just coming back empty. This was tested by
hand against a simulated redesign (renamed classes, new wrapper divs, even
a totally different URL scheme) and it recovered the content correctly;
see test_parsers_offline.py::test_adaptive_survives_redesign.

It is not magic -- a site abandoning tables for a JS-rendered grid entirely
is still a real risk -- but it materially raises the bar before a source
needs a human to fix its selector.

SOURCES SCRAPED DIRECTLY:
  - FreeJobAlert.com        (national aggregator, Gujarat table)
  - MaruGujarat.in          (GSSSB / GPSC / OJAS-jobs / GPSSB categories)
  - MyBhartiGujarat.com     (GSSSB / GPSC categories)
  - EmploymentNews.gov.in   (official GoI weekly job highlights)

DELIBERATELY NOT SCRAPED: ojas.gujarat.gov.in, gpsc.gujarat.gov.in,
gsssb.gujarat.gov.in. Their robots.txt disallows automated access, checked
by hand before any code was written. Their notifications already show up
on the aggregators above same-day, so nothing is functionally lost.

PERSISTENCE: two files under data/ carry memory between runs and MUST be
committed back to the repo each time (the workflow file already does this):
  - seen_jobs.json          which postings you've already been shown
  - scrapling_elements.db   Scrapling's adaptive fingerprints (SQLite)
If these get wiped, the scraper still works -- it just starts "fresh",
re-showing everything once and re-learning fingerprints from scratch.
"""

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests  # only for the optional Telegram push -- not used for scraping
from scrapling.fetchers import Fetcher

# ---------------------------------------------------------------- #
# CONFIG
# ---------------------------------------------------------------- #

REQUEST_DELAY = 2        # seconds of politeness between requests
REQUEST_TIMEOUT = 15     # seconds before giving up on one page
ADAPTIVE_PERCENTAGE = 40  # Scrapling's minimum similarity score to accept a relocation
MAX_ITEMS_PER_SOURCE_MSG = 12  # cap in Telegram message so it doesn't get huge

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SEEN_FILE = DATA_DIR / "seen_jobs.json"
ADAPTIVE_DB = DATA_DIR / "scrapling_elements.db"
OUTPUT_HTML = BASE_DIR / "docs" / "index.html"

# Turn on Scrapling's adaptive tracking for every fetch made through
# `Fetcher`, and point its memory at a file inside data/ (default would be
# inside the installed package, which is useless on throwaway CI runners).
Fetcher.adaptive = True
Fetcher.storage_args = {"storage_file": str(ADAPTIVE_DB)}

SOURCES = [
    {
        "name": "FreeJobAlert — Gujarat Govt Jobs",
        "type": "table",
        "url": "https://www.freejobalert.com/gujarat-government-jobs/",
        "category": "National aggregator",
        "identifier": "freejobalert_gj_table",
    },
    {
        "name": "MaruGujarat — GSSSB",
        "type": "wp_category",
        "url": "https://www.marugujarat.in/category/gsssb/",
        "category": "Gujarat aggregator",
        "identifier": "marugujarat_gsssb_posts",
    },
    {
        "name": "MaruGujarat — GPSC",
        "type": "wp_category",
        "url": "https://www.marugujarat.in/gpsc/",
        "category": "Gujarat aggregator",
        "identifier": "marugujarat_gpsc_posts",
    },
    {
        "name": "MaruGujarat — OJAS Jobs",
        "type": "wp_category",
        "url": "https://www.marugujarat.in/ojas-jobs",
        "category": "Gujarat aggregator",
        "identifier": "marugujarat_ojas_posts",
    },
    {
        "name": "MaruGujarat — GPSSB",
        "type": "wp_category",
        "url": "https://www.marugujarat.in/category/gpssb/",
        "category": "Gujarat aggregator",
        "identifier": "marugujarat_gpssb_posts",
    },
    {
        "name": "MyBhartiGujarat — GSSSB",
        "type": "custom_cms",
        "url": "https://mybhartigujarat.com/C/GSSSB-Recruitment",
        "category": "Gujarat aggregator",
        "identifier": "mybharti_gsssb_posts",
    },
    {
        "name": "MyBhartiGujarat — GPSC",
        "type": "custom_cms",
        "url": "https://mybhartigujarat.com/C/Gujarat-Public-Service-Commission-(GPSC)-Recruitment",
        "category": "Gujarat aggregator",
        "identifier": "mybharti_gpsc_posts",
    },
    {
        "name": "Employment News (Govt of India)",
        "type": "employment_news",
        "url": "https://employmentnews.gov.in/newemp/Home.aspx",
        "category": "Official — Central Govt",
        "identifier": "employment_news_table",
    },
]

# ---------------------------------------------------------------- #
# LOW-LEVEL HELPERS
# ---------------------------------------------------------------- #


def polite_get(url: str):
    """Fetch a URL through Scrapling with a realistic browser fingerprint,
    a timeout, and a short pause afterwards so we never hammer someone
    else's server. Returns Scrapling's page object -- queryable directly
    with .css()/.xpath() -- or raises if the site didn't return 200."""
    page = Fetcher.get(url, timeout=REQUEST_TIMEOUT, impersonate="chrome", stealthy_headers=True)
    time.sleep(REQUEST_DELAY)
    if page.status != 200:
        raise RuntimeError(f"HTTP {page.status}")
    return page


def make_id(title: str, link: str) -> str:
    """Stable short fingerprint for a (title, link) pair, used to spot
    postings we've already shown before."""
    return hashlib.sha256(f"{title}|{link}".encode("utf-8")).hexdigest()[:16]


def normalize_link(link: str, base_url: str) -> str:
    if link.startswith("http"):
        return link
    return urljoin(base_url, link)


def find_container_with_fallback(page, identifier: str, finder_fn):
    """For 'find the ONE container element' cases (e.g. the results table).
    Tries `finder_fn(page)` (our own heuristic) first; if that finds
    nothing, asks Scrapling to relocate the last known-good container by
    similarity. Returns a single element or None."""
    found = finder_fn(page)
    if found:
        try:
            page.save(found, identifier)
        except Exception as e:
            print(f"    [!] could not save adaptive fingerprint for '{identifier}': {e}")
        return found

    saved = page.retrieve(identifier)
    if saved:
        relocated = page.relocate(saved, ADAPTIVE_PERCENTAGE, True)
        if relocated:
            print(f"    [i] '{identifier}' container relocated via adaptive fallback (heuristic found nothing)")
            return relocated[0]
    return None


def find_list_with_fallback(page, identifier: str, finder_fn):
    """For 'find MANY matching items' cases (e.g. every job link on the
    page). Tries `finder_fn(page)` first; if that finds nothing, relocates
    the last known-good item by similarity, then expands to its structural
    siblings with find_similar() to recover the rest of the list too."""
    found = finder_fn(page)
    if found:
        try:
            page.save(found[0], identifier)
        except Exception as e:
            print(f"    [!] could not save adaptive fingerprint for '{identifier}': {e}")
        return found

    saved = page.retrieve(identifier)
    if saved:
        relocated = page.relocate(saved, ADAPTIVE_PERCENTAGE, True)
        if relocated:
            print(f"    [i] '{identifier}' relocated via adaptive fallback, expanding to siblings...")
            expanded = relocated[0].find_similar()
            combined = list(relocated) + [e for e in expanded if e not in relocated]
            return combined if combined else list(relocated)
    return []


# ---------------------------------------------------------------- #
# PER-SOURCE-TYPE SCRAPERS
# Each returns a list of {"title": str, "link": str} dicts.
# Each is wrapped in try/except by the caller, so one broken source
# never stops the others from running.
# ---------------------------------------------------------------- #


def scrape_table(url: str, identifier: str) -> list:
    """Pages that list jobs in an HTML <table> (FreeJobAlert-style).
    Heuristic: the table whose header row mentions job-ish column names --
    resilient to a visual redesign, since we're matching on text, not a
    CSS class. Backed by adaptive relocation if that heuristic ever fails."""
    items = []
    page = polite_get(url)

    def finder(p):
        for table in p.css("table"):
            rows = table.css("tr")
            header_text = rows[0].get_all_text(strip=True).lower() if rows else ""
            if any(k in header_text for k in ("job title", "notification", "post name")):
                return table
        return None

    target_table = find_container_with_fallback(page, identifier, finder)
    if target_table is None:
        return items

    for row in target_table.css("tr")[1:]:
        title = row.css("a::text").get()
        link = row.css("a::attr(href)").get()
        if title and link:
            items.append({"title": title.strip(), "link": link})
    return items


def scrape_wp_category(url: str, identifier: str) -> list:
    """WordPress category pages (MaruGujarat). Tries the site's RSS feed
    first -- WordPress exposes one at <url>/feed/ by default, and an RSS
    <item>/<title>/<link> structure is far more stable long-term than any
    HTML heuristic. Falls back to scraping post headlines (with adaptive
    relocation backing that heuristic too) if no feed is found."""
    feed_url = url.rstrip("/") + "/feed/"
    try:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            items = []
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                link = getattr(entry, "link", "")
                if title and link:
                    items.append({"title": title, "link": link})
            if items:
                return items
    except Exception:
        pass  # fall through to HTML scraping below

    page = polite_get(url)

    def finder(p):
        found = []
        for heading in list(p.css("h2")) + list(p.css("h3")):
            link_el = heading.css("a")
            if link_el and link_el[0].css("::text").get():
                found.append(link_el[0])
        return found

    links = find_list_with_fallback(page, identifier, finder)
    items = []
    for a in links:
        title = a.css("::text").get()
        link = a.attrib.get("href")
        if title and link:
            items.append({"title": title.strip(), "link": link})
    return items


def scrape_custom_cms(url: str, identifier: str) -> list:
    """MyBhartiGujarat-style sites: every job post URL contains '/Post/'.
    Matching on the URL shape survives a visual redesign much better than
    matching a <div class="..."> a theme update can rename -- and adaptive
    relocation covers the rarer case where the URL scheme itself changes."""
    page = polite_get(url)

    def finder(p):
        return [a for a in p.css("a") if "/Post/" in (a.attrib.get("href") or "") and a.css("::text").get()]

    links = find_list_with_fallback(page, identifier, finder)
    items = []
    for a in links:
        title = a.css("::text").get()
        link = a.attrib.get("href")
        if title and link:
            items.append({"title": title.strip(), "link": link})
    return items


def scrape_employment_news(url: str, identifier: str) -> list:
    """employmentnews.gov.in 'JOB HIGHLIGHTS' table on the homepage."""
    items = []
    page = polite_get(url)

    def finder(p):
        for table in p.css("table"):
            header_text = table.get_all_text(strip=True).lower()
            if "organisation" in header_text and "method of appointment" in header_text:
                return table
        return None

    target_table = find_container_with_fallback(page, identifier, finder)
    if target_table is None:
        return items

    for row in target_table.css("tr"):
        cells = row.css("td")
        if not cells:
            continue
        link_el = row.css("a")
        title = (link_el[0].css("::text").get() if link_el else "") or cells[0].get_all_text(strip=True)
        link = link_el[0].attrib.get("href") if link_el else url
        if title and title.strip().lower() not in ("organisation", "post"):
            items.append({"title": title.strip(), "link": link})
    return items


SCRAPERS = {
    "table": scrape_table,
    "wp_category": scrape_wp_category,
    "custom_cms": scrape_custom_cms,
    "employment_news": scrape_employment_news,
}

# ---------------------------------------------------------------- #
# DEDUPE STORE
# ---------------------------------------------------------------- #


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("  [!] seen_jobs.json was corrupted, starting fresh")
    return {}


def save_seen(seen: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- #
# MAIN RUN
# ---------------------------------------------------------------- #


def run() -> dict:
    """Scrape every source, return only the postings not seen in a
    previous run, grouped by source name. Updates seen_jobs.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen = load_seen()
    new_items_by_source = {}
    now = datetime.now().isoformat(timespec="seconds")

    for source in SOURCES:
        print(f"Scraping: {source['name']} ({source['url']})")
        scraper_fn = SCRAPERS[source["type"]]
        try:
            raw_items = scraper_fn(source["url"], source["identifier"])
            print(f"  -> {len(raw_items)} listing(s) found on page")
        except Exception as e:
            print(f"  [!] source failed, skipping: {e}")
            raw_items = []

        fresh = []
        for item in raw_items:
            link = normalize_link(item["link"], source["url"])
            title = item["title"]
            uid = make_id(title, link)
            if uid not in seen:
                seen[uid] = {
                    "title": title,
                    "link": link,
                    "source": source["name"],
                    "first_seen": now,
                }
                fresh.append(seen[uid])

        if fresh:
            new_items_by_source[source["name"]] = fresh

    save_seen(seen)
    return new_items_by_source


# ---------------------------------------------------------------- #
# OUTPUT: HTML DIGEST
# ---------------------------------------------------------------- #


def build_html(new_items_by_source: dict) -> int:
    date_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    total = sum(len(v) for v in new_items_by_source.values())

    if not new_items_by_source:
        sections = "<p class='empty'>No new postings since the last run — you're already caught up. ✅</p>"
    else:
        blocks = []
        for source_name, items in new_items_by_source.items():
            rows = "".join(
                f"<li><a href='{i['link']}' target='_blank' rel='noopener'>{i['title']}</a></li>"
                for i in items
            )
            blocks.append(
                f"<h2>{source_name} <span class='count'>{len(items)} new</span></h2><ul>{rows}</ul>"
            )
        sections = "".join(blocks)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gujarat Job Digest — {date_str}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; max-width: 700px;
         margin: 0 auto; padding: 24px 18px 60px; background:#fafafa; color:#1a1a1a; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  h2 {{ font-size: 1.05rem; margin-top: 30px; border-bottom: 2px solid #eee; padding-bottom: 6px; }}
  .count {{ color:#fff; background:#d9480f; font-size:.7rem; padding:2px 9px; border-radius:10px; margin-left:6px; }}
  ul {{ padding-left: 1.2rem; }}
  li {{ margin-bottom: 10px; line-height: 1.4; }}
  a {{ color:#1155cc; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .meta {{ color:#666; font-size:.85rem; }}
  .empty {{ color:#2f9e44; font-weight:500; }}
  hr {{ border:none; border-top:1px solid #e5e5e5; margin:36px 0 14px; }}
</style>
</head>
<body>
  <h1>📋 Gujarat Job Digest</h1>
  <p class="meta">Updated {date_str} · checked hourly · {total} new posting(s) this run</p>
  {sections}
  <hr>
  <p class="meta">Auto-generated from public sources. Always confirm details on the linked notification before applying or posting.</p>
</body>
</html>"""

    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    return total


# ---------------------------------------------------------------- #
# OUTPUT: OPTIONAL TELEGRAM PUSH
# Only runs if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set as
# environment variables (or GitHub Actions secrets). Silently does
# nothing otherwise -- this feature is optional, not required.
# ---------------------------------------------------------------- #


def send_telegram(new_items_by_source: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return

    total = sum(len(v) for v in new_items_by_source.values())
    if total == 0:
        return

    lines = [f"*Gujarat Job Digest* — {total} new posting(s)", ""]
    for source, items in new_items_by_source.items():
        lines.append(f"*{source}*")
        for i in items[:MAX_ITEMS_PER_SOURCE_MSG]:
            safe_title = i["title"].replace("[", "(").replace("]", ")")
            lines.append(f"• [{safe_title}]({i['link']})")
        if len(items) > MAX_ITEMS_PER_SOURCE_MSG:
            lines.append(f"...and {len(items) - MAX_ITEMS_PER_SOURCE_MSG} more on the digest page")
        lines.append("")

    message = "\n".join(lines)[:4000]  # stay under Telegram's message size limit

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  [!] Telegram API returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  [!] Telegram send failed: {e}")


# ---------------------------------------------------------------- #

if __name__ == "__main__":
    print(f"=== Gujarat Job Digest run: {datetime.now().isoformat(timespec='seconds')} ===")
    new_items = run()
    total_new = build_html(new_items)
    send_telegram(new_items)
    print(f"Done. {total_new} new item(s) total. Digest written to {OUTPUT_HTML}")
