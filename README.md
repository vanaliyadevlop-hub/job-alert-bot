# Gujarat Job Digest Scraper

Pulls new Gujarat job postings from several sites into **one page**, checked
**every hour**, so you stop checking each site by hand — and see new
postings before pages that only check once a day do.

## What changed in this version

- **Scraping engine is now [Scrapling](https://github.com/D4Vinci/Scrapling)**
  instead of `requests` + `BeautifulSoup`. Two real upgrades:
  1. Its fetcher sends a realistic browser TLS fingerprint + headers, so
     it's less likely to get a flat 403 than a plain `requests.get()`.
  2. It has **adaptive element relocation**: the first time a source is
     scraped successfully, Scrapling remembers a fingerprint of what it
     found. If a later run's normal matching logic finds nothing — because
     the site redesigned — it searches the new page for whatever is most
     structurally/textually similar to that fingerprint, instead of just
     coming back empty. This was tested by hand against a simulated
     redesign (renamed classes, new wrapper tags, even a totally different
     URL scheme) and it correctly recovered every posting, including one
     that didn't exist in the original snapshot. See
     `test_parsers_offline.py::test_adaptive_survives_redesign` — it's a
     real, runnable test, not just a claim.
  This is **not** a guarantee against every possible redesign (a site
  abandoning tables for a JS-rendered grid entirely is still a real risk),
  but it meaningfully raises the bar before a source needs a human fix.
- **Runs hourly, not daily** (`.github/workflows/hourly-digest.yml`, cron
  `0 * * * *`). If you set up the previous daily version, delete
  `.github/workflows/daily-digest.yml` from your repo — this one replaces it.

## Sites, by category — what's scraped and what isn't

| Category | Site | Scraped? |
|---|---|---|
| Official state source | ojas.gujarat.gov.in | ❌ robots.txt disallows bots |
| Official state source | gpsc.gujarat.gov.in | ❌ robots.txt disallows bots |
| Official state source | gsssb.gujarat.gov.in | ❌ robots.txt disallows bots |
| Official central source | employmentnews.gov.in | ✅ |
| Gujarat aggregator | marugujarat.in — GSSSB / GPSC / OJAS Jobs / GPSSB | ✅ |
| Gujarat aggregator | mybhartigujarat.com — GSSSB / GPSC | ✅ |
| National aggregator | freejobalert.com — Gujarat govt jobs page | ✅ |
| Private/corporate | Naukri, LinkedIn, Indeed | ❌ ToS forbids scraping + login walls |

Reasoning for the ❌ rows is unchanged from before: the three official
Gujarat boards explicitly disallow bots in robots.txt (checked directly),
and their content still reaches you same-day via the aggregators above.
Naukri/LinkedIn/Indeed require logins and forbid scraping in their ToS —
use each platform's native job-alert email instead.

## What you get

Every run writes `docs/index.html`: one mobile-friendly page listing only
the postings you haven't seen before. Optionally, it also pushes a
Telegram message the moment new postings appear.

## Setup — Option A: GitHub Actions (recommended)

Runs on GitHub's servers, so it works even if your phone or laptop is off.

1. Create a free GitHub account if you don't have one.
2. Create a new repository and upload every file here, **keeping the folder
   structure** — `.github/workflows/hourly-digest.yml` must stay at that
   exact path.
3. Go to **Settings → Pages** → Source: "Deploy from a branch" → Branch:
   `main`, folder: `/docs` → Save. GitHub gives you a URL like
   `https://yourname.github.io/repo-name/` — bookmark it, that's your digest.
   (`docs/.nojekyll` is already included so this doesn't hit the Jekyll/SCSS
   build error some setups run into — see Troubleshooting below if it does.)
4. Go to the **Actions** tab → open "Hourly Gujarat Job Digest" → click
   **Run workflow** to trigger it manually once and confirm it works.
5. After that it runs automatically every hour. To change the frequency,
   edit the `cron:` line in `.github/workflows/hourly-digest.yml` — e.g.
   `0 */3 * * *` for every 3 hours if hourly turns out to be more than you need.

### A note on run cost
Public repos get unlimited free Actions minutes, so hourly costs nothing
extra. Private repos get 2,000 free minutes/month; this script (no browser
download needed — see below) typically finishes in under two minutes per
run, so hourly should land around 1,000–1,500 minutes/month, comfortably
inside the free tier, but worth knowing if you add heavier steps later.

### Optional: get it as a Telegram message instead of visiting the page
1. Message **@BotFather** on Telegram → `/newbot` → copy the token it gives you.
2. Message **@userinfobot** → copy your numeric chat ID.
3. In your repo: **Settings → Secrets and variables → Actions → New repository
   secret**. Add one named `TELEGRAM_BOT_TOKEN` and one named `TELEGRAM_CHAT_ID`.
4. The next run will message you directly with new postings.

## Setup — Option B: run it yourself, locally

```bash
pip install -r requirements.txt
python scraper.py
```
Then open `docs/index.html` in a browser. Note: this installs
`scrapling[fetchers]`, which is enough for everything this project uses.
You do **not** need to run `scrapling install` (that downloads full browser
binaries for Scrapling's browser-automation fetchers, which this project
doesn't use).

To get it automatically every hour:
- **Windows** — Task Scheduler → New Task → trigger "Repeat task every: 1
  hour" → action: `python C:\path\to\scraper.py`
- **Mac/Linux** — `crontab -e` → add: `0 * * * * cd /path/to/folder && python3 scraper.py`

## Troubleshooting: Pages build fails with a Jekyll/SCSS error

If the **pages-build-deployment** run in your Actions tab (a separate,
GitHub-generated workflow) fails with something like `Jekyll::Converters::Scss
encountered an error ... assets/css/style.scss`, that's GitHub defaulting to
treating `/docs` as a Jekyll site. `docs/.nojekyll` (already in this project)
prevents that. If you set the repo up before this file existed, add it
directly on GitHub: open the `docs` folder → **Add file → Create new file**
→ name it exactly `.nojekyll` → leave it empty → commit to `main`.

## Honest limitations, stated plainly

This was built by directly inspecting each site's real HTML/RSS structure,
testing the parsing logic offline against saved samples of that structure,
and testing Scrapling's adaptive relocation against a simulated redesign —
all of which pass (`test_parsers_offline.py`). What I could **not** do is
run this end-to-end against the *live* sites myself: my own sandbox can
only reach package registries (pypi, npm, github), not these job sites, so
I couldn't watch a real live run before handing it to you.

Run it for real once and skim `docs/index.html` or the terminal output.
Two things to look for:
- A source shows 0 new items when you're sure there should be some →
  that site likely tweaked its layout enough that even adaptive relocation
  couldn't bridge it. Tell me which source and what you see.
- Terminal output includes a line like `'<identifier>' relocated via
  adaptive fallback` → that source's normal heuristic broke and adaptive
  matching saved the run. Not an error — but worth knowing that source is
  now running on its fallback path and may be due for a proper fix.

## Maintenance note

Job sites redesign without warning, occasionally. Each source runs in
isolation (one breaking doesn't stop the others), adaptive relocation
absorbs many redesigns automatically, and the terminal output always tells
you exactly which source did what on every run.

## Files in this project

- `scraper.py` — the scraper (Scrapling-based) + digest builder + optional Telegram push
- `test_parsers_offline.py` — offline tests, including the redesign-survival test
- `requirements.txt` — Python dependencies
- `.github/workflows/hourly-digest.yml` — hourly automation
- `data/seen_jobs.json` — dedupe memory (auto-updates, don't hand-edit)
- `data/scrapling_elements.db` — Scrapling's adaptive fingerprints (auto-updates, don't hand-edit; must stay committed for adaptive matching to work across runs)
- `docs/index.html` — the digest page itself (auto-generated, don't hand-edit)
- `docs/.nojekyll` — tells GitHub Pages to serve `docs/` as-is, not as a Jekyll site
