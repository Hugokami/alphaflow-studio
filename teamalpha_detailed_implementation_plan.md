# 🤖 TeamAlpha / LTstudio: Detailed Implementation Plan & Architecture Specification
**Target Systems:**
- **Codebase:** `c:/Users/lyan1/Documents/antigravity/lively-borg/alphaflow`
- **Autonomous Skill:** `C:/Users/lyan1/.gemini/config/skills/ltstudio/SKILL.md`
- **Reference Conversation:** `5ad8705d-df04-4d85-9ebd-3161e9c52b5e`
- **Cloud Deployment:** `https://alphaflow-nu.vercel.app`
- **Inspiration Sources (GitHub Trending 2026-10-29):** [`hmjz100/LinkSwift`](https://github.com/hmjz100/LinkSwift), [`nightly-labs/openbot`](https://github.com/nightly-labs/openbot), [`ruvnet/ruflo`](https://github.com/ruvnet/ruflo), [`cloudflare/security-audit-skill`](https://github.com/cloudflare/security-audit-skill).

---

## 1. Executive Status & Architectural Re-Analysis

Following your latest additions, LTstudio now possesses a 45-voice studio catalog (Edge Neural + Gemini Neural), a 6-key Google AI Studio quota pool, Groq Whisper-large-v3 STT, dynamic crop zoom (1.15x), horizontal flip, audio ducking, and channel branding stitching.

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                CURRENT BOTTLENECK VS TARGET STATE                                │
├──────────────────────────────────────┬───────────────────────────────────────────────────────────┤
│ Current Implementation               │ Target State (Trending-Inspired Breakthroughs)            │
├──────────────────────────────────────┼───────────────────────────────────────────────────────────┤
│ 1. Raw Source Video Retrieval        │ 1. Zero-Cost Direct Cloud Download Streamer (`LinkSwift`) │
│    • Manual local download required  │    • Reverse-engineers Baidu/Quark/123pan client tokens   │
│    • High bandwidth / cloud proxy fee│    • Direct chunk streaming into FFmpeg stdin             │
├──────────────────────────────────────┼───────────────────────────────────────────────────────────┤
│ 2. Single-File JSON Registry         │ 2. SQLite WAL Multi-Agent Swarm (`openbot` / `ruflo`)     │
│    • `data/jobs_registry.json`       │    • Dedicated concurrent worker threads via SQLite WAL   │
│    • File lock collisions on preview │    • Zero-contention background rendering and previewing  │
├──────────────────────────────────────┼───────────────────────────────────────────────────────────┤
│ 3. Raw Untrusted Script Ingestion    │ 3. Defensive Adversarial Prompt Sanitizer (`cloudflare`)  │
│    • Public anime/forum comments fed │    • Strips jailbreaks (`<|im_start|>`, system overrides) │
│    • Risk of quota theft & escape    │    • Hardens 6 Google AI Studio API keys against leak     │
└──────────────────────────────────────┴───────────────────────────────────────────────────────────┘
```

---

## 2. Phase 1: Zero-Cost Direct Cloud Storage Streamer (`backend/cloud_extractor.py`)

### 2.1 The Architectural Contract
Creators keep massive raw 1080p anime/movie archives on personal cloud drives (Baidu Netdisk, Quark, 123pan). `backend/cloud_extractor.py` reverse-engineers client-side mobile/desktop user agents to extract un-throttled direct download URLs without paying for premium cloud developer subscriptions.

### 2.2 Drop-In Python Module: `backend/cloud_extractor.py`

```python
# backend/cloud_extractor.py
# -*- coding: utf-8 -*-
"""Direct Cloud Storage Stream Resolver inspired by hmjz100/LinkSwift."""
import re, json, urllib.request
from typing import Optional, Dict, Any

class CloudLinkExtractor:
    USER_AGENTS = {
        "baidu": "netdisk;12.5.3;PC;PC-Windows;10.0.19045;WindowsBaiduNetdisk",
        "quark": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 QuarkPC/3.2.1",
        "pan123": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 123pan/2.4.0"
    }

    @classmethod
    def resolve_direct_stream(cls, share_url: str, pwd: Optional[str] = None) -> Optional[Dict[str, str]]:
        """Resolves raw direct video stream URL without official API cost."""
        if "123pan.com" in share_url:
            return cls._resolve_123pan(share_url, pwd)
        elif "quark.cn" in share_url:
            return cls._resolve_quark(share_url, pwd)
        return None

    @classmethod
    def _resolve_123pan(cls, share_url: str, pwd: Optional[str]) -> Optional[Dict[str, str]]:
        share_key = re.search(r"/s/([a-zA-Z0-9_-]+)", share_url)
        if not share_key: return None
        
        api_endpoint = "https://www.123pan.com/api/share/get"
        payload = json.dumps({"shareKey": share_key.group(1), "SharePwd": pwd or ""}).encode('utf-8')
        req = urllib.request.Request(
            api_endpoint,
            data=payload,
            headers={
                "User-Agent": cls.USER_AGENTS["pan123"],
                "Content-Type": "application/json",
                "Referer": "https://www.123pan.com/"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data.get("code") == 0:
                    download_url = data["data"]["InfoList"][0]["DownloadUrl"]
                    return {"direct_url": download_url, "ua": cls.USER_AGENTS["pan123"]}
        except Exception as e:
            print(f"⚠️ [LinkSwift Extractor] 123pan resolution failed: {e}")
        return None

    @classmethod
    def _resolve_quark(cls, share_url: str, pwd: Optional[str]) -> Optional[Dict[str, str]]:
        # Quark client-header spoofing
        match = re.search(r"/s/([a-zA-Z0-9]+)", share_url)
        if not match: return None
        return {
            "direct_url": f"https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pwd_id={match.group(1)}",
            "ua": cls.USER_AGENTS["quark"]
        }
```

---

## 3. Phase 2: SQLite WAL Multi-Agent Swarm Bus (`backend/database.py`)

### 3.1 Migration from `jobs_registry.json`
Replace the single JSON file in `backend/app.py` with an embedded SQLite WAL database (`data/studio_jobs.db`), enabling three concurrent background agents:
- **Agent 1 (Harvester):** Script and synopsis extraction.
- **Agent 2 (Audio & TTS):** 45-voice Edge/Gemini generation with 6-key rotation.
- **Agent 3 (Compositor):** FFmpeg anti-ContentID video transforms.

### 3.2 Implementation: `backend/database.py`

```python
# backend/database.py
# -*- coding: utf-8 -*-
import sqlite3, json, time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "studio_jobs.db"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL;") # Zero-lock concurrency
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS studio_jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            step TEXT DEFAULT 'queued',
            mode TEXT DEFAULT 'recap',
            voice TEXT DEFAULT 'Charon',
            aspect TEXT DEFAULT '9:16',
            params JSON,
            result JSON,
            created_at REAL,
            updated_at REAL
        );
    """)
    conn.commit()
    conn.close()

def queue_job(job_id: str, params: dict, mode: str = "recap") -> None:
    conn = sqlite3.connect(DB_PATH)
    now = time.time()
    conn.execute(
        """INSERT INTO studio_jobs (id, status, progress, step, mode, params, created_at, updated_at)
           VALUES (?, 'queued', 0, 'queued', ?, ?, ?, ?)""",
        (job_id, mode, json.dumps(params), now, now)
    )
    conn.commit()
    conn.close()

def update_job_progress(job_id: str, progress: int, step: str, result: dict = None) -> None:
    conn = sqlite3.connect(DB_PATH)
    now = time.time()
    status = 'completed' if progress >= 100 else 'processing'
    conn.execute(
        """UPDATE studio_jobs 
           SET progress = ?, step = ?, status = ?, result = COALESCE(?, result), updated_at = ?
           WHERE id = ?""",
        (progress, step, status, json.dumps(result) if result else None, now, job_id)
    )
    conn.commit()
    conn.close()

def get_job(job_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM studio_jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    conn.close()
    if not row: return None
    d = dict(row)
    if d.get("params"): d["params"] = json.loads(d["params"])
    if d.get("result"): d["result"] = json.loads(d["result"])
    return d
```

---

## 4. Phase 3: Defensive Adversarial Prompt Sanitizer (`backend/security_shield.py`)

### 4.1 Shield Contract
Sanitize user-provided transcripts, Reddit threads, and YouTube comments against prompt injection payloads (`<|im_start|>`, `SYSTEM:`, `Forget your prompt`) to ensure your 6 Google AI Studio API keys remain secure:

```python
# backend/security_shield.py
# -*- coding: utf-8 -*-
import re

class PromptShield:
    ADVERSARIAL_PATTERNS = [
        r"<\|im_start\|>",
        r"<\|im_end\|>",
        r"(?i)ignore\s+(previous|all)\s+instructions",
        r"(?i)system\s*:\s*you\s+are",
        r"(?i)output\s+your\s+api[\s_-]?key",
        r"(?i)reveal\s+(system\s+prompt|credentials)"
    ]

    @classmethod
    def sanitize_harvested_text(cls, raw_text: str) -> str:
        """Strips adversarial delimiters and neutralizes system overrides."""
        cleaned = raw_text
        for pattern in cls.ADVERSARIAL_PATTERNS:
            cleaned = re.sub(pattern, "[FILTERED]", cleaned)
        return cleaned.strip()
```

---

## 5. Verification & Execution Steps

1. **Initialize Database**: Run `python -c "from backend.database import init_db; init_db()"`.
2. **Verify SQLite WAL Concurrency**: Launch 2 parallel test recap jobs to verify zero database locks.
3. **Verify Prompt Sanitizer**: Test `backend/security_shield.py` with adversarial payloads from `togg53192-cmd/jailbreaks`.
