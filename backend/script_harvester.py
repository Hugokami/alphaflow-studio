# -*- coding: utf-8 -*-
"""
LTstudio Zero-Cost Viral Script & Synopsis Harvester
Powered by Agent-Reach principles: extracts viral plots, anime synopses, and recap outlines
from YouTube, Wikipedia, and Reddit discussions with zero official API fees.
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _clean_html_tags(text: str) -> str:
    """Removes HTML tags and unescapes HTML entities."""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = html.unescape(clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def harvest_from_youtube(query: str, limit: int = 3) -> List[Dict[str, Any]]:
    """Extracts top video commentary outlines and plot synopses via yt_dlp without downloading video."""
    results: List[Dict[str, Any]] = []
    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 5,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f"ytsearch{limit}:{query} recap synopsis"
            res = ydl.extract_info(search_query, download=False)
            entries = res.get("entries") or []
            for entry in entries:
                if not entry:
                    continue
                title = entry.get("title", "")
                desc = entry.get("description", "")
                url = entry.get("webpage_url") or entry.get("url") or ""
                chapters = entry.get("chapters") or []

                chapter_texts = []
                for c in chapters[:6]:
                    title_c = c.get("title")
                    start_t = c.get("start_time", 0)
                    if title_c:
                        chapter_texts.append(f"[{int(start_t)}s] {title_c}")

                snippet = desc[:600] if desc else ""
                if chapter_texts:
                    snippet += "\nKey Beats:\n" + "\n".join(chapter_texts)

                results.append({
                    "platform": "YouTube",
                    "title": title,
                    "url": url,
                    "content": snippet.strip(),
                    "duration": entry.get("duration", 0),
                })
    except Exception as e:
        print(f"[*] [Harvester] YouTube extraction notice: {e}")

    return results


def harvest_from_wikipedia(query: str, limit: int = 2) -> List[Dict[str, Any]]:
    """Extracts verified story synopses and character backgrounds via Wikipedia's Open REST API."""
    results: List[Dict[str, Any]] = []
    clean_q = re.sub(r"(?i)\b(recap|summary|full\s+movie|episode|review|synopsis)\b", "", query).strip()
    if not clean_q:
        clean_q = query

    try:
        w_url = (
            f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch="
            f"{urllib.parse.quote(clean_q)}&format=json&utf8=1"
        )
        req = urllib.request.Request(
            w_url,
            headers={"User-Agent": "LTstudio/2.0 (https://teamalpha.app; contact@ltstudio.local)"}
        )
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            search_items = data.get("query", {}).get("search", [])

        for item in search_items[:limit]:
            page_title = item.get("title", "")
            snippet = _clean_html_tags(item.get("snippet", ""))

            # Fetch page intro extract if possible
            extract_url = (
                f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1"
                f"&explaintext=1&titles={urllib.parse.quote(page_title)}&format=json&utf8=1"
            )
            req_ex = urllib.request.Request(
                extract_url,
                headers={"User-Agent": "LTstudio/2.0"}
            )
            try:
                with urllib.request.urlopen(req_ex, timeout=5.0) as resp_ex:
                    ex_data = json.loads(resp_ex.read().decode("utf-8"))
                    pages = ex_data.get("query", {}).get("pages", {})
                    for pid, pdata in pages.items():
                        full_extract = pdata.get("extract", "")
                        if full_extract:
                            snippet = full_extract[:800]
            except Exception:
                pass

            results.append({
                "platform": "Wikipedia",
                "title": page_title,
                "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(page_title.replace(' ', '_'))}",
                "content": snippet.strip(),
            })
    except Exception as e:
        print(f"[*] [Harvester] Wikipedia extraction notice: {e}")

    return results


def harvest_trending_script(
    query: str,
    platforms: Optional[List[str]] = None,
    limit: int = 5
) -> Dict[str, Any]:
    """
    Primary ingestion pipeline:
    1. Tries `agent-reach` CLI if installed on the host.
    2. Otherwise, executes embedded zero-fee multi-source harvesters (YouTube + Wikipedia).
    3. Returns a synthesized, structured plot outline buffer.
    """
    clean_query = query.strip()
    if not clean_query:
        return {"success": False, "query": "", "combined_outline": "", "sources": [], "sources_count": 0}

    # Step 1: Check for agent-reach CLI binary
    agent_reach_bin = shutil.which("agent-reach")
    if agent_reach_bin:
        try:
            platform_flag = ",".join(platforms or ["youtube", "reddit"])
            cmd = [
                agent_reach_bin, "search", clean_query,
                "--platform", platform_flag,
                "--limit", str(limit),
                "--json"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if res.returncode == 0 and res.stdout.strip():
                raw_data = json.loads(res.stdout)
                items = raw_data.get("results") or []
                sources = []
                outlines = []
                for it in items:
                    t = it.get("title", "Source")
                    c = it.get("content") or it.get("transcript") or ""
                    u = it.get("url", "")
                    sources.append({"platform": it.get("platform", "Agent-Reach"), "title": t, "url": u, "content": c})
                    outlines.append(f"### [Agent-Reach] {t}\n{c}")
                if outlines:
                    return {
                        "success": True,
                        "query": clean_query,
                        "harvester": "agent-reach-cli",
                        "combined_outline": "\n\n".join(outlines),
                        "sources": sources,
                        "sources_count": len(sources),
                    }
        except Exception as ar_err:
            print(f"[*] [Harvester] agent-reach CLI execution fallback: {ar_err}")

    # Step 2: Embedded zero-cost multi-source fallback
    all_sources: List[Dict[str, Any]] = []

    # A. YouTube
    yt_items = harvest_from_youtube(clean_query, limit=3)
    all_sources.extend(yt_items)

    # B. Wikipedia
    wiki_items = harvest_from_wikipedia(clean_query, limit=2)
    all_sources.extend(wiki_items)

    if not all_sources:
        # Fallback query template if offline or blocked
        synthetic_outline = (
            f"### Synopsis Outline for: {clean_query}\n"
            f"1. Hook: Introduce the central conflict, protagonist status, and initial stakes of {clean_query}.\n"
            f"2. Rising Action: Uncover the key antagonist, escalating obstacles, and dramatic twists.\n"
            f"3. Climax & Resolution: The decisive battle or revelation that reshapes the outcome."
        )
        return {
            "success": True,
            "query": clean_query,
            "harvester": "synthetic_template",
            "combined_outline": synthetic_outline,
            "sources": [],
            "sources_count": 0,
        }

    # Format synthesized markdown outline buffer
    outline_blocks = []
    for s in all_sources:
        outline_blocks.append(f"### Source [{s['platform']}]: {s['title']}\n{s['content']}")

    combined = "\n\n".join(outline_blocks)
    return {
        "success": True,
        "query": clean_query,
        "harvester": "embedded_agent_reach",
        "combined_outline": combined,
        "sources": all_sources,
        "sources_count": len(all_sources),
    }


def synthesize_recap_script(
    harvest_result: Dict[str, Any],
    duration_sec: float = 60.0,
    target_language: str = "Burmese (Myanmar Language)",
    voice: str = "Charon",
    script_style: str = "Engaging Story",
    voice_tone: str = "Energetic",
) -> Dict[str, Any]:
    """
    Transforms the harvested plot outline into a production-ready recap narration script
    with syllable-timed subtitle lines using Gemini LLMs with multi-key failover.
    """
    from backend.key_pool import key_pool

    outline = harvest_result.get("combined_outline") or harvest_result.get("query") or "Viral video recap"
    query = harvest_result.get("query") or "Trending Story"

    system_prompt = f"""You are the master viral story recap director for LTstudio.
Your goal is to transform the provided plot synopsis and commentary outline into an electrifying, viral, suspenseful video recap script in {target_language}.
Audience: High-retention social media audiences (TikTok, YouTube Shorts, Reels).

CRITICAL SCRIPTING RULES:
1. Pacing & Hook: The first sentence MUST be an irresistible curiosity hook that stops viewers from scrolling.
2. Narrative Progression: Deliver a coherent chronological storyline highlighting twists, character choices, and high-stakes climaxes.
3. Natural Human Prosody: Use authentic, dramatic spoken {target_language}. Ban robotic literal machine translations.
4. Duration Match: Aim for approximately {int(duration_sec)} seconds of spoken narration (around {int(duration_sec * 2.2)} words/syllables).
5. Output Format: Return strictly a valid JSON object matching the schema below.

JSON SCHEMA:
{{
  "viral_title": "Curiosity-inducing 5-10 word title",
  "hook_line": "Opening 1-2 sentence hook",
  "full_narration": "Complete narration text divided with punctuation (။ for Burmese)",
  "captions": [
    {{"start": 0.0, "end": 4.5, "text": "Opening hook sentence..."}},
    {{"start": 4.5, "end": 9.0, "text": "Next dramatic beat..."}}
  ]
}}
"""

    user_prompt = f"""Topic / Search: {query}
Target Duration: {duration_sec:.1f} seconds
Script Style: {script_style} | Tone: {voice_tone}

HARVESTED PLOT OUTLINE & COMMENTS:
{outline[:2500]}
"""

    def _call_gemini(api_key: str) -> Dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        txt = body["candidates"][0]["content"]["parts"][0]["text"].strip()
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
        return json.loads(txt)

    # Try Gemini Multi-Key Pool
    try:
        if key_pool.has_active_keys("gemini"):
            res = key_pool.execute_with_failover("gemini", _call_gemini)
            if res.get("full_narration") and res.get("captions"):
                return res
    except Exception as gem_err:
        print(f"[*] [Harvester] LLM synthesis fallback notice: {gem_err}")

    # Robust local fallback synthesis if LLMs are unavailable
    title = f"{query} ဇာတ်လမ်းအကျဉ်း"
    hook = f"{query} ရဲ့ အမိုက်စား ဇာတ်လမ်းကို လူတိုင်းသိအောင် အစအဆုံး အကျဉ်းချုပ် တင်ဆက်ပေးလိုက်ပါတယ်။"
    narration = (
        f"{hook} ဒီဇာတ်လမ်းထဲမှာ အဓိကဇာတ်ကောင်ဟာ မထင်မှတ်ထားတဲ့ အခက်အခဲတွေနဲ့ ရင်ဆိုင်ရပြီး "
        f"နောက်ဆုံးမှာတော့ အံ့အားသင့်စရာ အလှည့်အပြောင်းတွေနဲ့ အဆုံးသတ်သွားခဲ့ပါတယ်။ "
        f"တကယ်ကို ကြည့်ရတာ တန်ဖိုးရှိတဲ့ ဇာတ်ကားကောင်းတစ်ခု ဖြစ်ပါတယ်။"
    )
    clauses = [c.strip() for c in narration.replace("။", "။\n").splitlines() if c.strip()]
    slot = max(2.5, duration_sec / max(1, len(clauses)))
    captions = [
        {"start": round(i * slot, 2), "end": round(min(duration_sec, (i + 1) * slot), 2), "text": c}
        for i, c in enumerate(clauses)
    ]
    return {
        "viral_title": title[:30],
        "hook_line": hook,
        "full_narration": narration,
        "captions": captions,
    }
