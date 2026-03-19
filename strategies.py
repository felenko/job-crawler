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


# ── Strategy detection ────────────────────────────────────────────────────────

def detect_strategy(url: str) -> str:
    """
    Return the strategy name for a given URL, or 'playwright' as fallback.
    Strategies: 'workday', 'greenhouse', 'lever', 'ashby', 'microsoft',
                'smartrecruiters', 'salesforce', 'playwright'
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
        if not is_job_match(title):
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
        if not is_job_match(title):
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
# API:          POST https://api.ashbyhq.com/posting-public/graphql
# Body:         {"operationName":"ApiJobBoardWithTeams","variables":{"organizationHostedJobsPageName":"{slug}"},...}

def _ashby_slug(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip('/').split('/') if p]
    return parts[0] if parts else None


def scrape_ashby(url: str, location_re, is_job_match,
                 log: logging.Logger) -> list[dict] | None:
    slug = _ashby_slug(url)
    if not slug:
        return None

    api_url = "https://api.ashbyhq.com/posting-public/graphql"
    query = """
    query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
      jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
        jobPostings {
          id title locationName isRemote jobPostingState
          externalLink
          team { name }
        }
      }
    }
    """
    payload = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": slug},
        "query": query,
    }
    log.info("  [Ashby API] slug=%s", slug)

    try:
        r = _SESSION.post(api_url, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("  [Ashby API] failed: %s", e)
        return None

    postings = (data.get('data', {})
                    .get('jobBoard', {})
                    .get('jobPostings', []))

    results = []
    for p in postings:
        if p.get('jobPostingState') != 'Published':
            continue
        title = (p.get('title') or '').strip()
        if not is_job_match(title):
            continue

        loc = (p.get('locationName') or '').strip()
        if p.get('isRemote') and not loc:
            loc = 'Remote'
        job_url = p.get('externalLink') or f"https://jobs.ashbyhq.com/{slug}/{p.get('id','')}"

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
    log.info('  [Salesforce] %s', _SF_FEED)

    def _text(el, tag: str) -> str:
        child = el.find(tag)
        return (child.text or '').strip() if child is not None else ''

    results: list[dict] = []
    seen_ids: set = set()

    for loc_query in ('New York', 'Remote'):
        page = 1
        log.debug('  [SF] querying location=%r', loc_query)
        while True:
            try:
                r = _SESSION.get(_SF_FEED, params={
                    'search': 'software engineer',
                    'location': loc_query,
                    'page': page,
                }, timeout=20)
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception as e:
                log.warning('  [SF] feed failed loc=%r page=%d: %s', loc_query, page, e)
                break

            jobs = root.findall('job')
            if not jobs:
                break

            for job in jobs:
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

            log.debug('  [SF] loc=%r page=%d got=%d matching=%d',
                      loc_query, page, len(jobs), len(results))
            page += 1
            if len(jobs) < 10:
                break
            time.sleep(0.2)

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
