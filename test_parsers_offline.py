"""
Offline sanity check for scraper.py's parsing logic. Does NOT hit the
internet -- feeds each scrape_* function sample HTML shaped like what the
real sites actually returned (verified by fetching them directly), by
monkeypatching polite_get(). This confirms the *parsing logic* is sound; it
can't confirm the live sites haven't changed their markup since -- run
scraper.py for real (see README) to confirm that part.

Also includes test_adaptive_survives_redesign, which is the important one:
it simulates a site redesign mid-test (renamed classes, new wrapper divs,
a link scheme changing entirely) and checks that Scrapling's adaptive
relocation still recovers the content when our own heuristic would have
returned nothing.
"""

import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import scraper
from scrapling.parser import Selector

TEST_DB_DIR = Path("/tmp/gujarat_digest_test")


def fresh_test_db(name: str) -> str:
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    db_path = TEST_DB_DIR / f"{name}.db"
    if db_path.exists():
        db_path.unlink()
    return str(db_path)


def page_from_html(html: str, url: str, db_name: str):
    """Build a Scrapling page exactly like polite_get() would return,
    but from a local HTML string instead of a real network fetch."""
    return Selector(html, url=url, adaptive=True, storage_args={"storage_file": fresh_test_db(db_name)})


FREEJOBALERT_SAMPLE = """
<table>
<tr><th>Post Date</th><th>Job Title</th><th>Post name</th><th>Vacancies</th>
<th>Qualification</th><th>Last Date</th><th>Notification</th></tr>
<tr>
  <td>26 Aug 2026</td>
  <td><a href="https://www.freejobalert.com/articles/test-job-1">Test Board Vacancy 2026 - 1 Clerk Posts</a></td>
  <td>Clerk</td><td>1 Posts</td><td>Any Graduate</td><td>05 Sep 2026</td>
  <td><a href="https://www.freejobalert.com/articles/test-job-1">View details</a></td>
</tr>
<tr>
  <td>25 Aug 2026</td>
  <td><a href="https://www.freejobalert.com/articles/test-job-2">Test Univ Vacancy 2026 - 5 Assistant Posts</a></td>
  <td>Assistant</td><td>5 Posts</td><td>M.Sc</td><td>10 Sep 2026</td>
  <td><a href="https://www.freejobalert.com/articles/test-job-2">View details</a></td>
</tr>
</table>
"""

MYBHARTI_SAMPLE = """
<div class="latest">
<a href="https://mybhartigujarat.com/Post/NDky/470-Posts-Test-Executive-Recruitment">470 Posts-Test Executive Recruitment</a>
<a href="https://mybhartigujarat.com/Post/NDkx/4029-Posts-Test-JE-Recruitment">4029 Posts-Test JE Recruitment</a>
<a href="https://mybhartigujarat.com/Contact/us">Contact us</a>
</div>
"""

EMPLOYMENT_NEWS_SAMPLE = """
<table>
<tr><td>ORGANISATION</td><td>POST</td><td>METHOD OF APPOINTMENT</td><td>LAST DATE (DD/MM/YYYY)</td></tr>
<tr>
  <td><a href="https://employmentnews.gov.in/test-org">TEST RESEARCH INSTITUTE</a></td>
  <td>SCIENTIST-B &amp; OTHERS</td><td>Recruitment</td><td>11/09/2026</td>
</tr>
</table>
"""

WP_HTML_FALLBACK_SAMPLE = """
<article>
  <h2 class="entry-title"><a href="https://www.marugujarat.in/2026/08/test-post-1">GSSSB Test Post 2026</a></h2>
</article>
<article>
  <h2 class="entry-title"><a href="https://www.marugujarat.in/2026/08/test-post-2">GSSSB Another Test Post 2026</a></h2>
</article>
"""


def test_table_scraper():
    page = page_from_html(FREEJOBALERT_SAMPLE, "https://www.freejobalert.com/gujarat-government-jobs/", "table")
    with patch("scraper.polite_get", return_value=page):
        items = scraper.scrape_table("https://www.freejobalert.com/gujarat-government-jobs/", "test_freejobalert")
    assert len(items) == 2, f"expected 2 rows, got {len(items)}"
    assert items[0]["title"] == "Test Board Vacancy 2026 - 1 Clerk Posts"
    assert items[0]["link"].startswith("https://www.freejobalert.com/articles/")
    print("scrape_table: PASS ->", items)


def test_custom_cms_scraper():
    page = page_from_html(MYBHARTI_SAMPLE, "https://mybhartigujarat.com/C/GSSSB-Recruitment", "cms")
    with patch("scraper.polite_get", return_value=page):
        items = scraper.scrape_custom_cms("https://mybhartigujarat.com/C/GSSSB-Recruitment", "test_mybharti")
    assert len(items) == 2, f"expected 2 /Post/ links, got {len(items)}"
    titles = [i["title"] for i in items]
    assert "470 Posts-Test Executive Recruitment" in titles
    assert not any("Contact us" in t for t in titles), "should not pick up non-/Post/ links"
    print("scrape_custom_cms: PASS ->", items)


def test_employment_news_scraper():
    page = page_from_html(EMPLOYMENT_NEWS_SAMPLE, "https://employmentnews.gov.in/newemp/Home.aspx", "empnews")
    with patch("scraper.polite_get", return_value=page):
        items = scraper.scrape_employment_news("https://employmentnews.gov.in/newemp/Home.aspx", "test_empnews")
    assert len(items) == 1, f"expected 1 row, got {len(items)}"
    assert items[0]["title"] == "TEST RESEARCH INSTITUTE"
    print("scrape_employment_news: PASS ->", items)


def test_wp_category_html_fallback():
    # Simulate: RSS feed parse returns no entries -> falls back to HTML h2 parsing
    empty_feed = MagicMock()
    empty_feed.entries = []
    page = page_from_html(WP_HTML_FALLBACK_SAMPLE, "https://www.marugujarat.in/category/gsssb/", "wpfallback")
    with patch("feedparser.parse", return_value=empty_feed), \
         patch("scraper.polite_get", return_value=page):
        items = scraper.scrape_wp_category("https://www.marugujarat.in/category/gsssb/", "test_marugujarat")
    assert len(items) == 2, f"expected 2 h2 posts, got {len(items)}"
    assert items[0]["title"] == "GSSSB Test Post 2026"
    print("scrape_wp_category (HTML fallback): PASS ->", items)


def test_wp_category_rss_path():
    # Simulate: RSS feed DOES return entries -> should use those, skip HTML entirely
    fake_entry = MagicMock()
    fake_entry.title = "GSSSB RSS Post 2026"
    fake_entry.link = "https://www.marugujarat.in/2026/08/rss-post"
    fake_feed = MagicMock()
    fake_feed.entries = [fake_entry]
    with patch("feedparser.parse", return_value=fake_feed):
        items = scraper.scrape_wp_category("https://www.marugujarat.in/category/gsssb/", "test_marugujarat_rss")
    assert len(items) == 1
    assert items[0]["title"] == "GSSSB RSS Post 2026"
    print("scrape_wp_category (RSS path): PASS ->", items)


def test_adaptive_survives_redesign():
    """The important one. Run 1 scrapes a normal page and Scrapling learns
    its fingerprint. Run 2 simulates a total redesign -- new tag, new
    classes, new URL scheme -- so our own '/Post/' heuristic finds NOTHING
    directly. Adaptive relocation + find_similar() should still recover
    every item, including a brand new one that didn't exist in Run 1."""
    db = fresh_test_db("redesign")
    url = "https://mybhartigujarat.com/C/GSSSB-Recruitment"

    html_v1 = (
        '<div><a href="https://mybhartigujarat.com/Post/1/Job-A">Job A</a>'
        '<a href="https://mybhartigujarat.com/Post/2/Job-B">Job B</a></div>'
    )
    page1 = Selector(html_v1, url=url, adaptive=True, storage_args={"storage_file": db})
    with patch("scraper.polite_get", return_value=page1):
        items_run1 = scraper.scrape_custom_cms(url, "test_redesign_survival")
    assert len(items_run1) == 2, f"run 1 sanity check failed: {items_run1}"
    print("Redesign test, run 1 (normal page):", [i["title"] for i in items_run1])

    # Total redesign: '/Post/' scheme replaced by '/News/' entirely, wrapped
    # in a <section> instead of a <div>, plus one brand-new posting.
    html_v2 = (
        '<section><a href="https://mybhartigujarat.com/News/1/Job-A">Job A</a>'
        '<a href="https://mybhartigujarat.com/News/2/Job-B">Job B</a>'
        '<a href="https://mybhartigujarat.com/News/3/Job-C">Job C</a></section>'
    )
    page2 = Selector(html_v2, url=url, adaptive=True, storage_args={"storage_file": db})

    # Confirm our heuristic genuinely breaks against the redesigned page --
    # otherwise this test would pass for the wrong reason.
    direct_hits = [a for a in page2.css("a") if "/Post/" in (a.attrib.get("href") or "")]
    assert len(direct_hits) == 0, "heuristic should find nothing directly after the simulated redesign"

    with patch("scraper.polite_get", return_value=page2):
        items_run2 = scraper.scrape_custom_cms(url, "test_redesign_survival")

    titles_run2 = sorted(i["title"] for i in items_run2)
    print("Redesign test, run 2 (heuristic broken, relying on adaptive):", titles_run2)
    assert titles_run2 == ["Job A", "Job B", "Job C"], (
        f"expected all 3 postings recovered via adaptive relocation, got {titles_run2}"
    )
    print("test_adaptive_survives_redesign: PASS")


def test_dedupe_and_html_build():
    test_dir = Path("/tmp/digest_test")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True)

    scraper.DATA_DIR = test_dir / "data"
    scraper.SEEN_FILE = scraper.DATA_DIR / "seen_jobs.json"
    scraper.OUTPUT_HTML = test_dir / "docs" / "index.html"

    fake_new = {
        "FreeJobAlert — Gujarat Govt Jobs": [
            {"title": "Sample Job A", "link": "https://example.com/a", "source": "x", "first_seen": "now"}
        ]
    }
    total = scraper.build_html(fake_new)
    assert total == 1
    assert scraper.OUTPUT_HTML.exists()
    html_content = scraper.OUTPUT_HTML.read_text()
    assert "Sample Job A" in html_content
    assert "1 new" in html_content
    print("build_html: PASS -> wrote", scraper.OUTPUT_HTML, f"({len(html_content)} chars)")

    seen = {}
    uid = scraper.make_id("Sample Job A", "https://example.com/a")
    seen[uid] = fake_new["FreeJobAlert — Gujarat Govt Jobs"][0]
    scraper.save_seen(seen)
    reloaded = scraper.load_seen()
    assert uid in reloaded
    print("dedupe save/load: PASS")

    shutil.rmtree(test_dir)


if __name__ == "__main__":
    test_table_scraper()
    test_custom_cms_scraper()
    test_employment_news_scraper()
    test_wp_category_html_fallback()
    test_wp_category_rss_path()
    test_adaptive_survives_redesign()
    test_dedupe_and_html_build()
    if TEST_DB_DIR.exists():
        shutil.rmtree(TEST_DB_DIR)
    print("\nAll offline parser tests passed.")
