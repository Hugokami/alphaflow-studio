# -*- coding: utf-8 -*-
"""
LTstudio High-Concurrency SQLite WAL Job Queue & Persistent State Store
Replaces fragile single-file jobs_registry.json with zero-lock concurrent reading & writing.
Inspired by openbot architecture.
"""

import collections.abc
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent

if os.environ.get("VERCEL"):
    DATA_DIR = Path("/tmp/ltstudio_data")
else:
    DATA_DIR = BASE_DIR / "data"

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path("/tmp/ltstudio_data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "studio_jobs.db"
LEGACY_JSON_PATH = DATA_DIR / "jobs_registry.json"


def get_db_connection() -> sqlite3.Connection:
    """Creates a SQLite connection with WAL journal mode and busy timeout configured."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_db() -> None:
    """Initializes tables, indexes, and automatically migrates legacy JSON store if present."""
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS studio_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER DEFAULT 0,
                step INTEGER DEFAULT 0,
                log TEXT DEFAULT '',
                mode TEXT DEFAULT 'recap',
                voice TEXT DEFAULT 'Charon',
                aspect TEXT DEFAULT '9:16',
                duration REAL DEFAULT 0.0,
                params TEXT DEFAULT '{}',
                data TEXT DEFAULT '{}',
                created_at REAL,
                updated_at REAL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON studio_jobs(created_at DESC);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON studio_jobs(status);")
        conn.commit()
    finally:
        conn.close()

    auto_migrate_legacy_json()


def auto_migrate_legacy_json() -> int:
    """Migrates existing completed/processing jobs from jobs_registry.json into SQLite WAL."""
    if not LEGACY_JSON_PATH.exists():
        return 0

    try:
        raw_text = LEGACY_JSON_PATH.read_text(encoding="utf-8").strip()
        if not raw_text:
            return 0
        legacy_data = json.loads(raw_text)
        if not isinstance(legacy_data, dict):
            return 0
    except Exception as e:
        print(f"[*] [Database] Warning reading legacy jobs_registry.json: {e}")
        return 0

    migrated_count = 0
    conn = get_db_connection()
    now = time.time()
    try:
        for job_id, item in legacy_data.items():
            if not isinstance(item, dict):
                continue
            status = item.get("status", "completed")
            progress = int(item.get("progress") or 100)
            step = int(item.get("step") or 5)
            log_msg = item.get("log", "")
            params = item.get("params", {})
            mode = params.get("processing_mode") or item.get("mode", "recap")
            voice = params.get("voice") or item.get("voice", "Charon")
            aspect = params.get("aspect_ratio") or item.get("aspect", "9:16")
            duration = float(item.get("duration") or 0.0)
            created_at = float(item.get("created_at") or now)

            # Store remaining properties in data JSON
            extra_data = {k: v for k, v in item.items() if k not in ("job_id", "id", "params")}
            conn.execute("""
                INSERT OR REPLACE INTO studio_jobs
                (id, status, progress, step, log, mode, voice, aspect, duration, params, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id,
                status,
                progress,
                step,
                log_msg,
                mode,
                voice,
                aspect,
                duration,
                json.dumps(params, ensure_ascii=False),
                json.dumps(extra_data, ensure_ascii=False),
                created_at,
                now,
            ))
            migrated_count += 1
        conn.commit()

        # Rename legacy file to .bak to avoid re-migration
        bak_path = DATA_DIR / "jobs_registry.json.bak"
        try:
            if bak_path.exists():
                bak_path.unlink()
            LEGACY_JSON_PATH.rename(bak_path)
            print(f"[+] [Database] Successfully migrated {migrated_count} legacy jobs to SQLite WAL ({DB_PATH.name})")
        except Exception as rename_err:
            print(f"[*] [Database] Notice archiving legacy JSON: {rename_err}")

    except Exception as err:
        print(f"[*] [Database] Migration error: {err}")
    finally:
        conn.close()

    return migrated_count


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Helper to convert a sqlite3.Row into a complete job dictionary."""
    out: Dict[str, Any] = {
        "job_id": row["id"],
        "id": row["id"],
        "status": row["status"],
        "progress": row["progress"],
        "step": row["step"],
        "log": row["log"],
        "mode": row["mode"],
        "voice": row["voice"],
        "aspect": row["aspect"],
        "duration": row["duration"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    # Unpack params
    try:
        out["params"] = json.loads(row["params"]) if row["params"] else {}
    except Exception:
        out["params"] = {}

    # Unpack arbitrary data payload
    try:
        data_payload = json.loads(row["data"]) if row["data"] else {}
        if isinstance(data_payload, dict):
            for k, v in data_payload.items():
                if k not in out or out[k] is None:
                    out[k] = v
    except Exception:
        pass

    return out


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves a single job by ID from the SQLite WAL queue."""
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM studio_jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


def save_job(job_id: str, job_dict: Dict[str, Any]) -> None:
    """Inserts or replaces a full job definition in the database."""
    conn = get_db_connection()
    now = time.time()
    try:
        status = job_dict.get("status", "processing")
        progress = int(job_dict.get("progress") or 0)
        step = int(job_dict.get("step") or 0)
        log_msg = str(job_dict.get("log") or "")
        params = job_dict.get("params") or {}
        mode = params.get("processing_mode") or job_dict.get("mode", "recap")
        voice = params.get("voice") or job_dict.get("voice", "Charon")
        aspect = params.get("aspect_ratio") or job_dict.get("aspect", "9:16")
        duration = float(job_dict.get("duration") or 0.0)
        created_at = float(job_dict.get("created_at") or now)

        known_cols = {"id", "job_id", "status", "progress", "step", "log", "mode", "voice", "aspect", "duration", "params", "created_at", "updated_at"}
        extra_data = {k: v for k, v in job_dict.items() if k not in known_cols}

        conn.execute("""
            INSERT OR REPLACE INTO studio_jobs
            (id, status, progress, step, log, mode, voice, aspect, duration, params, data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id,
            status,
            progress,
            step,
            log_msg,
            mode,
            voice,
            aspect,
            duration,
            json.dumps(params, ensure_ascii=False),
            json.dumps(extra_data, ensure_ascii=False),
            created_at,
            now,
        ))
        conn.commit()
    finally:
        conn.close()


def update_job(job_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """
    Atomically updates fields of an existing job.
    Directly updates indexed table columns or merges into the `data` JSON column.
    """
    conn = get_db_connection()
    now = time.time()
    try:
        cur = conn.execute("SELECT * FROM studio_jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            # If job doesn't exist yet, create basic stub
            stub = {"job_id": job_id, "created_at": now, **kwargs}
            save_job(job_id, stub)
            return get_job(job_id)

        job_data = _row_to_dict(row)
        job_data.update(kwargs)
        job_data["updated_at"] = now

        status = job_data.get("status", "processing")
        progress = int(job_data.get("progress", 0))
        step = int(job_data.get("step", 0))
        log_msg = str(job_data.get("log", ""))
        duration = float(job_data.get("duration", 0.0))
        params = job_data.get("params", {})

        known_cols = {"id", "job_id", "status", "progress", "step", "log", "mode", "voice", "aspect", "duration", "params", "created_at", "updated_at"}
        extra_data = {k: v for k, v in job_data.items() if k not in known_cols}

        conn.execute("""
            UPDATE studio_jobs
            SET status = ?, progress = ?, step = ?, log = ?, duration = ?, params = ?, data = ?, updated_at = ?
            WHERE id = ?
        """, (
            status,
            progress,
            step,
            log_msg,
            duration,
            json.dumps(params, ensure_ascii=False),
            json.dumps(extra_data, ensure_ascii=False),
            now,
            job_id,
        ))
        conn.commit()
        return job_data
    finally:
        conn.close()


def list_jobs(limit: int = 50, offset: int = 0, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lists jobs sorted chronologically newest-first."""
    conn = get_db_connection()
    try:
        if status:
            cur = conn.execute(
                "SELECT * FROM studio_jobs WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset)
            )
        else:
            cur = conn.execute(
                "SELECT * FROM studio_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
        rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def delete_job(job_id: str) -> bool:
    """Deletes a job from the database."""
    conn = get_db_connection()
    try:
        cur = conn.execute("DELETE FROM studio_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


class JobRegistryProxy(collections.abc.MutableMapping):
    """
    Drop-in mapping proxy for app.py's legacy `JOBS` dictionary.
    Provides standard dict syntax `JOBS[job_id]`, `job_id in JOBS`, `JOBS.get()`, `JOBS.values()`
    while delegating all operations directly to the SQLite WAL queue.
    """

    def __getitem__(self, key: str) -> Dict[str, Any]:
        job = get_job(key)
        if job is None:
            raise KeyError(key)
        return job

    def __setitem__(self, key: str, value: Dict[str, Any]) -> None:
        save_job(key, value)

    def __delitem__(self, key: str) -> None:
        if not delete_job(key):
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        for j in list_jobs(limit=500):
            yield j["job_id"]

    def __len__(self) -> int:
        conn = get_db_connection()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM studio_jobs")
            return int(cur.fetchone()[0])
        finally:
            conn.close()

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return get_job(key) is not None

    def get(self, key: str, default: Any = None) -> Any:
        job = get_job(key)
        return job if job is not None else default

    def values(self) -> List[Dict[str, Any]]:
        return list_jobs(limit=100)


# Initialize DB upon module load
init_db()
db_jobs = JobRegistryProxy()
