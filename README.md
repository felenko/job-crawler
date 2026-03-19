# Job Crawler

Scrapes company career pages for **Senior / Staff / Principal Software Engineer** positions.

Uses a headless Chromium browser (Playwright) to render JavaScript-heavy pages, automatically
clicks "Open Positions / Browse Jobs / See open roles" CTAs, handles pagination, and saves each
matching job as a Markdown file.

## Scraping strategies

For each company the crawler auto-detects the best approach:

| Strategy | Trigger | How it works |
|---|---|---|
| **Workday API** | `myworkdayjobs.com` | POST JSON API — no browser, very fast |
| **Greenhouse API** | `greenhouse.io` | GET public JSON API |
| **Lever API** | `lever.co` | GET public JSON API |
| **Ashby API** | `ashbyhq.com` | GraphQL API |
| **Microsoft API** | `careers.microsoft.com` | GET `apply.careers.microsoft.com/api/pcsx/search` |
| **SmartRecruiters API** | `smartrecruiters.com` or mapped custom domains | GET `api.smartrecruiters.com/v1/companies/{slug}/postings` |
| **Salesforce XML** | `careers.salesforce.com` | GET XML feed with full job descriptions |
| **Playwright** | Everything else | Full headless browser with stealth |

API strategies are tried first — faster, more reliable, and invisible to bot-detection. If the URL doesn't match any known platform, Playwright is used as fallback. The active strategy is logged per company: `▶  NVIDIA  [workday]  https://...`

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Output structure

```
Jobs/
  OpenAI/
    .last_scan               ← timestamp + job count from last successful run
    Senior_Software_Engineer_Platform.md
    Staff_Engineer_Infrastructure.md
  Anthropic/
    .last_empty              ← written when page loaded but no matching jobs found
    ...
  SomeBrokenSite/
    .last_error              ← written when the page failed to load
```

Each `.md` file contains:
- Direct **Apply URL**
- Job listing URL
- Full job description text

## Basic usage

```bash
# Scrape all companies in seeds_test.txt, save to Jobs/
python job_crawler.py seeds_test.txt

# Custom output folder
python job_crawler.py seeds_test.txt --output Jobs

# Verbose (shows every nav click, link found, debug detail)
python job_crawler.py seeds_test.txt --verbose

# Limit to 10 jobs per company (useful for testing)
python job_crawler.py seeds_test.txt --max-jobs 10
```

## Freshness / skip controls

Avoid redundant re-scraping on repeated runs:

```bash
# Skip companies whose .last_scan is younger than 24 hours
python job_crawler.py seeds_test.txt --fresh-hours 24

# Don't re-check companies that returned zero jobs for 72 hours
# (career pages rarely post new roles within hours)
python job_crawler.py seeds_test.txt --retry-empty-hours 72

# Retry companies that had a load error after 6 hours
# (handles transient network/bot-protection issues)
python job_crawler.py seeds_test.txt --retry-error-hours 6

# Typical daily run — combine all three
python job_crawler.py seeds_test.txt \
    --fresh-hours 20 \
    --retry-empty-hours 72 \
    --retry-error-hours 6
```

## Location filtering

Jobs are filtered by location **on the detail page** (dedicated location fields, meta tags, etc.).

- If a location is found and **matches** → saved
- If a location is found and **doesn't match** → skipped (logged as `✗ Location mismatch`)
- If **no location** can be determined → kept (never reject a job we can't locate)

```bash
# Default locations: US, USA, United States, America, NY, New York, NYC, Remote, Hybrid
python job_crawler.py seeds_test.txt

# Override with a custom list (comma-separated)
python job_crawler.py seeds_test.txt --locations "US,New York,Remote"

# Add a city without losing the defaults — list them all explicitly
python job_crawler.py seeds_test.txt \
    --locations "US,USA,United States,NY,New York,NYC,Remote,Hybrid,Chicago,Austin"

# Disable location filtering entirely — save every matching job title
python job_crawler.py seeds_test.txt --no-location-filter
```

The saved `.md` file includes a **Location** line when the location was detected:
```
# Senior Software Engineer, Platform

**Apply URL:** https://...
**Job Listing URL:** https://...
**Location:** New York, NY

---
...
```

## Debugging — visible browser mode

If a site works in a real browser but the crawler fails to find buttons or jobs, run with
`--no-headless` to watch the browser in real time:

```bash
python job_crawler.py seeds_test.txt --no-headless --verbose
```

The crawler also now logs every nav-hint candidate it finds on the page (text, tag, score, href),
so even in headless mode you can see exactly what buttons were detected and which one was chosen.

## Tuning page load behaviour

```bash
# Wait longer for slow JS-heavy pages (ms after networkidle)
python job_crawler.py seeds_test.txt --wait 5000

# Increase politeness delay between individual job page loads (seconds)
python job_crawler.py seeds_test.txt --delay 2.0
```

## All options

| Option | Default | Description |
|---|---|---|
| `seed_file` | *(required)* | Text file with one career URL per line (`#` = comment) |
| `--output` / `-o` | `Jobs` | Root output folder |
| `--max-jobs` | `0` (unlimited) | Max jobs to save per company |
| `--delay` | `1.0` | Seconds between individual job-page loads |
| `--wait` | `3000` | Extra ms after page load for JS to settle |
| `--fresh-hours` | `0` (always rescan) | Skip if `.last_scan` is younger than N hours |
| `--retry-empty-hours` | `0` (always retry) | Skip if `.last_empty` is younger than N hours |
| `--retry-error-hours` | `0` (always retry) | Skip if `.last_error` is younger than N hours |
| `--locations` | *(see defaults)* | Comma-separated location keywords to accept |
| `--no-location-filter` | off | Disable location filtering — save all matching titles |
| `--verbose` / `-v` | off | Show debug output |

## Scan markers

Three hidden marker files are maintained inside each company folder:

| File | Meaning |
|---|---|
| `.last_scan` | Last successful scrape — records timestamp and number of jobs saved |
| `.last_empty` | Last scrape that loaded OK but found zero matching positions |
| `.last_error` | Last scrape that failed to load the page entirely |

A successful scan automatically removes any stale `.last_empty` / `.last_error` markers.
