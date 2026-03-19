"""
Persistent job state (applied/etc.) stored in SQLite.

DB file: jobs_state.db (next to this script).
Hash:    SHA-256 of "company\\0title" (both lowercased+stripped), truncated to 16 hex chars.
         Stable across rescapes as long as the company folder name and job title don't change.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent / 'jobs_state.db'


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS applied_jobs (
                job_hash  TEXT PRIMARY KEY,
                company   TEXT NOT NULL,
                title     TEXT NOT NULL,
                url       TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        ''')


def compute_hash(company: str, title: str) -> str:
    """Deterministic 16-char hex hash for a (company, title) pair."""
    key = f"{company.lower().strip()}\0{title.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def get_applied_hashes() -> set[str]:
    with _conn() as conn:
        rows = conn.execute('SELECT job_hash FROM applied_jobs').fetchall()
    return {r['job_hash'] for r in rows}


def mark_applied(job_hash: str, company: str, title: str, url: str = '') -> None:
    with _conn() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO applied_jobs (job_hash, company, title, url) VALUES (?,?,?,?)',
            (job_hash, company, title, url),
        )


def unmark_applied(job_hash: str) -> None:
    with _conn() as conn:
        conn.execute('DELETE FROM applied_jobs WHERE job_hash = ?', (job_hash,))
