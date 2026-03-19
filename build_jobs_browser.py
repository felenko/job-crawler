"""
Build a static HTML browser for scraped jobs.

Scans the Jobs/ output folder (company dirs with .last_scan / .last_empty / .last_error
and .md job files), then writes jobs_browser/index.html with:
  - Collapsible company nodes
  - Last update time and any error message per company
  - List of jobs under each company with link to application

Usage:
  python build_jobs_browser.py
  python build_jobs_browser.py --jobs-dir Jobs --out jobs_browser
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from datetime import datetime, timezone

MARKER_SCAN = '.last_scan'
MARKER_EMPTY = '.last_empty'
MARKER_ERROR = '.last_error'
APPLY_URL_RE = re.compile(r'^\*\*Apply URL:\*\*\s*(.+)$', re.IGNORECASE)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _parse_md_job(md_path: Path) -> dict:
    """Extract title (first # line) and apply_url from a job .md file."""
    title = md_path.stem.replace('_', ' ')
    apply_url = ''
    text = md_path.read_text(encoding='utf-8')
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('# '):
            title = line[2:].strip()
        m = APPLY_URL_RE.match(line)
        if m:
            apply_url = (m.group(1) or '').strip()
            break
    return {'title': title, 'apply_url': apply_url, 'path': md_path.name}


def _company_status(company_dir: Path) -> tuple[str, str | None, str | None]:
    """
    Return (status, timestamp_iso, error_message).
    status is 'saved' | 'empty' | 'error'.
    """
    scan = _read_json(company_dir / MARKER_SCAN)
    empty = _read_json(company_dir / MARKER_EMPTY)
    err = _read_json(company_dir / MARKER_ERROR)

    # Prefer most recent marker for "last update"
    best_ts: str | None = None
    status = 'unknown'
    error_msg: str | None = None

    if err and err.get('timestamp'):
        best_ts = err['timestamp']
        status = 'error'
        error_msg = err.get('error') or 'Unknown error'
    if empty and empty.get('timestamp'):
        ts = empty['timestamp']
        if not best_ts or ts > best_ts:
            best_ts = ts
            if status != 'error':
                status = 'empty'
    if scan and scan.get('timestamp'):
        ts = scan['timestamp']
        if not best_ts or ts > best_ts:
            best_ts = ts
            status = 'saved'

    return status, best_ts or None, error_msg


def _format_time(iso: str | None) -> str:
    if not iso:
        return '—'
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return iso


def _company_seed_url(company_dir: Path) -> str:
    """Return the seed URL stored in the most recent marker file, or ''."""
    for marker in (MARKER_SCAN, MARKER_EMPTY, MARKER_ERROR):
        data = _read_json(company_dir / marker)
        if data and data.get('url'):
            return data['url'].strip()
    return ''


def collect_browser_data(jobs_dir: Path) -> list[dict]:
    """Build list of companies with last update, status, error, and jobs."""
    data = []
    if not jobs_dir.is_dir():
        return data

    for company_dir in sorted(jobs_dir.iterdir()):
        if not company_dir.is_dir() or company_dir.name.startswith('.'):
            continue
        company = company_dir.name
        status, timestamp_iso, error_msg = _company_status(company_dir)
        jobs = []
        for md in sorted(company_dir.glob('*.md')):
            jobs.append(_parse_md_job(md))

        data.append({
            'company': company,
            'status': status,
            'last_update': timestamp_iso,
            'last_update_display': _format_time(timestamp_iso),
            'error': error_msg,
            'jobs': jobs,
            'seed_url': _company_seed_url(company_dir),
        })
    return data


def build_html(data: list[dict], out_path: Path) -> None:
    """Write a single index.html with embedded JSON and collapsible UI."""
    json_escaped = json.dumps(data).replace('<', '\\u003c').replace('>', '\\u003e')
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Scraped Jobs</title>
  <style>
    :root {{ font-family: system-ui, sans-serif; font-size: 15px; line-height: 1.4; }}
    body {{ max-width: 900px; margin: 0 auto; padding: 1rem; background: #f8f9fa; }}
    h1 {{ font-size: 1.5rem; color: #1a1a1a; margin-bottom: 0.5rem; }}
    .meta {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    .company {{ margin-bottom: 0.5rem; border: 1px solid #dee2e6; border-radius: 6px; background: #fff; overflow: hidden; }}
    .company-header {{ display: flex; align-items: center; gap: 0.75rem; padding: 0.6rem 0.85rem; cursor: pointer; user-select: none; }}
    .company-header:hover {{ background: #f1f3f5; }}
    .company-header .toggle {{ font-size: 0.8rem; color: #868e96; transition: transform 0.15s; }}
    .company.open .toggle {{ transform: rotate(90deg); }}
    .company-name {{ font-weight: 600; color: #212529; }}
    .company-badges {{ display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}
    .badge {{ font-size: 0.7rem; padding: 0.2rem 0.45rem; border-radius: 4px; }}
    .badge.saved {{ background: #d3f9d8; color: #2b8a3e; }}
    .badge.empty {{ background: #fff3bf; color: #e67700; }}
    .badge.error {{ background: #ffe3e3; color: #c92a2a; }}
    .company-time {{ font-size: 0.8rem; color: #868e96; }}
    .company-body {{ padding: 0 0.85rem 0.85rem; border-top: 1px solid #eee; display: none; }}
    .company.open .company-body {{ display: block; }}
    .company-error {{ font-size: 0.85rem; color: #c92a2a; background: #fff5f5; padding: 0.5rem 0.6rem; border-radius: 4px; margin-top: 0.5rem; white-space: pre-wrap; word-break: break-word; }}
    .job-list {{ list-style: none; margin: 0.5rem 0 0; padding: 0; }}
    .job-list li {{ margin: 0.35rem 0; }}
    .job-list a {{ color: #1971c2; text-decoration: none; }}
    .job-list a:hover {{ text-decoration: underline; }}
    .job-title {{ font-size: 0.95rem; }}
  </style>
</head>
<body>
  <h1>Scraped Jobs</h1>
  <p class="meta">Companies as collapsible nodes — last update, status, and job links.</p>
  <div id="root"></div>
  <script>
    const data = {json_escaped};
    const root = document.getElementById('root');
    data.forEach((c, i) => {{
      const open = c.status === 'saved' && c.jobs.length > 0;
      const div = document.createElement('div');
      div.className = 'company' + (open ? ' open' : '');
      div.innerHTML = `
        <div class="company-header" data-i="${{i}}">
          <span class="toggle">▶</span>
          <span class="company-name">${{escapeHtml(c.company)}}</span>
          <span class="company-badges">
            <span class="badge ${{c.status}}">${{c.status}}</span>
            <span class="company-time">${{escapeHtml(c.last_update_display)}}</span>
          </span>
        </div>
        <div class="company-body">
          ${{c.error ? `<div class="company-error">${{escapeHtml(c.error)}}</div>` : ''}}
          <ul class="job-list">
            ${{c.jobs.map(j => `
              <li><a href="${{escapeAttr(j.apply_url)}}" target="_blank" rel="noopener" class="job-title">${{escapeHtml(j.title)}}</a></li>
            `).join('')}}
          </ul>
        </div>
      `;
      root.appendChild(div);
      div.querySelector('.company-header').addEventListener('click', () => {{
        div.classList.toggle('open');
      }});
    }});
    function escapeHtml(s) {{ return (s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}
    function escapeAttr(s) {{ return (s ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }}
  </script>
</body>
</html>
'''
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding='utf-8')


def main() -> None:
    ap = argparse.ArgumentParser(description='Build static HTML browser for scraped jobs')
    ap.add_argument('--jobs-dir', '-j', default='Jobs', help='Jobs output folder to scan')
    ap.add_argument('--out', '-o', default='jobs_browser', help='Output folder for index.html')
    args = ap.parse_args()
    jobs_dir = Path(args.jobs_dir)
    out_dir = Path(args.out)
    out_path = out_dir / 'index.html'
    data = collect_browser_data(jobs_dir)
    build_html(data, out_path)
    print(f"Wrote {len(data)} companies to {out_path.resolve()}")


if __name__ == '__main__':
    main()
