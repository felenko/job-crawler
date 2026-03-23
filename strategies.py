"""
API-based job scrapers for common ATS platforms.

Instead of loading a browser, these call the platform's JSON API directly —
faster, more reliable, and invisible to bot-detection.

Supported platforms:
  - Workday  (*.myworkdayjobs.com  or custom domains using Workday)
  - Greenhouse (boards.greenhouse.io/*)
  - Lever (jobs.lever.co/*)
  - Ashby (jobs.ashbyhq.com/*)

Usage from job_crawler.py:
    from strategies import detect_strategy, api_scrape

    result = api_scrape(company, url, location_re, is_job_match, log)
    if result is not None:
        # result is list[dict] with keys: title, url, location, description, apply_url
        # use it directly, skip Playwright entirely
    else:
        # fall through to Playwright scraper
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin, quote

import requests

# Use the OS/Windows certificate store instead of certifi's bundle.
# certifi lacks intermediate CAs (e.g. Amazon RSA 2048 M04) that servers
# sometimes omit from their TLS chain; the system store handles AIA fetching.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # pip install truststore to fix SSL errors on Windows

# ── Shared HTTP session ───────────────────────────────────────────────────────

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
})
# Bypass any local SSL inspection proxy (e.g. Fiddler/Charles at 127.0.0.1:8888).
# Empty string (not None) explicitly disables proxy routing for the session.
_SESSION.proxies = {"http": "", "https": ""}


# ── Strategy detection ────────────────────────────────────────────────────────

def detect_strategy(url: str) -> str:
    """
    Return the strategy name for a given URL, or 'playwright' as fallback.
    Strategies: 'workday', 'greenhouse', 'lever', 'ashby', 'microsoft',
                'smartrecruiters', 'salesforce', 'meta', 'doordash', 'google',
                'block_xyz', 'amazon', 'janestreet', 'netflix',
                'twosigma', 'hrt', 'playwright'
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if 'myworkdayjobs.com' in host:
        return 'workday'

    # Custom Workday domains: path pattern /us/en/search-results or /wday/
    if '/wday/' in path or re.search(r'/[a-z]{2}/[a-z]{2}/search-results', path):
        return 'workday_custom'

    if 'greenhouse.io' in host:
        return 'greenhouse'

    if 'lever.co' in host:
        return 'lever'

    if 'ashbyhq.com' in host:
        return 'ashby'

    if 'careers.microsoft.com' in host:
        return 'microsoft'

    if 'smartrecruiters.com' in host or host in _SR_CUSTOM_DOMAINS:
        return 'smartrecruiters'

    if 'careers.salesforce.com' in host:
        return 'salesforce'

    if 'metacareers.com' in host:
        return 'meta'

    if 'careersatdoordash.com' in host:
        return 'doordash'

    if 'careers.google.com' in host or ('google.com' in host and '/careers/' in path):
        return 'google'

    if host == 'block.xyz' and '/careers' in path:
        return 'block_xyz'

    if 'amazon.jobs' in host:
        return 'amazon'

    if 'janestreet.com' in host and '/jobs/' in path:
        return 'janestreet'

    if 'jobs.netflix.net' in host or 'explore.jobs.netflix.net' in host:
        return 'netflix'

    if 'careers.twosigma.com' in host:
        return 'twosigma'

    if 'hudsonrivertrading.com' in host and '/careers' in path:
        return 'hrt'

    return 'playwright'


# ── Workday ───────────────────────────────────────────────────────────────────
# URL pattern:  https://{tenant}.wd5.myworkdayjobs.com/{SiteName}
# API:          POST https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{SiteName}/jobs
# Payload:      {"appliedFacets":{}, "limit":20, "offset":0, "searchText":"software engineer"}
# Response:     {"jobPostings":[{"title","externalPath","locationsText","bulletFields"}], "total":N}

def _workday_tenant_site(url: str) -> tuple[str, str, str] | None:
    """
    Extract (base_url, tenant, site) from a Workday URL.
    e.g. https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
    → ('https://nvidia.wd5.myworkdayjobs.com', 'nvidia', 'NVIDIAExternalCareerSite')
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if 'myworkdayjobs.com' not in host:
        return None

    tenant = host.split('.')[0]
    # Site name is the first path segment
    path_parts = [p for p in parsed.path.strip('/').split('/') if p]
    if not path_parts:
        return None
    site = path_parts[0]
    base = f"{parsed.scheme}://{parsed.netloc}"
    return base, tenant, site


def _workday_jobs(base: str, tenant: str, site: str,
                  search_text: str, location_re, is_job_match,
                  log: logging.Logger) -> list[dict]:
    api_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    log.debug("  [Workday API] %s", api_url)

    results = []
    offset = 0
    limit = 20
    max_pages = 20  # safety cap

    for _ in range(max_pages):
        payload = {
            "appliedFacets": {},
            "limit": limit,
            "offset": offset,
            "searchText": search_text,
        }
        try:
            r = _SESSION.post(api_url, json=payload, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("  [Workday API] request failed: %s", e)
            break

        postings = data.get('jobPostings', [])
        if not postings:
            break

        for p in postings:
            title = (p.get('title') or '').strip()
            if not is_job_match(title):
                continue

            loc = (p.get('locationsText') or '').strip()
            ext_path = p.get('externalPath', '')
            job_url = urljoin(base, ext_path) if ext_path else base

            # Early location filter
            if location_re and loc and not location_re.search(loc):
                log.debug("  [Workday] ✗ loc mismatch: %s — %s", title[:50], loc[:50])
                continue

            results.append({
                'title': title,
                'url': job_url,
                'apply_url': job_url,
                'location': loc,
                'description': '',   # fetched separately if needed
            })

        log.debug("  [Workday] page offset=%d  got=%d  matching so far=%d",
                  offset, len(postings), len(results))

        if len(postings) < limit:
            break
        offset += limit
        time.sleep(0.3)

    return results


def scrape_workday(url: str, location_re, is_job_match,
                   log: logging.Logger) -> list[dict] | None:
    parts = _workday_tenant_site(url)
    if not parts:
        log.warning("  [Workday] could not parse tenant/site from %s", url)
        return None
    base, tenant, site = parts
    log.info("  [Workday API] tenant=%s  site=%s", tenant, site)
    return _workday_jobs(base, tenant, site,
                         search_text="software engineer",
                         location_re=location_re,
                         is_job_match=is_job_match,
                         log=log)


# ── API matching helper ───────────────────────────────────────────────────────
# Companies on Greenhouse/Lever/Ashby boards often omit seniority from titles
# (e.g. Figma, JumpTrading, Jane Street). Accept any matching role type from
# these company-specific boards; seniority filter is still applied when both
# seniority AND role are present via is_job_match.
_API_ROLE_RE = re.compile(
    r'\bEngineer\b|\bDeveloper\b|\bArchitect\b|\bSWE\b|\bSDE\b',
    re.IGNORECASE,
)
_API_EXCLUDE_RE = re.compile(
    r'\b(Intern|Co-?op|Apprentice|Manager|Director|VP |Vice\s+President|'
    r'Recruiter|Coordinator|Analyst(?!\s*Engineer)|Sales|Marketing|Legal|'
    r'Finance|Accountant|Office\s*Manager)\b',
    re.IGNORECASE,
)

def _api_job_match(title: str, is_job_match) -> bool:
    """Accept a job title from a company-specific API board.
    Prefers full is_job_match (seniority + role), but falls back to role-only
    for companies that don't include seniority in titles."""
    if is_job_match(title):
        return True
    # Fallback: role match without seniority, excluding clearly non-engineering roles
    return bool(_API_ROLE_RE.search(title)) and not bool(_API_EXCLUDE_RE.search(title))


# ── Greenhouse ────────────────────────────────────────────────────────────────
# URL pattern:  https://boards.greenhouse.io/{slug}
# API:          GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
# Response:     {"jobs":[{"title","location":{"name"},"absolute_url","content"}], "meta":{...}}

def _greenhouse_slug(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip('/').split('/') if p]
    return parts[0] if parts else None


def scrape_greenhouse(url: str, location_re, is_job_match,
                      log: logging.Logger) -> list[dict] | None:
    slug = _greenhouse_slug(url)
    if not slug:
        return None

    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    log.info("  [Greenhouse API] %s", api_url)

    try:
        r = _SESSION.get(api_url, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("  [Greenhouse API] failed: %s", e)
        return None

    results = []
    for job in data.get('jobs', []):
        title = (job.get('title') or '').strip()
        if not _api_job_match(title, is_job_match):
            continue

        loc = (job.get('location', {}).get('name') or '').strip()
        job_url = job.get('absolute_url', url)

        if location_re and loc and not location_re.search(loc):
            log.debug("  [Greenhouse] ✗ loc: %s — %s", title[:50], loc)
            continue

        description = _strip_html(job.get('content') or '')
        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': loc,
            'description': description,
        })

    return results


# ── Lever ─────────────────────────────────────────────────────────────────────
# URL pattern:  https://jobs.lever.co/{slug}
# API:          GET https://api.lever.co/v0/postings/{slug}?mode=json
# Response:     [ {"text","categories":{"location","team"},"hostedUrl","descriptionPlain"} ]

def _lever_slug(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip('/').split('/') if p]
    return parts[0] if parts else None


def scrape_lever(url: str, location_re, is_job_match,
                 log: logging.Logger) -> list[dict] | None:
    slug = _lever_slug(url)
    if not slug:
        return None

    api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    log.info("  [Lever API] %s", api_url)

    try:
        r = _SESSION.get(api_url, timeout=20)
        r.raise_for_status()
        jobs = r.json()
    except Exception as e:
        log.warning("  [Lever API] failed: %s", e)
        return None

    results = []
    for job in jobs:
        title = (job.get('text') or '').strip()
        if not _api_job_match(title, is_job_match):
            continue

        cats = job.get('categories', {})
        loc = (cats.get('location') or cats.get('allLocations') or '').strip()
        job_url = job.get('hostedUrl', url)

        if location_re and loc and not location_re.search(loc):
            log.debug("  [Lever] ✗ loc: %s — %s", title[:50], loc)
            continue

        description = (job.get('descriptionPlain') or
                       _strip_html(job.get('description') or ''))
        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': loc,
            'description': description,
        })

    return results


# ── Ashby ─────────────────────────────────────────────────────────────────────
# URL pattern:  https://jobs.ashbyhq.com/{slug}
# API:          GET https://api.ashbyhq.com/posting-api/job-board/{slug}
# Response:     {"jobPostings":[{"id","title","locationName","isRemote","externalLink",...}]}

def _ashby_slug(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip('/').split('/') if p]
    return parts[0] if parts else None


def scrape_ashby(url: str, location_re, is_job_match,
                 log: logging.Logger) -> list[dict] | None:
    slug = _ashby_slug(url)
    if not slug:
        return None

    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    log.info("  [Ashby API] slug=%s", slug)

    try:
        r = _SESSION.get(api_url, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("  [Ashby API] failed: %s", e)
        return None

    # REST API returns jobPostings array
    postings = data.get('jobPostings', data.get('jobs', []))

    results = []
    for p in postings:
        title = (p.get('title') or '').strip()
        if not _api_job_match(title, is_job_match):
            continue

        loc = (p.get('locationName') or p.get('location') or '').strip()
        if p.get('isRemote') and not loc:
            loc = 'Remote'
        job_url = p.get('externalLink') or p.get('jobUrl') or f"https://jobs.ashbyhq.com/{slug}/{p.get('id','')}"

        if location_re and loc and not location_re.search(loc):
            log.debug("  [Ashby] ✗ loc: %s — %s", title[:50], loc)
            continue

        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': loc,
            'description': '',
        })

    return results


# ── Shared location helpers ───────────────────────────────────────────────────

# Broad country-level terms that should only pass when they appear standalone
# (no comma = no state/city qualifier after them).
_BROAD_COUNTRY_RE = re.compile(r'^(US|USA|United\s+States|America)$', re.IGNORECASE)


def _loc_passes_strict(loc_str: str, location_re) -> bool:
    """Return True if loc_str satisfies location_re with strict country-term handling.

    Problem: location_re includes "United States" to catch US-wide / remote jobs,
    but hierarchical strings like "Santa Clara, California, United States" also
    contain that substring and should be rejected.

    Rule: if the only matching term is a broad country word (US/USA/United States/
    America) AND the string contains a comma (indicating a state/city is also
    present), reject it — the job is in a specific non-target location.
    """
    if not location_re:
        return True
    m = location_re.search(loc_str)
    if not m:
        return False
    if _BROAD_COUNTRY_RE.match(m.group(0)) and ',' in loc_str:
        return False
    return True


# ── SmartRecruiters ────────────────────────────────────────────────────────────
# Search: GET https://api.smartrecruiters.com/v1/companies/{slug}/postings
#   params: keyword, location, limit (max 100), offset
#   response: { totalFound, offset, limit,
#               content: [{ id, name,
#                           location: { city, country, remote, hybrid, fullLocation },
#                           ref }] }
# Detail: GET https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}
#   response: { postingUrl, applyUrl,
#               jobAd: { sections: { jobDescription: { text } } } }

_SR_BASE = 'https://api.smartrecruiters.com'

# Custom career domains that are powered by SmartRecruiters.
# Maps hostname → company slug used in the SR API.
_SR_CUSTOM_DOMAINS: dict[str, str] = {
    'careers.servicenow.com': 'ServiceNow',
}


def _sr_slug(url: str) -> str | None:
    """Extract SmartRecruiters company slug from a seed URL."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in _SR_CUSTOM_DOMAINS:
        return _SR_CUSTOM_DOMAINS[host]
    if 'smartrecruiters.com' in host:
        # https://jobs.smartrecruiters.com/CompanySlug/...
        parts = parsed.path.strip('/').split('/')
        return parts[0] if parts and parts[0] else None
    return None


def scrape_smartrecruiters(url: str, location_re, is_job_match,
                            log: logging.Logger) -> list[dict] | None:
    slug = _sr_slug(url)
    if not slug:
        return None

    search_url = f'{_SR_BASE}/v1/companies/{slug}/postings'
    log.info('  [SmartRecruiters] slug=%s  %s', slug, search_url)

    results: list[dict] = []
    seen_ids: set = set()
    page_size = 100

    # Two targeted API queries: NY-area jobs and Remote jobs.
    for loc_query in ('New York', 'Remote'):
        offset = 0
        log.debug('  [SR] querying location=%r', loc_query)
        while True:
            params: dict = {'keyword': 'software engineer', 'limit': page_size, 'offset': offset}
            if loc_query:
                params['location'] = loc_query
            try:
                r = _SESSION.get(search_url, params=params, timeout=20)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.warning('  [SR] search failed loc=%r offset=%d: %s', loc_query, offset, e)
                break

            jobs = data.get('content') or []
            total = data.get('totalFound') or 0
            if not jobs:
                break

            for job in jobs:
                job_id = job.get('id')
                if not job_id or job_id in seen_ids:
                    continue

                title = (job.get('name') or '').strip()
                if not is_job_match(title):
                    continue

                loc_obj = job.get('location') or {}
                is_remote = bool(loc_obj.get('remote'))
                full_loc = (loc_obj.get('fullLocation') or loc_obj.get('city') or '').strip()
                loc_str = 'Remote' if is_remote else full_loc

                # Remote jobs always pass; others use strict location check
                if not is_remote and location_re:
                    if full_loc and not _loc_passes_strict(full_loc, location_re):
                        log.debug('  [SR] ✗ loc: %s — %s', title[:50], full_loc)
                        continue

                seen_ids.add(job_id)

                # Fetch description + apply URL from detail endpoint
                posting_url = ''
                apply_url = ''
                description = ''
                try:
                    dr = _SESSION.get(f'{search_url}/{job_id}', timeout=20)
                    dr.raise_for_status()
                    detail = dr.json()
                    posting_url = (detail.get('postingUrl') or '').strip()
                    apply_url = (detail.get('applyUrl') or posting_url).strip()
                    desc_html = (detail.get('jobAd') or {}).get('sections', {}) \
                                      .get('jobDescription', {}).get('text') or ''
                    description = _strip_html(desc_html)
                except Exception as e:
                    log.debug('  [SR] detail failed for %s: %s', job_id, e)
                time.sleep(0.15)

                results.append({
                    'title': title,
                    'url': posting_url or f'https://jobs.smartrecruiters.com/{slug}/{job_id}',
                    'apply_url': apply_url or posting_url or f'https://jobs.smartrecruiters.com/{slug}/{job_id}',
                    'location': loc_str,
                    'description': description,
                })

            log.debug('  [SR] loc=%r offset=%d got=%d matching=%d total=%d',
                      loc_query, offset, len(jobs), len(results), total)
            offset += page_size
            if offset >= total or len(jobs) < page_size:
                break
            time.sleep(0.2)

    return results


# ── Microsoft ─────────────────────────────────────────────────────────────────
# Search: GET https://apply.careers.microsoft.com/api/pcsx/search
#   params: domain, query, location, start (0-based offset, page size = 10)
#   response: { data: { count, positions: [{id, name, locations[], positionUrl}] } }
# Detail: GET https://apply.careers.microsoft.com/api/pcsx/position_details
#   params: position_id, domain, hl
#   response: { data: { jobDescription (HTML), ... } }
# Job URL: https://apply.careers.microsoft.com + positionUrl

_MS_BASE = 'https://apply.careers.microsoft.com'
_MS_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Referer': 'https://apply.careers.microsoft.com/',
}

def _ms_loc_ok(raw_locs: list, location_re) -> tuple[bool, str]:
    """Microsoft-specific location check over a list of raw location strings.

    Uses _loc_passes_strict so "United States, Washington, Redmond" is rejected
    while "United States, New York, New York City" or standalone "Remote" pass.
    """
    if not location_re:
        return True, raw_locs[0] if raw_locs else ''
    if not raw_locs:
        return True, ''  # location unknown — keep
    for loc in raw_locs:
        if _loc_passes_strict(loc, location_re):
            return True, loc
    return False, raw_locs[0] if raw_locs else ''


def scrape_microsoft(url: str, location_re, is_job_match,
                     log: logging.Logger) -> list[dict] | None:
    search_url = f'{_MS_BASE}/api/pcsx/search'
    log.info('  [Microsoft API] %s', search_url)

    results = []
    seen_ids: set = set()
    page_size = 10
    # Two targeted API queries: NY-area jobs and Remote jobs.
    # This pre-filters at the API level so we don't wade through pages of
    # Redmond/WA results before hitting the 403 rate-limit at page 30.
    location_queries = ['New York', 'Remote']

    for loc_query in location_queries:
        start = 0
        log.debug('  [Microsoft] querying location=%r', loc_query)
        while True:
            params = {
                'domain': 'microsoft.com',
                'query': 'senior software engineer OR staff software engineer OR principal software engineer',
                'location': loc_query,
                'start': start,
            }
            try:
                r = _SESSION.get(search_url, params=params,
                                 headers=_MS_HEADERS, timeout=20)
                if r.status_code == 403:
                    log.warning('  [Microsoft API] 403 at start=%d loc=%r — stopping', start, loc_query)
                    break
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.warning('  [Microsoft API] search failed: %s', e)
                break

            positions = data.get('data', {}).get('positions', [])
            total = data.get('data', {}).get('count', 0)
            if not positions:
                break

            for pos in positions:
                pos_id = pos.get('id')
                if pos_id in seen_ids:
                    continue

                title = (pos.get('name') or '').strip()
                if not is_job_match(title):
                    continue

                job_url = _MS_BASE + (pos.get('positionUrl') or f'/careers/job/{pos_id}')

                # Smarter location check: reject "United States, Washington, Redmond" style strings
                raw_locs = pos.get('locations') or []
                location_ok, matched_loc = _ms_loc_ok(raw_locs, location_re)

                if not location_ok:
                    log.debug('  [Microsoft] ✗ loc: %s — %s', title[:50], raw_locs)
                    continue

                seen_ids.add(pos_id)

                # Fetch description from detail endpoint
                description = ''
                if pos_id:
                    try:
                        dr = _SESSION.get(
                            f'{_MS_BASE}/api/pcsx/position_details',
                            params={'position_id': pos_id, 'domain': 'microsoft.com', 'hl': 'en'},
                            headers=_MS_HEADERS, timeout=20,
                        )
                        dr.raise_for_status()
                        description = _strip_html(
                            dr.json().get('data', {}).get('jobDescription') or ''
                        )
                    except Exception as e:
                        log.debug('  [Microsoft] detail fetch failed for %s: %s', pos_id, e)
                    time.sleep(0.2)

                results.append({
                    'title': title,
                    'url': job_url,
                    'apply_url': job_url,
                    'location': matched_loc or (raw_locs[0] if raw_locs else ''),
                    'description': description,
                })

            log.debug('  [Microsoft] loc=%r start=%d  got=%d  matching so far=%d  total=%d',
                      loc_query, start, len(positions), len(results), total)

            start += page_size
            if start >= total or len(positions) < page_size:
                break
            time.sleep(0.3)

    return results


# ── Salesforce ────────────────────────────────────────────────────────────────
# XML feed: GET https://careers.salesforce.com/en/jobs/xml/
#   params: search, location, page (1-based)
#   response: XML <source> with <job> elements:
#     title, url, apijobid, city, state, country, remotetype, description (HTML)

_SF_FEED = 'https://careers.salesforce.com/en/jobs/xml/'


def scrape_salesforce(url: str, location_re, is_job_match,
                      log: logging.Logger) -> list[dict] | None:
    # The feed always returns the full dataset in a single response — pagination
    # params and location params are silently ignored by the endpoint.
    log.info('  [Salesforce] %s', _SF_FEED)

    def _text(el, tag: str) -> str:
        child = el.find(tag)
        return (child.text or '').strip() if child is not None else ''

    try:
        r = _SESSION.get(_SF_FEED, params={'search': 'software engineer'}, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning('  [SF] feed failed: %s', e)
        return None

    all_jobs = root.findall('job')
    log.debug('  [SF] total jobs in feed: %d', len(all_jobs))

    results: list[dict] = []
    seen_ids: set = set()

    for job in all_jobs:
        job_id = _text(job, 'apijobid') or _text(job, 'requisitionid')
        if not job_id or job_id in seen_ids:
            continue

        title = _text(job, 'title')
        if not is_job_match(title):
            continue

        city       = _text(job, 'city')
        state      = _text(job, 'state')
        remotetype = _text(job, 'remotetype').lower()
        is_remote  = remotetype == 'remote'

        if is_remote:
            loc_str = 'Remote'
        elif city and state:
            loc_str = f'{city}, {state}'
        else:
            loc_str = city or _text(job, 'country')

        if not is_remote and location_re:
            if loc_str and not _loc_passes_strict(loc_str, location_re):
                log.debug('  [SF] ✗ loc: %s — %s', title[:50], loc_str)
                continue

        seen_ids.add(job_id)
        job_url = _text(job, 'url') or f'https://careers.salesforce.com/en/jobs/{job_id}/'
        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': loc_str,
            'description': _strip_html(_text(job, 'description')),
        })

    log.debug('  [SF] matching jobs: %d', len(results))
    return results


# ── Meta ──────────────────────────────────────────────────────────────────────
# GraphQL API: POST https://www.metacareers.com/graphql
#   doc_id: CareersJobSearchResultsDataQuery_candidate_portalRelayOperation
#   variables: { search_input: { q, page, offices, roles, leadership_levels, ... } }
#   The endpoint returns all matching jobs in one shot (pagination params ignored).
#
# CSRF: the page embeds an LSD token that must be echoed in the POST body and
#   X-FB-LSD header.  We fetch the careers page once to extract it.

_META_GRAPHQL = 'https://www.metacareers.com/graphql'
_META_SEARCH_DOC_ID = '29615178951461218'   # CareersJobSearchResultsDataQuery


def scrape_meta(url: str, location_re, is_job_match,
                log: logging.Logger) -> list[dict] | None:
    import requests as _requests
    log.info('  [Meta] %s', _META_GRAPHQL)

    # Use a fresh session with browser-like headers.
    # /jobs/?q=... requires a logged-in session; /jobsearch is publicly accessible.
    meta_session = _requests.Session()
    meta_session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Upgrade-Insecure-Requests': '1',
    })
    try:
        page_r = meta_session.get('https://www.metacareers.com/jobsearch', timeout=20)
        page_r.raise_for_status()
    except Exception as e:
        log.warning('  [Meta] failed to load jobsearch page: %s', e)
        return None

    lsd_match = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', page_r.text)
    if not lsd_match:
        log.warning('  [Meta] LSD token not found in page')
        return None
    lsd_token = lsd_match.group(1)

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://www.metacareers.com',
        'Referer': 'https://www.metacareers.com/jobsearch',
        'X-FB-LSD': lsd_token,
    }

    variables = {
        'search_input': {
            'q': 'software engineer',
            'page': 1,
            'offices': [],
            'roles': ['Full time employment'],
            'leadership_levels': [],
            'teams': ['Software Engineering', 'Infrastructure'],
            'sub_teams': [],
            'is_leadership': False,
        }
    }

    try:
        import json as _json
        r = meta_session.post(_META_GRAPHQL, headers=headers, data={
            'variables': _json.dumps(variables),
            'doc_id': _META_SEARCH_DOC_ID,
            'lsd': lsd_token,
        }, timeout=30)
        r.raise_for_status()
        body = r.text
        if body.startswith('for (;;);'):
            body = body[9:]
        data = _json.loads(body)
    except Exception as e:
        log.warning('  [Meta] API call failed: %s', e)
        return None

    errors = data.get('errors', [])
    if errors and not data.get('data'):
        log.warning('  [Meta] API errors: %s', errors[0].get('message', ''))
        return None

    jswf = (data.get('data') or {}).get('job_search_with_featured_jobs') or {}
    all_jobs = jswf.get('all_jobs', []) + jswf.get('featured_jobs', [])
    log.debug('  [Meta] total jobs from API: %d', len(all_jobs))

    # Meta doesn't include seniority ("Senior", "Staff") in job titles —
    # they use "Software Engineer, [Specialty]" for all levels.
    # Filter by role keywords only; skip the seniority half of is_job_match.
    _meta_role_re = re.compile(
        r'\b(Software\s*Engineer|Software\s*Developer|SWE|'
        r'Backend\s*Engineer|Frontend\s*Engineer|Full[\s\-]?Stack|'
        r'Platform\s*Engineer|Infrastructure\s*Engineer|'
        r'Site\s*Reliability\s*Engineer|SRE|'
        r'Machine\s*Learning\s*Engineer|ML\s*Engineer|'
        r'Systems?\s*Engineer|Security\s*Engineer|Data\s*Engineer|'
        r'Application\s*Engineer|Cloud\s*Engineer|DevOps\s*Engineer)\b',
        re.IGNORECASE,
    )

    results: list[dict] = []
    seen_ids: set = set()

    for job in all_jobs:
        job_id = job.get('id', '')
        if not job_id or job_id in seen_ids:
            continue

        title = (job.get('title') or '').strip()
        if not _meta_role_re.search(title):
            continue

        locations = job.get('locations') or []
        loc_str = ', '.join(locations) if locations else ''

        if location_re and loc_str:
            if not _loc_passes_strict(loc_str, location_re):
                log.debug('  [Meta] ✗ loc: %s — %s', title[:50], loc_str)
                continue

        seen_ids.add(job_id)
        job_url = f'https://www.metacareers.com/v2/jobs/{job_id}/'
        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': loc_str,
            'description': '',
        })

    log.debug('  [Meta] matching jobs: %d', len(results))
    return results


# ── Google ────────────────────────────────────────────────────────────────────
# URL pattern:  https://www.google.com/about/careers/applications/jobs/results/?q=...&location=...
# Strategy:     Paginate ?page=N; job data embedded in AF_initDataCallback(key:'ds:1') JSON blob
# Structure:    data[0] = list of jobs; data[2] = total count; data[3] = page size
#               job[0]=id, job[1]=title, job[2]=apply_url, job[9]=[[loc_str, ...], ...]

_GOOGLE_DS1_RE = re.compile(
    r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?)\}\);",
    re.DOTALL,
)


def _google_extract_jobs(html: str) -> tuple[list, int] | None:
    """Return (jobs_list, total) from embedded AF_initDataCallback data."""
    m = _GOOGLE_DS1_RE.search(html)
    if not m:
        return None
    raw_start = m.start(1)
    depth = 0
    for i, ch in enumerate(html[raw_start:], raw_start):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                raw = html[raw_start:i + 1]
                break
    else:
        return None
    try:
        import json
        d = json.loads(raw)
    except Exception:
        return None
    jobs = d[0] if d and isinstance(d[0], list) else []
    total = d[2] if len(d) > 2 and isinstance(d[2], int) else 0
    return jobs, total


def scrape_google(url: str, location_re, is_job_match,
                  log: logging.Logger) -> list[dict] | None:
    parsed = urlparse(url)
    qs = dict(pair.split('=', 1) for pair in parsed.query.split('&') if '=' in pair)
    q = qs.get('q', 'software engineer').replace('+', ' ').replace('%20', ' ')
    location = qs.get('location', '').replace('+', ' ').replace('%20', ' ')

    base = 'https://www.google.com/about/careers/applications/jobs/results/'
    log.info('  [Google] q=%r location=%r', q, location)

    results = []
    page = 1

    while True:
        params = {'q': q, 'page': page}
        if location:
            params['location'] = location
        page_url = base + '?' + '&'.join(f'{k}={str(v).replace(" ", "+")}' for k, v in params.items())

        html = _curl_get(page_url, log)
        if not html:
            break

        extracted = _google_extract_jobs(html)
        if not extracted:
            log.warning('  [Google] failed to extract jobs from page %d', page)
            break

        jobs, total = extracted
        if not jobs:
            break

        log.debug('  [Google] page %d: %d jobs (total=%d)', page, len(jobs), total)

        for job in jobs:
            title = (job[1] or '').strip() if len(job) > 1 else ''
            apply_url = (job[2] or '').strip() if len(job) > 2 else ''
            locs_raw = job[9] if len(job) > 9 and job[9] else []

            if not is_job_match(title):
                continue

            # job[9] is list of location tuples; first element of each is the display string
            loc_strings = [loc[0] for loc in locs_raw if loc and loc[0]]
            loc_display = '; '.join(loc_strings)

            if location_re and loc_strings:
                if not any(location_re.search(ls) for ls in loc_strings):
                    log.debug('  [Google] ✗ loc: %s — %s', title[:50], loc_display)
                    continue

            results.append({
                'title': title,
                'url': apply_url,
                'apply_url': apply_url,
                'location': loc_display,
                'description': '',
            })

        if len(jobs) < 20 or len(results) >= total:
            break
        page += 1

    return results


# ── DoorDash ──────────────────────────────────────────────────────────────────
# URL pattern:  https://careersatdoordash.com/job-search/
# Strategy:     Paginate server-rendered HTML at ?spage=N, parse job-item divs
# Each page:    25 jobs; stop when a page returns 0 job-items

_DD_BASE = 'https://careersatdoordash.com/job-search/'
_DD_JOB_ITEM_RE  = re.compile(
    r'class="job-item[^"]*".*?'
    r'href="(https://careersatdoordash\.com/jobs/[^"]+)"[^>]*>([^<]+)</a>.*?'
    r'value-secondary">([^<]+)</div>',
    re.DOTALL,
)


def _curl_get(page_url: str, log: logging.Logger) -> str | None:
    """Fetch a URL using curl subprocess (bypasses TLS fingerprint blocking)."""
    import subprocess
    try:
        result = subprocess.run(
            ['curl', '-s', '-A',
             'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
             '--max-time', '20', page_url],
            capture_output=True, timeout=25,
        )
        return result.stdout.decode('utf-8', errors='replace')
    except Exception as e:
        log.warning('  [curl] failed for %s: %s', page_url, e)
        return None


def scrape_doordash(url: str, location_re, is_job_match,
                    log: logging.Logger) -> list[dict] | None:
    log.info('  [DoorDash] scraping %s', _DD_BASE)
    results = []

    page = 1
    while True:
        page_url = f'{_DD_BASE}?spage={page}'
        html = _curl_get(page_url, log)
        if html is None:
            break

        items = _DD_JOB_ITEM_RE.findall(html)
        if not items:
            break

        for job_url, title, location in items:
            title = title.strip()
            location = location.strip()

            if not is_job_match(title):
                continue
            if location_re and not location_re.search(location):
                log.debug('  [DoorDash] ✗ loc: %s — %s', title[:50], location)
                continue

            results.append({
                'title': title,
                'url': job_url,
                'apply_url': job_url,
                'location': location,
                'description': '',
            })

        log.debug('  [DoorDash] page %d: %d items, %d matching so far', page, len(items), len(results))
        page += 1

    return results


# ── Block.xyz ─────────────────────────────────────────────────────────────────
# URL pattern:  https://block.xyz/careers/jobs
# Strategy:     SvelteKit __data.json endpoint; dehydrated flat-array format
# Each page:    36-50 jobs; paginate until empty

def scrape_block_xyz(url: str, location_re, is_job_match,
                     log: logging.Logger) -> list[dict] | None:
    import json as _json

    log.info('  [Block.xyz] scraping %s', url)

    # The __data.json endpoint returns one page of jobs in SvelteKit's dehydrated
    # flat-array format.  The page= param is currently ignored server-side, so we
    # make a single request and parse what we get.
    raw = _curl_get('https://block.xyz/careers/jobs/__data.json', log)
    if not raw:
        return None

    try:
        outer = _json.loads(raw)
    except Exception as e:
        log.warning('  [Block.xyz] JSON parse failed: %s', e)
        return None

    # SvelteKit dehydrated payload: find the node whose flat data has a 'jobs' key
    flat: list | None = None
    for node in outer.get('nodes', []):
        if isinstance(node, dict) and isinstance(node.get('data'), list):
            d = node['data']
            if d and isinstance(d[0], dict) and 'jobs' in d[0]:
                flat = d
                break

    if not flat:
        log.warning('  [Block.xyz] could not find jobs node in response')
        return None

    # Navigate the reference tree
    root = flat[0]
    jobs_ref = root.get('jobs')
    if not isinstance(jobs_ref, int) or jobs_ref >= len(flat):
        return None
    jobs_obj = flat[jobs_ref]
    if not isinstance(jobs_obj, dict):
        return None

    page_arr_ref = jobs_obj.get('currentPage')
    if not isinstance(page_arr_ref, int) or page_arr_ref >= len(flat):
        return None
    job_indices = flat[page_arr_ref]
    if not isinstance(job_indices, list):
        return None

    total = jobs_obj.get('total', 0)
    log.debug('  [Block.xyz] total jobs on site: %d', total)

    results: list[dict] = []
    for schema_idx in job_indices:
        if not isinstance(schema_idx, int) or schema_idx >= len(flat):
            continue
        schema = flat[schema_idx]
        if not isinstance(schema, dict):
            continue

        def _r(ref, _flat=flat):
            """Resolve a flat-array index reference to its value."""
            if isinstance(ref, int) and 0 <= ref < len(_flat):
                return _flat[ref]
            return ref

        title = _r(schema.get('title', ''))
        if not isinstance(title, str) or not title:
            continue
        if not is_job_match(title):
            continue

        location = _r(schema.get('location', ''))
        if not isinstance(location, str):
            location = ''

        is_remote = _r(schema.get('isRemote', False))
        if is_remote and not location:
            location = 'Remote'

        if not is_remote and location_re and location:
            if not location_re.search(location):
                log.debug('  [Block.xyz] ✗ loc: %s — %s', title[:50], location)
                continue

        job_id = _r(schema.get('id', ''))
        job_url = f'https://block.xyz/careers/jobs/{job_id}' if job_id else 'https://block.xyz/careers/jobs'
        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': location,
            'description': '',
        })

    log.debug('  [Block.xyz] matching jobs: %d (of %d total)', len(results), len(job_indices))
    return results


# ── Amazon ────────────────────────────────────────────────────────────────────
# URL pattern:  https://www.amazon.jobs/en/search?base_query=...
# API:          GET https://www.amazon.jobs/en/search.json
# Params:       base_query, loc_query, offset, result_limit, sort
# Response:     {"jobs": [...], "hits": N}
# Job fields:   title, location, job_path, id, description_short

def scrape_amazon(url: str, location_re, is_job_match,
                  log: logging.Logger) -> list[dict] | None:
    # Amazon uses two primary engineering title families:
    #   "Software Development Engineer" (SDE) — the main Amazon ladder
    #   "Software Engineer" — used by Twitch, Ring, AWS teams and acquisitions
    # Run both queries to capture the full picture; deduplicate by job id.
    _AMAZON_QUERIES = [
        'software development engineer',
        'senior software engineer',
        'principal software engineer',
    ]

    api_url = 'https://www.amazon.jobs/en/search.json'
    api_headers = {'Referer': 'https://www.amazon.jobs/en/search',
                   'X-Requested-With': 'XMLHttpRequest'}
    page_size = 100
    seen_ids: set[str] = set()
    results: list[dict] = []

    for base_query in _AMAZON_QUERIES:
        offset = 0
        log.info('  [Amazon] query=%r', base_query)

        while True:
            params = [
                ('base_query', base_query),
                ('offset', offset),
                ('result_limit', page_size),
                ('sort', 'relevant'),
                ('normalized_country_code[]', 'USA'),  # strict US filter
            ]
            try:
                r = _SESSION.get(api_url, params=params, timeout=20, headers=api_headers)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.warning('  [Amazon] API error at query=%r offset=%d: %s', base_query, offset, e)
                break

            jobs = data.get('jobs', [])
            total = data.get('hits', 0)
            log.debug('  [Amazon] offset=%d/%d page=%d', offset, total, len(jobs))

            if not jobs:
                break

            for job in jobs:
                job_id = str(job.get('id') or job.get('id_icims') or '')
                if job_id and job_id in seen_ids:
                    continue

                title = (job.get('title') or '').strip()
                if not title or not is_job_match(title):
                    continue

                if job_id:
                    seen_ids.add(job_id)

                location = (job.get('location') or '').strip()
                # Amazon uses "Virtual" for remote positions
                is_remote = any(w in location.lower() for w in ('virtual', 'remote'))

                if not is_remote and location_re and location:
                    if not location_re.search(location):
                        continue

                job_path = job.get('job_path') or ''
                job_url = (f'https://www.amazon.jobs{job_path}'
                           if job_path.startswith('/') else job_path
                           or 'https://www.amazon.jobs/en/search')

                results.append({
                    'title': title,
                    'url': job_url,
                    'apply_url': job_url,
                    'location': location,
                    'description': (job.get('description_short') or '').strip(),
                })

            offset += len(jobs)
            if offset >= total or len(jobs) < page_size:
                break

    log.info('  [Amazon] %d unique matching jobs total', len(results))
    return results


# ── Jane Street ───────────────────────────────────────────────────────────────
# URL pattern:  https://www.janestreet.com/jobs/main.json
# API:          Static JSON file, no pagination needed
# Response:     list of {id, position, category, availability, city, overview}
# Job URL:      https://www.janestreet.com/join-jane-street/open-roles/{id}/

_JS_TECH_CATEGORIES = {'Technology', 'Cybersecurity', 'Machine Learning', 'Quantitative Research'}
_JS_ROLE_RE = re.compile(r'\bEngineer\b|\bDeveloper\b|\bArchitect\b|\bProgrammer\b', re.IGNORECASE)
_JS_EXCLUDE_RE = re.compile(r'\b(Recruiter|Compliance|Legal|Sales|Analyst(?!\s*Engineer)|Finance|HR|Tax|Coordinator|Specialist|Clerk)\b', re.IGNORECASE)

def scrape_janestreet(url: str, location_re, is_job_match,
                      log: logging.Logger) -> list[dict] | None:
    log.info('  [JaneStreet] fetching %s', url)
    try:
        r = _SESSION.get('https://www.janestreet.com/jobs/main.json', timeout=20)
        r.raise_for_status()
        jobs = r.json()
    except Exception as e:
        log.warning('  [JaneStreet] fetch failed: %s', e)
        return None

    results: list[dict] = []
    for job in jobs:
        title = (job.get('position') or '').strip()
        category = (job.get('category') or '').strip()
        if not title:
            continue
        # Jane Street doesn't use seniority prefixes; match tech roles by category + title
        tech_role = (category in _JS_TECH_CATEGORIES and
                     bool(_JS_ROLE_RE.search(title)) and
                     not bool(_JS_EXCLUDE_RE.search(title)))
        if not tech_role:
            continue

        location = (job.get('city') or '').strip()
        is_remote = 'remote' in location.lower()

        if not is_remote and location_re and location:
            if not location_re.search(location):
                continue

        job_id = job.get('id', '')
        job_url = (f'https://www.janestreet.com/join-jane-street/open-roles/{job_id}/'
                   if job_id else 'https://www.janestreet.com/join-jane-street/open-roles/')
        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': location,
            'description': (job.get('overview') or '').strip(),
        })

    log.info('  [JaneStreet] %d matching jobs (of %d total)', len(results), len(jobs))
    return results


# ── Netflix ────────────────────────────────────────────────────────────────────
# URL pattern:  https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com&query=...
# API:          GET https://explore.jobs.netflix.net/api/apply/v2/jobs
# Params:       domain=netflix.com, query, start, num
# Response:     {"positions": [...], "count": N}
# Job fields:   id, name, location, locations, tags

_NETFLIX_QUERIES = [
    'senior software engineer',
    'staff software engineer',
    'principal software engineer',
    'software engineer L5',
    'software engineer L6',
]
_NETFLIX_SENIOR_RE = re.compile(
    r'\bL[56]\b|\b[56]/[56789]\b|\bSenior\b|\bStaff\b|\bPrincipal\b', re.IGNORECASE
)
_NETFLIX_ROLE_RE = re.compile(r'\bSoftware\s*Engineer\b|\bSWE\b|\bSDE\b', re.IGNORECASE)

def scrape_netflix(url: str, location_re, is_job_match,
                   log: logging.Logger) -> list[dict] | None:
    # Netflix API caps at 10 results per page; run multiple queries + paginate to get
    # all L5+ (Senior) and L6 (Staff) software engineering roles.
    api_url = 'https://explore.jobs.netflix.net/api/apply/v2/jobs'
    page_size = 10
    results: list[dict] = []
    seen_ids: set[str] = set()

    for query in _NETFLIX_QUERIES:
        start = 0
        log.info('  [Netflix] query=%r', query)

        while True:
            params = {'domain': 'netflix.com', 'query': query,
                      'start': start, 'num': page_size, 'sort_by': 'relevance'}
            try:
                r = _SESSION.get(api_url, params=params, timeout=20)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.warning('  [Netflix] API error query=%r start=%d: %s', query, start, e)
                break

            positions = data.get('positions', [])
            total = data.get('count', 0)
            log.debug('  [Netflix] query=%r start=%d/%d', query, start, total)

            if not positions:
                break

            for pos in positions:
                job_id = str(pos.get('id') or '')
                if job_id and job_id in seen_ids:
                    continue

                title = (pos.get('name') or pos.get('posting_name') or '').strip()
                if not title:
                    continue
                # Netflix uses level numbers: L5=senior, L6=staff
                if not _NETFLIX_SENIOR_RE.search(title):
                    continue
                if not _NETFLIX_ROLE_RE.search(title):
                    continue

                if job_id:
                    seen_ids.add(job_id)

                locs = pos.get('locations') or []
                location = locs[0] if locs else (pos.get('location') or '')
                is_remote = 'remote' in location.lower()

                if not is_remote and location_re and location:
                    if not location_re.search(location):
                        continue

                job_url = f'https://jobs.netflix.com/jobs/{job_id}' if job_id else 'https://jobs.netflix.com'
                results.append({
                    'title': title,
                    'url': job_url,
                    'apply_url': job_url,
                    'location': location,
                    'description': '',
                })

            start += len(positions)
            if start >= total or len(positions) < page_size:
                break

    log.info('  [Netflix] %d unique matching jobs', len(results))
    return results


# ── TwoSigma ──────────────────────────────────────────────────────────────────
# TwoSigma uses Avature ATS which doesn't expose a JSON API, but publishes an
# RSS feed at /careers/OpenRoles/feed/ containing all open positions.

_TS_ROLE_RE = re.compile(
    r'\bEngineer\b|\bDeveloper\b|\bArchitect\b|\bResearcher\b|\bScientist\b',
    re.IGNORECASE,
)
_TS_EXCLUDE_RE = re.compile(
    r'\b(Intern|Co-?op|Apprentice|Manager|Director|Recruiter|Coordinator|'
    r'Analyst(?!\s*Engineer)|Legal|Finance|Accountant|HR\b|People\s+Partner|'
    r'Compliance|Sales|Marketing|Procurement|Operations|Specialist)\b',
    re.IGNORECASE,
)


def scrape_twosigma(url: str, location_re, is_job_match,
                    log: logging.Logger) -> list[dict]:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    feed_url = f"{base}/careers/OpenRoles/feed/?jobRecordsPerPage=1000"
    log.debug("  [TwoSigma] fetching RSS %s", feed_url)

    try:
        r = _SESSION.get(feed_url, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.text.encode('utf-8'))
    except Exception as e:
        log.warning("  [TwoSigma] feed request failed: %s", e)
        return []

    results = []
    for item in root.findall('.//item'):
        title = (item.findtext('title') or '').strip()
        if not title:
            continue
        if not _api_job_match(title, is_job_match):
            continue
        if _TS_EXCLUDE_RE.search(title):
            continue

        job_url = (item.findtext('link') or item.findtext('guid') or '').strip()
        location = (item.findtext('description') or '').strip()
        # description is "Country City" e.g. "United States New York City"
        if location_re and location and not location_re.search(location):
            # Remote is not listed separately; skip if no match
            if 'remote' not in location.lower():
                continue

        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': location,
            'description': '',
        })

    log.info('  [TwoSigma] %d matching jobs', len(results))
    return results


# ── HudsonRiverTrading ────────────────────────────────────────────────────────
# HRT runs a custom WordPress plugin ("hrt-jobs") that exposes all jobs via a
# WordPress admin-ajax.php handler: action=get_hrt_jobs_handler.
# The `data` field must be a JSON string (e.g. '{"search":""}').
# The response is a JSON array; each element has 'title' and 'content' (HTML).
# Location is embedded in data-term attribute: "new-york===london===singapore".

_HRT_ROLE_RE = re.compile(
    r'\bEngineer\b|\bDeveloper\b|\bArchitect\b|\bResearcher\b|\bScientist\b',
    re.IGNORECASE,
)
_HRT_EXCLUDE_RE = re.compile(
    r'\b(Intern|Co-?op|Campus|Recruiter|Coordinator|Manager|Director|'
    r'Analyst(?!\s*Engineer)|Legal|Finance|Accountant|HR\b|Compliance|'
    r'Sales|Marketing|Procurement|Operations|Specialist|Trader(?!\s+(?:Engineer|Systems)))\b',
    re.IGNORECASE,
)

_HRT_CAREERS_URL = 'https://www.hudsonrivertrading.com/careers/'
_HRT_AJAX_URL = 'https://www.hudsonrivertrading.com/wp-admin/admin-ajax.php'


def scrape_hrt(url: str, location_re, is_job_match,
               log: logging.Logger) -> list[dict]:
    log.debug("  [HRT] fetching careers page for settings")
    try:
        r = _SESSION.get(_HRT_CAREERS_URL, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.warning("  [HRT] careers page request failed: %s", e)
        return []

    # Extract data-filters-settings from the .hrt-card-wrapper element
    from html import unescape as _unescape
    settings_raw = re.findall(r'data-filters-settings="([^"]+)"', r.text)
    settings = _unescape(settings_raw[0]) if settings_raw else '{}'

    log.debug("  [HRT] calling AJAX endpoint")
    try:
        import json as _json
        resp = _SESSION.post(
            _HRT_AJAX_URL,
            data={
                'action': 'get_hrt_jobs_handler',
                'data': _json.dumps({'search': ''}),
                'queryparams': '',
                'setting': settings,
            },
            headers={
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': _HRT_CAREERS_URL,
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            },
            timeout=30,
        )
        resp.raise_for_status()
        jobs_data = resp.json()
    except Exception as e:
        log.warning("  [HRT] AJAX request failed: %s", e)
        return []

    if not isinstance(jobs_data, list):
        log.warning("  [HRT] unexpected response type: %s", type(jobs_data))
        return []

    results = []
    for job in jobs_data:
        title = (job.get('title') or '').strip()
        # Decode HTML entities in title
        title = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), title)
        title = title.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        if not title:
            continue
        if not _api_job_match(title, is_job_match):
            continue
        if _HRT_EXCLUDE_RE.search(title):
            continue

        content = job.get('content', '')
        # Extract job URL from href
        link_m = re.search(r'href="(https://www\.hudsonrivertrading\.com/hrt-job/[^"]+)"', content)
        job_url = link_m.group(1) if link_m else _HRT_CAREERS_URL

        # Extract locations from data-term attribute
        # e.g. data-term="new-york===london===software-engineeringc"
        term_m = re.search(r'data-term="([^"]*)"', content)
        location_parts = []
        if term_m:
            for part in term_m.group(1).split('==='):
                # Map slug to display name; skip non-location terms
                loc_map = {
                    'new-york': 'New York', 'new-york-city': 'New York',
                    'london': 'London', 'singapore': 'Singapore',
                    'chicago': 'Chicago', 'austin': 'Austin',
                    'remote': 'Remote', 'san-francisco': 'San Francisco',
                }
                if part in loc_map:
                    location_parts.append(loc_map[part])
        location = ', '.join(location_parts) if location_parts else 'New York'

        if location_re and location:
            if not location_re.search(location) and 'remote' not in location.lower():
                continue

        results.append({
            'title': title,
            'url': job_url,
            'apply_url': job_url,
            'location': location,
            'description': (job.get('description') or '').strip(),
        })

    log.info('  [HRT] %d matching jobs', len(results))
    return results


# ── Dispatcher ────────────────────────────────────────────────────────────────

def api_scrape(company: str, url: str,
               location_re, is_job_match,
               log: logging.Logger) -> list[dict] | None:
    """
    Try an API-based strategy for the given URL.
    Returns list[dict] if successful (may be empty), or None to fall through to Playwright.
    """
    strategy = detect_strategy(url)
    log.debug("  Strategy detected: %s", strategy)

    if strategy == 'workday':
        return scrape_workday(url, location_re, is_job_match, log)

    if strategy == 'greenhouse':
        return scrape_greenhouse(url, location_re, is_job_match, log)

    if strategy == 'lever':
        return scrape_lever(url, location_re, is_job_match, log)

    if strategy == 'ashby':
        return scrape_ashby(url, location_re, is_job_match, log)

    if strategy == 'microsoft':
        return scrape_microsoft(url, location_re, is_job_match, log)

    if strategy == 'smartrecruiters':
        return scrape_smartrecruiters(url, location_re, is_job_match, log)

    if strategy == 'salesforce':
        return scrape_salesforce(url, location_re, is_job_match, log)

    if strategy == 'meta':
        return scrape_meta(url, location_re, is_job_match, log)

    if strategy == 'doordash':
        return scrape_doordash(url, location_re, is_job_match, log)

    if strategy == 'google':
        return scrape_google(url, location_re, is_job_match, log)

    if strategy == 'block_xyz':
        return scrape_block_xyz(url, location_re, is_job_match, log)

    if strategy == 'amazon':
        return scrape_amazon(url, location_re, is_job_match, log)

    if strategy == 'janestreet':
        return scrape_janestreet(url, location_re, is_job_match, log)

    if strategy == 'netflix':
        return scrape_netflix(url, location_re, is_job_match, log)

    if strategy == 'twosigma':
        return scrape_twosigma(url, location_re, is_job_match, log)

    if strategy == 'hrt':
        return scrape_hrt(url, location_re, is_job_match, log)

    return None   # playwright fallback


# ── HTML utility ──────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\n{3,}')

def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(' ', html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text
