"""
JobCrawler: Scrapes career pages for Senior / Staff / Principal Software Engineer positions.

For each seed URL (a company careers page):
  1. Checks scan markers — skips if scraped recently enough (--fresh-hours)
  2. Navigates to job listings (clicks CTA buttons if needed)
  3. Finds jobs matching Senior / Staff / Principal + Software Engineer
  4. Filters by location (default: US / New York / Remote) — configurable via --locations
  5. Saves each job's description + apply URL to Jobs/<Company>/<Title>.md
  6. Writes scan markers recording outcome and timestamp

Scan marker files (inside Jobs/<Company>/):
  .last_scan        — written after every successful scan (timestamp + job count)
  .last_empty       — written when page loaded but zero matching jobs found
  .last_error       — written when the page failed to load / threw an exception

Usage:
    python job_crawler.py seeds_test.txt
    python job_crawler.py seeds_test.txt --output Jobs --verbose
    python job_crawler.py seeds_test.txt --output Jobs --max-jobs 5 --verbose
    python job_crawler.py seeds_test.txt --fresh-hours 24          # skip if scanned < 24 h ago
    python job_crawler.py seeds_test.txt --retry-empty-hours 48    # re-scan empties after 48 h
    python job_crawler.py seeds_test.txt --retry-error-hours 6     # re-scan errors after 6 h
    python job_crawler.py seeds_test.txt --locations "US,New York,Remote,San Francisco"
    python job_crawler.py seeds_test.txt --no-location-filter      # save jobs regardless of location
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ── Matching patterns ──────────────────────────────────────────────────────────

SENIORITY_RE = re.compile(r'\b(Senior|Sr\.?|Staff|Principal)\b', re.IGNORECASE)

ROLE_RE = re.compile(
    r'\b(Software\s*Engineer|Software\s*Developer|SWE|'
    r'Backend\s*Engineer|Frontend\s*Engineer|Full[\s\-]?Stack\s*Engineer|'
    r'Platform\s*Engineer|Infrastructure\s*Engineer|'
    r'Site\s*Reliability\s*Engineer|SRE|'
    r'Machine\s*Learning\s*Engineer|ML\s*Engineer|'
    r'Systems?\s*Engineer|Security\s*Engineer|Data\s*Engineer|'
    r'Application\s*Engineer|Cloud\s*Engineer|DevOps\s*Engineer)\b',
    re.IGNORECASE,
)

# ── Navigation CTA patterns ────────────────────────────────────────────────────
# Broad, creative set — matches what real career pages actually say.
NAV_HINTS_RE = re.compile(
    r'('
    # "See / View / Explore / Browse / Find / Search" + target noun
    r'(see|view|explore|browse|find|search|discover|check\s+out|check)\s+'
    r'(all\s+)?(open\s+)?(our\s+)?'
    r'(jobs?|positions?|roles?|openings?|opportunities?|vacancies|listings?|careers?)'
    r'|'
    # Noun-first variants: "Open Positions", "Current Openings", "Job Listings" etc.
    r'(open|current|available|latest|new)\s+(positions?|roles?|openings?|jobs?|vacancies)'
    r'|'
    # Action phrases
    r'(apply\s+now|join\s+us|join\s+our\s+team|work\s+with\s+us|'
    r'career\s+opportunities?|job\s+opportunities?|'
    r'all\s+jobs?|all\s+roles?|all\s+positions?|all\s+openings?|'
    r'job\s+search|search\s+jobs?|search\s+positions?|search\s+roles?|'
    r'start\s+(your\s+)?search|explore\s+careers?)'
    r')',
    re.IGNORECASE,
)

LOAD_MORE_RE = re.compile(
    r'\b(load\s+more|show\s+more|view\s+more|see\s+more|more\s+(jobs?|roles?|results?)|'
    r'next\s+page|next)\b',
    re.IGNORECASE,
)

ATS_DOMAINS = {
    'greenhouse.io', 'lever.co', 'workday.com', 'myworkdayjobs.com',
    'icims.com', 'taleo.net', 'smartrecruiters.com', 'jobvite.com',
    'ashbyhq.com', 'recruitee.com', 'breezy.hr', 'bamboohr.com',
    'workable.com', 'jazz.hr', 'rippling.com', 'applytojob.com',
    'hire.withgoogle.com', 'careers.google.com',
}

COMPANY_NAMES: dict[str, str] = {
    'openai': 'OpenAI', 'anthropic': 'Anthropic', 'databricks': 'Databricks',
    'snowflake': 'Snowflake', 'notion': 'Notion', 'stripe': 'Stripe',
    'roblox': 'Roblox', 'scale': 'Scale', 'palantir': 'Palantir',
    'nvidia': 'NVIDIA', 'janestreet': 'JaneStreet',
    'hudsonrivertrading': 'HudsonRiverTrading', 'twosigma': 'TwoSigma',
    'citadel': 'Citadel', 'citadelsecurities': 'CitadelSecurities',
    'jumptrading': 'JumpTrading', 'drw': 'DRW', 'imc': 'IMC',
    'optiver': 'Optiver', 'tower-research': 'TowerResearch',
    'google': 'Google', 'metacareers': 'Meta', 'apple': 'Apple',
    'amazon': 'Amazon', 'netflix': 'Netflix', 'microsoft': 'Microsoft',
    'uber': 'Uber', 'lyft': 'Lyft', 'airbnb': 'Airbnb',
    'doordash': 'DoorDash', 'figma': 'Figma', 'rippling': 'Rippling',
    'gusto': 'Gusto', 'brex': 'Brex', 'ramp': 'Ramp', 'chime': 'Chime',
    'plaid': 'Plaid', 'instacart': 'Instacart', 'coinbase': 'Coinbase',
    'block': 'Block', 'salesforce': 'Salesforce', 'servicenow': 'ServiceNow',
    'workday': 'Workday', 'vmware': 'VMware', 'cloudflare': 'Cloudflare',
    'mongodb': 'MongoDB', 'elastic': 'Elastic', 'confluent': 'Confluent',
    'twilio': 'Twilio', 'dropbox': 'Dropbox',
}

# ── Location filtering ────────────────────────────────────────────────────────

# Default locations we care about — passed as --locations on CLI
DEFAULT_LOCATIONS = [
    "US", "USA", "United States", "America",
    "NY", "New York", "New York City", "NYC",
    "Remote", "Remote US", "Remote (US)", "Hybrid",
]

def build_location_re(locations: list[str]) -> re.Pattern:
    """Compile a single regex that matches any of the given location strings."""
    parts = [re.escape(loc.strip()) for loc in locations if loc.strip()]
    return re.compile(r'\b(' + '|'.join(parts) + r')\b', re.IGNORECASE)


# CSS selectors used to find the location field on a job detail page
_LOCATION_SELECTORS = [
    '[class*="job-location"]',
    '[class*="jobLocation"]',
    '[class*="posting-location"]',           # Lever
    '[data-automation="jobPostingLocation"]', # Workday
    '[class*="workplace-type"]',
    '[class*="office-location"]',
    '[itemprop="addressLocality"]',
    '[itemprop="jobLocation"]',
    # Broader fallbacks — tried last to reduce false positives
    '[class*="location"]',
    '[id*="location"]',
    '[class*="office"]',
    '[class*="city"]',
]

# Words that prove a location string is actually a location, not a UI element.
# If none match, we discard the extracted text as a false positive.
_LOC_SANITY_RE = re.compile(
    r'\b('
    r'remote|hybrid|on[\s\-]?site|in[\s\-]?office|'   # work-style words
    r'US|USA|United\s+States|America|Canada|UK|'
    r'[A-Z]{2}'                                         # state / country abbreviation
    r')\b'
    r'|'
    r'\b\d{5}\b'                                        # US zip code
    r'|'
    r',\s*[A-Z][a-z]',                                  # "City, State" pattern
    re.IGNORECASE,
)

# ── Marker filenames inside each company folder ───────────────────────────────
MARKER_SCAN  = '.last_scan'
MARKER_EMPTY = '.last_empty'
MARKER_ERROR = '.last_error'


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("job_crawler")


# ── Scan markers ──────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _read_marker(path: Path) -> dict | None:
    """Read a JSON marker file; return None if missing or corrupt."""
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _write_marker(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data['timestamp'] = _now_utc().isoformat()
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')


def _hours_since_marker(marker_path: Path) -> float | None:
    """Return hours elapsed since the marker was written, or None if absent."""
    m = _read_marker(marker_path)
    if not m or 'timestamp' not in m:
        return None
    try:
        then = datetime.fromisoformat(m['timestamp'])
        delta = _now_utc() - then
        return delta.total_seconds() / 3600
    except Exception:
        return None


def should_skip(company_dir: Path,
                fresh_hours: float,
                retry_empty_hours: float,
                retry_error_hours: float,
                log: logging.Logger) -> tuple[bool, str]:
    """
    Returns (skip: bool, reason: str).
    Checks .last_scan, .last_empty, .last_error markers against the time thresholds.
    """
    # 1. Successful scan marker
    if fresh_hours > 0:
        h = _hours_since_marker(company_dir / MARKER_SCAN)
        if h is not None and h < fresh_hours:
            return True, f"scanned {h:.1f}h ago (threshold {fresh_hours}h)"

    # 2. Empty result marker
    if retry_empty_hours > 0:
        h = _hours_since_marker(company_dir / MARKER_EMPTY)
        if h is not None and h < retry_empty_hours:
            return True, f"was empty {h:.1f}h ago (threshold {retry_empty_hours}h)"

    # 3. Error marker
    if retry_error_hours > 0:
        h = _hours_since_marker(company_dir / MARKER_ERROR)
        if h is not None and h < retry_error_hours:
            return True, f"had error {h:.1f}h ago (threshold {retry_error_hours}h)"

    return False, ''


# ── Domain helpers ────────────────────────────────────────────────────────────

def get_company_name(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    for prefix in ('www.', 'careers.', 'jobs.', 'corp.', 'about.', 'join.'):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    slug = domain.split('.')[0]
    return COMPANY_NAMES.get(slug, slug.capitalize())


def is_job_match(title: str) -> bool:
    return bool(SENIORITY_RE.search(title) and ROLE_RE.search(title))


def sanitize_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:max_len] or 'job'


def is_allowed_domain(href: str, seed_url: str) -> bool:
    try:
        ph = urlparse(href)
        ps = urlparse(seed_url)
        if ph.scheme not in ('http', 'https'):
            return False
        hd = ph.netloc.lower()
        sd = ps.netloc.lower()
        if hd == sd:
            return True
        root = lambda d: '.'.join(d.split('.')[-2:])
        if root(hd) == root(sd):
            return True
        for ats in ATS_DOMAINS:
            if hd == ats or hd.endswith('.' + ats):
                return True
        return False
    except Exception:
        return False


# ── Page helpers ──────────────────────────────────────────────────────────────

def _get_links(page) -> list[dict]:
    try:
        items = page.eval_on_selector_all(
            'a[href]',
            """els => els.map(e => {
                const link_text = (e.innerText || e.textContent || '').trim();
                // Walk up to find a meaningful job-card container, then grab its full text.
                // This captures the location / metadata displayed next to the title.
                const container = e.closest(
                    'li, tr, [class*="job"], [class*="posting"], ' +
                    '[class*="role"], [class*="position"], [class*="card"], article'
                ) || e.parentElement;
                const ctx = container
                    ? (container.innerText || container.textContent || '').trim()
                    : '';
                return {text: link_text, href: e.href, ctx: ctx.substring(0, 300)};
            })"""
        )
        for item in items:
            item['text'] = _normalize(item.get('text', ''))
            item['ctx']  = _normalize(item.get('ctx',  ''))
        return items
    except Exception:
        return []


def _normalize(text: str) -> str:
    """Collapse all whitespace variants (incl. &nbsp;, \u00a0) to single spaces."""
    return re.sub(r'[\s\u00a0\u200b\u200c\u200d\ufeff]+', ' ', text).strip()


def _get_clickables(page) -> list[dict]:
    try:
        items = page.eval_on_selector_all(
            'a[href], button',
            "els => els.map(e => ({text:(e.innerText||e.textContent||'').trim(), "
            "tag:e.tagName.toLowerCase(), href:e.href||''}))"
        )
        # Normalize whitespace so non-breaking spaces etc. don't break matching
        for item in items:
            item['text'] = _normalize(item.get('text', ''))
        return items
    except Exception:
        return []


def extract_job_links(page, seed_url: str) -> list[dict]:
    seen: set[str] = set()
    results = []
    for item in _get_links(page):
        text = item.get('text', '')
        href = item.get('href', '').strip()
        ctx  = item.get('ctx',  '')
        if not href or href in seen:
            continue
        if is_job_match(text) and is_allowed_domain(href, seed_url):
            seen.add(href)
            # Extract location from the container text (everything except the title itself)
            listing_loc = ctx.replace(text, '').strip() if ctx else ''
            # Only keep it if it looks like an actual location
            if listing_loc and not _LOC_SANITY_RE.search(listing_loc):
                listing_loc = ''
            results.append({'title': text, 'url': href, 'listing_loc': listing_loc})
    return results


def try_click_nav_hint(page, log: logging.Logger) -> bool:
    """
    Find and click the most relevant CTA button/link.
    Scores candidates: longer match = higher score, prefer <a> over <button>.
    Logs all candidates found so failures are diagnosable.
    Returns True if navigation was performed.
    """
    candidates = []

    for c in _get_clickables(page):
        text = c.get('text', '')
        if not text or len(text) > 100:         # ignore mega-blocks
            continue
        m = NAV_HINTS_RE.search(text)
        if not m:
            continue
        score = len(m.group(0)) + (1 if c['tag'] == 'a' else 0)
        candidates.append((score, c))

    if not candidates:
        log.info("  No nav-hint buttons/links found on page.")
        return False

    # Log everything found so the user can see what's there
    candidates.sort(key=lambda x: x[0], reverse=True)
    log.info("  Nav hints found (%d):", len(candidates))
    for score, c in candidates[:8]:
        log.info("    [%s] score=%-3d  '%s'  href=%s",
                 c['tag'], score, c['text'][:60], c.get('href', '')[:60])

    _, best = candidates[0]

    # Strategy 1: navigate directly by href (most reliable — avoids click interception)
    href = best.get('href', '')
    if href and href.startswith('http'):
        log.info("  Navigating to: %s", href)
        try:
            page.goto(href, wait_until="load", timeout=20_000)
            return True
        except Exception as e:
            log.warning("  goto failed (%s), trying click...", e)

    # Strategy 2: scroll into view then click
    try:
        loc = page.get_by_text(best['text'], exact=False).first
        loc.scroll_into_view_if_needed(timeout=3_000)
        if loc.is_visible(timeout=3_000):
            loc.click()
            return True
    except Exception as e:
        log.warning("  Click also failed: %s", e)

    return False


def try_load_more(page, log: logging.Logger) -> bool:
    for c in _get_clickables(page):
        text = c.get('text', '').strip()
        if LOAD_MORE_RE.search(text) and len(text) < 30:
            try:
                loc = page.locator(f"{c['tag']}:has-text('{text[:30]}')").first
                if loc.is_visible(timeout=2_000):
                    loc.click()
                    log.debug("  Load-more: '%s'", text)
                    return True
            except Exception:
                pass
    return False


# ── Job detail extraction ─────────────────────────────────────────────────────

_DESC_SELECTORS = [
    '[class*="job-description"]', '[id*="job-description"]',
    '[class*="jobDescription"]',
    '[data-automation="jobDescription"]',       # Workday
    '[class*="job-detail"]', '[class*="job-content"]',
    '[class*="posting-description"]',            # Lever
    '[class*="job-posting"]',
    '.content-intro', 'main article', 'main section', 'main',
    'article', '[role="main"]',
]

_TITLE_SELECTORS = [
    'h1', '[class*="job-title"]', '[class*="position-title"]',
    '[class*="posting-title"]',
    '[data-automation="jobPostingHeader"]',      # Workday
]


def extract_location(page) -> str:
    """
    Try to extract the job location from the current detail page.
    Returns a location string, or '' if nothing found.
    Strategy:
      1. Check dedicated location elements (specific selectors).
      2. Scan nearby short text blocks (meta tags, page header area).
    """
    # 1. Dedicated location elements
    for sel in _LOCATION_SELECTORS:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                text = _normalize(el.inner_text() or '')
                # Location fields are short; must also look like an actual location
                if text and len(text) < 120 and _LOC_SANITY_RE.search(text):
                    return text
        except Exception:
            pass

    # 2. <meta> tags (some ATS embed location there)
    try:
        metas = page.eval_on_selector_all(
            'meta[name], meta[property]',
            "els => els.map(e => ({name: (e.name||e.getAttribute('property')||'').toLowerCase(), "
            "content: e.content || ''}))"
        )
        for m in metas:
            if 'location' in m.get('name', '') or 'address' in m.get('name', ''):
                val = m.get('content', '').strip()
                if val and len(val) < 120:
                    return val
    except Exception:
        pass

    return ''


def location_passes(location_text: str, location_re: re.Pattern | None) -> tuple[bool, str]:
    """
    Returns (passes: bool, reason: str).
    - If location_re is None (filtering disabled) → always passes.
    - If location_text is empty → passes with 'unknown' (don't reject what we can't determine).
    - Otherwise → passes only if location_re matches.
    """
    if location_re is None:
        return True, 'filter disabled'
    if not location_text:
        return True, 'location unknown — keeping'
    if location_re.search(location_text):
        return True, location_text
    return False, location_text


def extract_job_detail(page, url: str, wait_ms: int, log: logging.Logger) -> dict | None:
    try:
        page.goto(url, wait_until="load", timeout=30_000)
    except Exception as e:
        log.warning("    Failed to load %s: %s", url, e)
        return None
    try:
        page.wait_for_load_state("networkidle", timeout=max(wait_ms, 5_000))
    except Exception:
        pass
    page.wait_for_timeout(wait_ms)

    title = ''
    for sel in _TITLE_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                t = (el.inner_text() or '').strip()
                if t and len(t) < 200:
                    title = t
                    break
        except Exception:
            pass
    if not title:
        title = (page.title() or url).strip()

    location = extract_location(page)

    description = ''
    for sel in _DESC_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                text = (el.inner_text() or '').strip()
                if len(text) > 300:
                    description = text
                    break
        except Exception:
            pass
    if not description:
        try:
            description = (page.inner_text('body') or '').strip()
        except Exception:
            description = ''

    apply_url = url
    for c in _get_clickables(page):
        if re.search(r'\bapply\b', c.get('text', ''), re.IGNORECASE):
            href = c.get('href', '')
            if href and href.startswith('http'):
                apply_url = href
                break

    return {
        'title': title,
        'location': location,
        'description': description,
        'url': url,
        'apply_url': apply_url,
    }


# ── File saving ───────────────────────────────────────────────────────────────

def save_job_file(company_dir: Path, job: dict, log: logging.Logger) -> bool:
    company_dir.mkdir(parents=True, exist_ok=True)
    title = (job.get('title') or 'Unknown_Position').strip()
    base = sanitize_filename(title)
    filepath = company_dir / f"{base}.md"
    if filepath.exists():
        for i in range(1, 200):
            candidate = company_dir / f"{base}_{i}.md"
            if not candidate.exists():
                filepath = candidate
                break

    loc_line = f"**Location:** {job['location']}\n\n" if job.get('location') else ''
    content = (
        f"# {title}\n\n"
        f"**Apply URL:** {job.get('apply_url') or job.get('url', '')}\n\n"
        f"**Job Listing URL:** {job.get('url', '')}\n\n"
        f"{loc_line}"
        f"---\n\n"
        f"{(job.get('description') or '').strip()}\n"
    )
    try:
        filepath.write_text(content, encoding='utf-8')
        log.info("    ✓ Saved: %s", filepath.name)
        return True
    except Exception as e:
        log.warning("    Save error — %s: %s", filepath, e)
        return False


# ── Per-company scraper ───────────────────────────────────────────────────────

def scrape_company(page, seed_url: str, output_dir: Path,
                   delay: float, wait_ms: int, max_jobs: int,
                   location_re: re.Pattern | None,
                   log: logging.Logger,
                   company_name: str | None = None) -> tuple[int, str]:
    """
    Scrape one company. Returns (jobs_saved, outcome) where outcome is
    'saved', 'empty', or 'error'. If company_name is given, use it for the folder; else derive from URL.
    """
    from strategies import api_scrape, detect_strategy

    company = (company_name or get_company_name(seed_url)).strip() or get_company_name(seed_url)
    company_dir = output_dir / company

    log.info("")
    strategy = detect_strategy(seed_url)
    log.info("▶  %-22s  [%s]  %s", company, strategy, seed_url)

    # ── API strategy (Workday / Greenhouse / Lever / Ashby) ───────────────────
    # These don't need a browser — call JSON APIs directly.
    api_jobs = api_scrape(company, seed_url, location_re, is_job_match, log)

    if api_jobs is not None:
        log.info("  API returned %d matching job(s)", len(api_jobs))
        if not api_jobs:
            _write_marker(company_dir / MARKER_EMPTY, {
                'company': company, 'url': seed_url, 'jobs_found': 0, 'strategy': strategy,
            })
            return 0, 'empty'

        saved = 0
        limit = max_jobs if max_jobs else len(api_jobs)
        for job in api_jobs[:limit]:
            log.info("  → %s  [%s]", job['title'][:70], job.get('location', '')[:30])
            if save_job_file(company_dir, job, log):
                saved += 1
            time.sleep(delay * 0.2)   # API jobs need no full page delay

        _write_marker(company_dir / MARKER_SCAN, {
            'company': company, 'url': seed_url, 'jobs_saved': saved, 'strategy': strategy,
        })
        for stale in (MARKER_EMPTY, MARKER_ERROR):
            p = company_dir / stale
            if p.exists():
                try: p.unlink()
                except Exception: pass

        log.info("  Saved %d job(s) → %s/", saved, company_dir)
        return saved, 'saved'

    # ── Playwright strategy (fallback for everything else) ────────────────────
    # Use 'load' (not 'networkidle') so SPA sites with persistent background
    # requests (e.g. Microsoft, meta-tracking scripts) don't cause a timeout.
    # A follow-up wait_for_load_state("networkidle") settles any late AJAX.
    try:
        page.goto(seed_url, wait_until="load", timeout=30_000)
    except Exception as e:
        log.warning("  FAILED to load page: %s", e)
        _write_marker(company_dir / MARKER_ERROR, {
            'company': company, 'url': seed_url, 'error': str(e),
        })
        return 0, 'error'
    try:
        page.wait_for_load_state("networkidle", timeout=max(wait_ms, 5_000))
    except Exception:
        pass  # timeout on networkidle is common for SPAs; proceed with what's loaded
    page.wait_for_timeout(wait_ms)

    # Scan for matching jobs, with nav-hint navigation if needed
    job_links = extract_job_links(page, seed_url)
    log.debug("  Direct job links: %d", len(job_links))

    if not job_links:
        for hop in range(2):
            log.debug("  No jobs yet — trying nav hint (hop %d)...", hop + 1)
            clicked = try_click_nav_hint(page, log)
            if not clicked:
                break
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(wait_ms)
            job_links = extract_job_links(page, seed_url)
            log.debug("  After nav: %d job links", len(job_links))
            if job_links:
                break

    # Expand via "Load more / Next"
    for _ in range(3):
        if not job_links:
            break
        if not try_load_more(page, log):
            break
        page.wait_for_timeout(2_000)
        expanded = extract_job_links(page, seed_url)
        if len(expanded) <= len(job_links):
            break
        job_links = expanded

    log.info("  Matching jobs found: %d", len(job_links))

    if not job_links:
        log.info("  No Senior/Staff/Principal Software Engineer positions found.")
        _write_marker(company_dir / MARKER_EMPTY, {
            'company': company, 'url': seed_url, 'jobs_found': 0,
        })
        return 0, 'empty'

    # Fetch and save each job
    saved = 0
    skipped_loc = 0
    limit = max_jobs if max_jobs else len(job_links)

    for job_ref in job_links[:limit]:
        # ── Early location filter (listing page, no network cost) ──────────────
        listing_loc = job_ref.get('listing_loc', '')
        if listing_loc and location_re:
            passes, reason = location_passes(listing_loc, location_re)
            if not passes:
                log.info("  ✗ [listing] %-55s  loc: %s",
                         job_ref['title'][:55], reason[:50])
                skipped_loc += 1
                continue  # skip the detail page entirely

        log.info("  → %s", job_ref['title'][:80])
        if listing_loc:
            log.debug("    listing loc: %s", listing_loc[:60])
        log.debug("    %s", job_ref['url'])

        detail = extract_job_detail(page, job_ref['url'], wait_ms, log)
        if not detail:
            time.sleep(delay)
            continue

        # Use listing-page title if detail-page title lost the seniority level
        if detail['title'] and not is_job_match(detail['title']) and is_job_match(job_ref['title']):
            detail['title'] = job_ref['title']

        # ── Detail-page location filter (catches what listing page missed) ─────
        passes, loc_reason = location_passes(detail.get('location', ''), location_re)
        if passes:
            if detail.get('location'):
                log.debug("    location: %s  ✓", loc_reason)
            else:
                log.debug("    location: unknown — keeping")
            if save_job_file(company_dir, detail, log):
                saved += 1
        else:
            log.info("    ✗ [detail]  %-55s  loc: %s",
                     detail['title'][:55], loc_reason[:50])
            skipped_loc += 1

        time.sleep(delay)

    if skipped_loc:
        log.info("  Skipped %d job(s) due to location filter", skipped_loc)

    # Write success marker (overwrite any previous error/empty markers)
    _write_marker(company_dir / MARKER_SCAN, {
        'company': company, 'url': seed_url, 'jobs_saved': saved,
    })
    # Remove stale error/empty markers now that we have a good scan
    for stale in (MARKER_EMPTY, MARKER_ERROR):
        p = company_dir / stale
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    log.info("  Saved %d job(s) → %s/", saved, company_dir)
    return saved, 'saved'


# ── Main orchestrator ─────────────────────────────────────────────────────────

def crawl(seed_entries: list[tuple[str | None, str]], output_dir: Path,
          delay: float, wait_ms: int, max_jobs: int,
          fresh_hours: float, retry_empty_hours: float, retry_error_hours: float,
          location_re: re.Pattern | None, location_labels: list[str],
          headless: bool,
          log: logging.Logger,
          progress_file: Path | None = None) -> None:
    """
    seed_entries: list of (company_name_or_none, url). If company_name is None, derive from URL.
    If progress_file is set, write progress lines for the UI to poll (pid, start, company:, company_done:, done).
    """
    def progress(line: str) -> None:
        if progress_file:
            try:
                with open(progress_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
            except Exception:
                pass

    output_dir.mkdir(parents=True, exist_ok=True)
    if progress_file:
        progress_file.write_text("", encoding="utf-8")
        progress(f"pid:{os.getpid()}")
        progress(f"total:{len(seed_entries)}")
        progress("start")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright not installed — run: pip install playwright && playwright install chromium")
        sys.exit(1)

    try:
        from playwright_stealth import stealth_sync
    except ImportError:
        log.warning("playwright-stealth not installed — bot detection will be weaker.")
        log.warning("  Run: pip install playwright-stealth")
        stealth_sync = None

    log.info("═" * 64)
    log.info("Job Crawler starting — %s", _now_utc().strftime('%Y-%m-%d %H:%M UTC'))
    log.info("  Companies          : %d", len(seed_entries))
    log.info("  Output             : %s", output_dir.resolve())
    log.info("  Max jobs/company   : %s", max_jobs or "unlimited")
    log.info("  Skip if scanned <  : %s", f"{fresh_hours}h" if fresh_hours else "disabled")
    log.info("  Retry empty after  : %s", f"{retry_empty_hours}h" if retry_empty_hours else "always retry")
    log.info("  Retry errors after : %s", f"{retry_error_hours}h" if retry_error_hours else "always retry")
    log.info("  Targets            : Senior / Staff / Principal Software Engineers")
    if location_re:
        log.info("  Location filter    : %s", ", ".join(location_labels))
    else:
        log.info("  Location filter    : disabled (--no-location-filter)")
    log.info("═" * 64)

    counts = {'saved': 0, 'empty': 0, 'error': 0, 'skipped': 0, 'total_jobs': 0}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                # Suppress common automation-detection signals
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = context.new_page()
            if stealth_sync:
                # Patches navigator.webdriver, chrome runtime, permissions API,
                # plugins, languages, WebGL vendor, hairline feature, and more.
                stealth_sync(page)
                log.debug("playwright-stealth applied")

            for company_override, url in seed_entries:
                company = (company_override or get_company_name(url)).strip() or get_company_name(url)
                company_dir = output_dir / company

                skip, reason = should_skip(
                    company_dir, fresh_hours, retry_empty_hours, retry_error_hours, log
                )
                if skip:
                    log.info("")
                    log.info("⏭  %-22s  SKIPPED — %s", company, reason)
                    counts['skipped'] += 1
                    progress(f"skip:{company}")
                    continue

                progress(f"company:{company}")
                try:
                    n, outcome = scrape_company(
                        page, url, output_dir, delay, wait_ms, max_jobs, location_re, log,
                        company_name=company,
                    )
                    counts[outcome] += 1
                    counts['total_jobs'] += n
                    progress(f"company_done:{company} {n} {outcome}")
                except Exception as e:
                    log.error("Unhandled error for %s: %s", url, e)
                    _write_marker(company_dir / MARKER_ERROR, {
                        'company': company, 'url': url, 'error': str(e),
                    })
                    counts['error'] += 1
                    progress(f"company_error:{company} {str(e)[:200]}")

            browser.close()
    finally:
        progress("done")

    log.info("")
    log.info("═" * 64)
    log.info("DONE — %s", _now_utc().strftime('%Y-%m-%d %H:%M UTC'))
    log.info("  Skipped (fresh)     : %d", counts['skipped'])
    log.info("  Scanned with jobs   : %d", counts['saved'])
    log.info("  Scanned — no jobs   : %d", counts['empty'])
    log.info("  Errors              : %d", counts['error'])
    log.info("  Total jobs saved    : %d  →  %s", counts['total_jobs'], output_dir.resolve())
    log.info("═" * 64)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scrape Senior/Staff/Principal SWE job listings from company career pages. "
            "Saves Jobs/<Company>/<Title>.md with description + apply URL."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("seed_file",
                        help="Text file with one career URL per line (# = comment)")
    parser.add_argument("--output", "-o", default="Jobs",
                        help="Root output folder")
    parser.add_argument("--max-jobs", type=int, default=0,
                        help="Max jobs to save per company (0 = unlimited)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between individual job-page loads")
    parser.add_argument("--wait", type=int, default=3000,
                        help="Extra ms after page load for JS to settle")

    # ── Freshness / retry controls ────────────────────────────────────────────
    parser.add_argument("--fresh-hours", type=float, default=0,
                        help=(
                            "Skip companies whose .last_scan marker is younger than "
                            "this many hours. 0 = always re-scan."
                        ))
    parser.add_argument("--retry-empty-hours", type=float, default=0,
                        help=(
                            "Skip re-scanning companies that returned zero jobs, "
                            "unless their .last_empty marker is older than this many hours. "
                            "0 = always retry empties."
                        ))
    parser.add_argument("--retry-error-hours", type=float, default=0,
                        help=(
                            "Skip re-scanning companies that had a load error, "
                            "unless their .last_error marker is older than this many hours. "
                            "0 = always retry errors."
                        ))

    # ── Location filter ───────────────────────────────────────────────────────
    parser.add_argument(
        "--locations",
        default=",".join(DEFAULT_LOCATIONS),
        help=(
            "Comma-separated list of location keywords to accept. "
            "Jobs whose location doesn't match any keyword are skipped. "
            "Jobs with no detectable location are always kept. "
            f"Default: \"{','.join(DEFAULT_LOCATIONS)}\""
        ),
    )
    parser.add_argument(
        "--no-location-filter",
        action="store_true",
        help="Disable location filtering — save all jobs regardless of location.",
    )

    parser.add_argument(
        "--no-headless",
        action="store_true",
        help=(
            "Run browser in visible (non-headless) mode. "
            "Useful for debugging — you can watch exactly what the crawler sees."
        ),
    )
    parser.add_argument(
        "--progress-file",
        metavar="PATH",
        default=None,
        help="Write progress lines to this file for UI polling (pid, start, company:, company_done:, done).",
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show debug output")

    args = parser.parse_args()
    log = setup_logging(args.verbose)

    # Build location regex (or None if filtering is disabled)
    if args.no_location_filter:
        location_re = None
        location_labels: list[str] = []
    else:
        location_labels = [l.strip() for l in args.locations.split(',') if l.strip()]
        location_re = build_location_re(location_labels)

    seed_file = Path(args.seed_file)
    if not seed_file.is_file():
        log.error("Seed file not found: %s", seed_file)
        sys.exit(1)

    # Format: "CompanyName, URL" or just "URL". Comments and empty lines skipped.
    seed_entries: list[tuple[str | None, str]] = []
    for line in seed_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            company_part, url_part = line.split(",", 1)
            company_part = company_part.strip()
            url_part = url_part.strip()
            if url_part:
                seed_entries.append((company_part or None, url_part))
        else:
            if line:
                seed_entries.append((None, line))
    if not seed_entries:
        log.error("No URLs found in seed file")
        sys.exit(1)

    log.info("Loaded %d seed URL(s) from %s", len(seed_entries), seed_file)

    progress_path = Path(args.progress_file) if args.progress_file else None

    crawl(
        seed_entries=seed_entries,
        output_dir=Path(args.output),
        delay=args.delay,
        wait_ms=args.wait,
        max_jobs=args.max_jobs,
        fresh_hours=args.fresh_hours,
        retry_empty_hours=args.retry_empty_hours,
        retry_error_hours=args.retry_error_hours,
        location_re=location_re,
        location_labels=location_labels,
        headless=not args.no_headless,
        log=log,
        progress_file=progress_path,
    )


if __name__ == "__main__":
    main()
