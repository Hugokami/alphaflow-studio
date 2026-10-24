import json
import os
import time
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

BASE_DIR = Path(__file__).resolve().parent.parent

# In serverless environments like Vercel or read-only filesystems, use /tmp for runtime caches and writable state
if os.environ.get("VERCEL"):
    DATA_DIR = Path("/tmp/alphaflow_data")
else:
    DATA_DIR = BASE_DIR / "data"

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path("/tmp/alphaflow_data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

STORE_PATH = DATA_DIR / "keys_store.json"
ENV_PATH = BASE_DIR / ".env"


def _load_dotenv_file(env_path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class MultiKeyPoolManager:
    """
    Thread-safe Multi-Key Pool & Smart Failover Manager for Google AI Studio (Gemini)
    and Groq API keys, plus Smart Duration-Based Gemini TTS Model Routing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.env_vars = _load_dotenv_file(ENV_PATH)
        self.state: Dict[str, Any] = {
            "gemini_keys": [],
            "groq_keys": [],
            "tts_routing": {
                "mode": "smart_duration",  # "smart_duration" | "fixed"
                "default_model": self.env_vars.get("DEFAULT_GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"),
                "short_video_model": self.env_vars.get("SHORT_VIDEO_TTS_MODEL", "gemini-3.8-flash-tts"),
                "long_video_model": self.env_vars.get("LONG_VIDEO_TTS_MODEL", "gemini-3.8-flash-lite-tts"),
                "duration_threshold_sec": int(self.env_vars.get("DURATION_THRESHOLD_SEC", "180")),
                "fallback_models": [
                    "gemini-2.5-flash-preview-tts",
                    "gemini-3.8-flash-lite-tts",
                    "gemini-3.8-flash-tts",
                ],
            },
            "telegram": {
                "bot_token": self.env_vars.get("TELEGRAM_BOT_TOKEN", ""),
                "chat_id": self.env_vars.get("TELEGRAM_CHAT_ID", ""),
            },
        }
        self._load_or_seed()

    def _mask_key(self, key: str) -> str:
        if len(key) <= 12:
            return key[:4] + "..."
        return f"{key[:10]}...{key[-6:]}"

    def _load_or_seed(self) -> None:
        if STORE_PATH.exists():
            try:
                loaded = json.loads(STORE_PATH.read_text(encoding="utf-8"))
                self.state.update(loaded)
            except Exception:
                pass

        # Ensure env keys are seeded into the pool without duplicates
        env_gemini = [
            k.strip()
            for k in (os.environ.get("GEMINI_API_KEYS") or self.env_vars.get("GEMINI_API_KEYS", "")).split(",")
            if k.strip()
        ]
        env_groq = [
            k.strip()
            for k in (os.environ.get("GROQ_API_KEYS") or self.env_vars.get("GROQ_API_KEYS", "")).split(",")
            if k.strip()
        ]

        for idx, k in enumerate(env_gemini):
            self._ensure_key_entry("gemini", k, label=f"Google AI Studio #{idx + 1}")
        for idx, k in enumerate(env_groq):
            self._ensure_key_entry("groq", k, label=f"Groq Key #{idx + 1}")

        if (os.environ.get("TELEGRAM_BOT_TOKEN") or self.env_vars.get("TELEGRAM_BOT_TOKEN")) and not self.state["telegram"].get("bot_token"):
            self.state["telegram"]["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or self.env_vars.get("TELEGRAM_BOT_TOKEN", "")

        self._save()

    def _ensure_key_entry(self, provider: str, key: str, label: str = "") -> Dict[str, Any]:
        bucket = "gemini_keys" if provider == "gemini" else "groq_keys"
        for item in self.state[bucket]:
            if item["key"] == key:
                return item
        entry = {
            "id": f"{provider}_{len(self.state[bucket]) + 1}_{int(time.time() * 1000) % 100000}",
            "key": key,
            "masked": self._mask_key(key),
            "label": label or f"{provider.upper()} Key #{len(self.state[bucket]) + 1}",
            "status": "active",  # active | cooldown | invalid
            "cooldown_until": 0.0,
            "success_count": 0,
            "quota_hits": 0,
            "last_error": None,
            "last_used_at": None,
        }
        self.state[bucket].append(entry)
        return entry

    def _save(self) -> None:
        try:
            STORE_PATH.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def add_key(self, provider: str, key: str, label: str = "") -> Dict[str, Any]:
        key = key.strip()
        if not key:
            raise ValueError("API key cannot be empty")
        with self._lock:
            entry = self._ensure_key_entry(provider, key, label=label)
            entry["status"] = "active"
            entry["cooldown_until"] = 0.0
            entry["last_error"] = None
            self._save()
            return {k: v for k, v in entry.items() if k != "key"}

    def remove_key(self, provider: str, key_id: str) -> bool:
        bucket = "gemini_keys" if provider == "gemini" else "groq_keys"
        with self._lock:
            before = len(self.state[bucket])
            self.state[bucket] = [k for k in self.state[bucket] if k["id"] != key_id]
            self._save()
            return len(self.state[bucket]) < before

    def reset_cooldowns(self, provider: Optional[str] = None) -> None:
        with self._lock:
            buckets = ["gemini_keys", "groq_keys"] if not provider else [f"{provider}_keys"]
            for b in buckets:
                for item in self.state.get(b, []):
                    item["status"] = "active"
                    item["cooldown_until"] = 0.0
                    item["last_error"] = None
            self._save()

    def update_tts_routing(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            routing = self.state["tts_routing"]
            for field in ("mode", "default_model", "short_video_model", "long_video_model", "duration_threshold_sec"):
                if field in payload and payload[field] is not None:
                    routing[field] = payload[field]
            self._save()
            return dict(routing)

    def update_telegram(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if bot_token is not None:
                self.state["telegram"]["bot_token"] = bot_token.strip()
            if chat_id is not None:
                self.state["telegram"]["chat_id"] = str(chat_id).strip()
            self._save()
            return dict(self.state["telegram"])

    def resolve_gemini_tts_models(
        self,
        video_duration_sec: float = 0.0,
        requested_model: Optional[str] = None,
    ) -> List[str]:
        """
        Determines the ordered list of Gemini TTS models to try:
        - Default model is `gemini-2.5-flash-preview-tts` when fixed/explicitly chosen.
        - In `smart_duration` mode (or when requested_model == 'auto_duration'):
          * Video <= 180s (under 3 min) -> `gemini-3.8-flash-tts`
          * Video > 180s (over 3 min)  -> `gemini-3.8-flash-lite-tts`
        - Always appends `gemini-2.5-flash-preview-tts` and the alternate 3.8 model as fallbacks
          in case a specific model's per-day quota is exhausted across keys.
        """
        routing = self.state["tts_routing"]
        threshold = float(routing.get("duration_threshold_sec", 180))
        default_model = routing.get("default_model", "gemini-2.5-flash-preview-tts")
        short_model = routing.get("short_video_model", "gemini-3.8-flash-tts")
        long_model = routing.get("long_video_model", "gemini-3.8-flash-lite-tts")

        if requested_model and requested_model not in ("auto", "auto_duration", "smart_duration", ""):
            primary = requested_model
        elif routing.get("mode") == "smart_duration" or requested_model in ("auto", "auto_duration", "smart_duration"):
            if video_duration_sec > threshold:
                primary = long_model
            else:
                primary = short_model
        else:
            primary = default_model

        candidates = [primary, default_model, short_model, long_model]
        ordered: List[str] = []
        for m in candidates:
            if m and m not in ordered:
                ordered.append(m)
        return ordered

    def get_public_status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            for b in ("gemini_keys", "groq_keys"):
                for item in self.state[b]:
                    if item["status"] == "cooldown" and now >= item.get("cooldown_until", 0):
                        item["status"] = "active"
            return {
                "gemini_keys": [
                    {
                        **{k: v for k, v in item.items() if k != "key"},
                        "cooldown_remaining_sec": max(0, int(item.get("cooldown_until", 0) - now)),
                    }
                    for item in self.state["gemini_keys"]
                ],
                "groq_keys": [
                    {
                        **{k: v for k, v in item.items() if k != "key"},
                        "cooldown_remaining_sec": max(0, int(item.get("cooldown_until", 0) - now)),
                    }
                    for item in self.state["groq_keys"]
                ],
                "tts_routing": dict(self.state["tts_routing"]),
                "telegram": {
                    "configured": bool(self.state["telegram"].get("bot_token")),
                    "chat_id": self.state["telegram"].get("chat_id", ""),
                },
            }

    def execute_with_failover(
        self,
        provider: str,
        fn: Callable[[str], Any],
        cooldown_sec: int = 90,
    ) -> Any:
        """
        Executes `fn(api_key)` across all keys for `provider`.
        Automatically fails over to the next key on HTTP 429 / Quota / Rate Limit / 5xx errors.
        """
        bucket = "gemini_keys" if provider == "gemini" else "groq_keys"
        now = time.time()
        with self._lock:
            entries = list(self.state.get(bucket, []))
            if not entries:
                raise RuntimeError(f"No API keys configured for provider '{provider}'. Add one in the API Key Pool.")
            # Refresh expired cooldowns
            for item in entries:
                if item["status"] == "cooldown" and now >= item.get("cooldown_until", 0):
                    item["status"] = "active"

        # Sort active keys first (least recently used or lowest quota hits first), then cooldown keys as last resort
        active_entries = [e for e in entries if e["status"] == "active"]
        cooldown_entries = sorted(
            [e for e in entries if e["status"] != "active"],
            key=lambda x: x.get("cooldown_until", 0),
        )
        ordered_entries = active_entries + cooldown_entries

        errors: List[str] = []
        for entry in ordered_entries:
            try:
                result = fn(entry["key"])
                with self._lock:
                    entry["status"] = "active"
                    entry["cooldown_until"] = 0.0
                    entry["success_count"] = entry.get("success_count", 0) + 1
                    entry["last_used_at"] = int(time.time())
                    self._save()
                return result
            except Exception as exc:
                err_str = str(exc)
                errors.append(f"[{entry['label']} ({entry['masked']})]: {err_str[:160]}")
                is_quota = any(
                    tok in err_str.lower()
                    for tok in ("429", "quota", "rate_limit", "resource_exhausted", "too many requests")
                )
                with self._lock:
                    if is_quota:
                        entry["status"] = "cooldown"
                        entry["quota_hits"] = entry.get("quota_hits", 0) + 1
                        entry["cooldown_until"] = time.time() + cooldown_sec
                    entry["last_error"] = err_str[:240]
                    self._save()
                continue

        raise RuntimeError(
            f"All {len(ordered_entries)} {provider.upper()} keys failed or exhausted quota: " + " | ".join(errors)
        )


key_pool = MultiKeyPoolManager()
