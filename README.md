# ⚡ AlphaFlow AI Studio — Zero-Cost Video Recap, Dubbing & Subtitle Engine

AlphaFlow is a complete **$0-upfront-cost** AI Video Dubbing, Subtitle, and Movie/Anime Recap platform that reverse-engineers and upgrades every capability of `https://teamalpha.app/` (`https://recap.teamalpha.app/`).

---

## 🌟 Key Upgrades Over TeamAlpha

1. **Multi-Key Google AI Studio & Groq Failover Pool ([`key_pool.py`](backend/key_pool.py))**:
   - Supports unlimited Google AI Studio keys and Groq keys.
   - Add backup keys in the UI (`🔑 API Key Pool & Failover`) or `.env`.
   - When any key hits a rate limit or exhausts its daily quota (`HTTP 429 RESOURCE_EXHAUSTED`), AlphaFlow places that key on cooldown and **instantly fails over to the next healthy key** mid-render.

2. **Smart Duration-Based Gemini TTS Router ([`tts_engine.py`](backend/tts_engine.py))**:
   - **Default Model**: `gemini-2.5-flash-preview-tts`
   - **Auto Smart Duration Mode (Default Active)**:
     - Videos **$\le 3\text{ minutes}$ ($\le 180\text{s}$)** $\rightarrow$ Automatically routes to **`gemini-3.8-flash-tts`**
     - Videos **$> 3\text{ minutes}$ ($> 180\text{s}$)** $\rightarrow$ Automatically routes to **`gemini-3.8-flash-lite-tts`**
     - Automatic fallback to `gemini-2.5-flash-preview-tts` (and ultimately `edge-tts` `my-MM-ThihaNeural`) if all keys for a specific model tier are exhausted.
   - **Pure TTS Speech Delivery Invariant**: Sends strictly verbatim narration text without style prefixes so the TTS model never reads prompt instructions aloud.

3. **All 45 Voices Unlocked for $0 ([`voices.py`](backend/voices.py))**:
   - **15 Microsoft Neural Voices (`edge-tts`)**: Includes `my-MM-ThihaNeural`, `my-MM-NilarNeural`, and the 13 `*MultilingualNeural` voices that natively speak Myanmar Unicode script.
   - **30 Google Gemini Voices**: All 30 Gemini voices (`Charon`, `Kore`, `Zephyr`, `Puck`, `Fenrir`, `Leda`, `Orus`, `Aoede`, etc.) unlocked at $0 cost via your Multi-Key Pool.

4. **Free Unlimited Cloud Storage via Telegram Bot (`@hyugoustoragebot`)**:
   - Easily configure your Telegram Bot Token in `.env` or the Studio UI.
   - Send `/start` to your bot on Telegram once, and AlphaFlow will automatically detect your Chat ID and push every finished MP4 render to your Telegram chat/channel for permanent $0 cloud storage.

---

## 🚀 Quick Start (Mode 1: Local FastAPI + FFmpeg Studio)

```powershell
cd c:\Users\lyan1\Documents\antigravity\lively-borg\alphaflow
python run_studio.py
```
Then open **http://127.0.0.1:8090** in your browser.

---

## ☁️ Quick Start (Mode 2: Cloudflare Edge Worker + GitHub Actions Serverless Pipeline)

### Deploy Edge API & Multi-Key Router to Cloudflare Workers:
```powershell
cd c:\Users\lyan1\Documents\antigravity\lively-borg\alphaflow\cloudflare
npx wrangler deploy
```

### Run Serverless CLI / GitHub Actions Worker Directly:
```powershell
cd c:\Users\lyan1\Documents\antigravity\lively-borg\alphaflow
python cli_render.py --job-id demo_01 --url "https://www.youtube.com/watch?v=..." --mode recap --voice Charon --gemini-tts-model auto_duration
```

---

## ⚡ Quick Start (Mode 3: Vercel Serverless Studio)

Deploy the studio UI, key pool failover API, voice catalog, and audio synthesis endpoints to Vercel with zero server maintenance:

```powershell
cd c:\Users\lyan1\Documents\antigravity\lively-borg\alphaflow
npx vercel --prod
```
Set environment variables on Vercel (`GROQ_API_KEYS`, `GEMINI_API_KEYS`, `TELEGRAM_BOT_TOKEN`) in the Vercel Dashboard or CLI.
