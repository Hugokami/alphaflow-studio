import asyncio
import base64
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

from .key_pool import key_pool, DATA_DIR
from .tts_engine import synthesize_voice

JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

BURMESE_SYLLABLE_RE = re.compile(r"([က-ဪa-zA-Z0-9]+[်္ျြွှ]*)")


def get_ffmpeg_exe() -> str:
    """Returns path to system ffmpeg binary or bundled imageio-ffmpeg static executable."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return "ffmpeg"


def count_burmese_syllables(text: str) -> int:
    return max(1, len(BURMESE_SYLLABLE_RE.findall(text or "")))


def probe_media_info(video_path: Path) -> Dict[str, Any]:
    """Extracts duration, width, height, and fps via ffprobe or ffmpeg -i fallback."""
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin:
        cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height,codec_type",
            "-of",
            "json",
            str(video_path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            data = json.loads(res.stdout or "{}")
            duration = float(data.get("format", {}).get("duration") or 0.0)
            width, height = 1280, 720
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" and s.get("width") and s.get("height"):
                    width = int(s["width"])
                    height = int(s["height"])
                    break
            if duration > 0:
                return {"duration": duration, "width": width, "height": height}
        except Exception:
            pass

    # Fallback to ffmpeg -i output parsing
    ffmpeg_bin = get_ffmpeg_exe()
    try:
        cmd = [ffmpeg_bin, "-i", str(video_path)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        out = (res.stderr or "") + " " + (res.stdout or "")
        dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
        duration = 0.0
        if dur_match:
            duration = float(dur_match.group(1)) * 3600 + float(dur_match.group(2)) * 60 + float(dur_match.group(3))
        res_match = re.search(r"(\d{3,5})x(\d{3,5})", out)
        width, height = 1280, 720
        if res_match:
            width = int(res_match.group(1))
            height = int(res_match.group(2))
        return {"duration": duration, "width": width, "height": height}
    except Exception:
        return {"duration": 30.0, "width": 1080, "height": 1920}


def extract_youtube_id(url: str) -> Optional[str]:
    """Extracts 11-char YouTube video ID from various URL formats."""
    patterns = [
        r"(?:v=|\/vi\/|youtu\.be\/|\/v\/|\/e\/|\/embed\/|\/shorts\/)([\w-]{11})",
        r"[\?&]v=([\w-]{11})",
        r"^([\w-]{11})$",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _download_youtube_cloud_fallback(vid: str, dest_dir: Path, ffmpeg_bin: str) -> Dict[str, Any]:
    """
    Cloud Serverless Datacenter IP Handler for YouTube.
    When serverless cloud datacenter IPs trigger YouTube bot verification ('Sign in to confirm you're not a bot'),
    we fetch title & metadata via oEmbed and timed transcript via timedtext API, but DO NOT synthesize fake static frames.
    """
    title = f"YouTube Video ({vid})"
    duration = 60.0

    # 1. Fetch title and metadata from oEmbed
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            title = data.get("title") or title
    except Exception as oe_err:
        print(f"oEmbed fetch warning: {oe_err}")

    # 2. Fetch transcript
    snippets = []
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        try:
            api = YouTubeTranscriptApi()
            snippets = api.fetch(vid)
        except Exception:
            snippets = YouTubeTranscriptApi.get_transcript(vid)
    except Exception as tr_err:
        print(f"YouTube transcript fetch warning: {tr_err}")

    segments = []
    full_text_parts = []
    if snippets:
        for s in snippets:
            t = (s.get("text") if isinstance(s, dict) else getattr(s, "text", "")).strip()
            start = float(s.get("start") if isinstance(s, dict) else getattr(s, "start", 0.0))
            dur = float(s.get("duration") if isinstance(s, dict) else getattr(s, "duration", 0.0))
            if t:
                segments.append({"start": round(start, 2), "end": round(start + dur, 2), "text": t})
                full_text_parts.append(t)

    if segments:
        full_text = " ".join(full_text_parts)
        duration = round(segments[-1]["end"], 2)
    else:
        try:
            pg_req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(pg_req, timeout=5) as pg_resp:
                html = pg_resp.read().decode("utf-8", errors="ignore")
                m_dur = re.search(r'approxDurationMs\\":\\"(\d+)\\"', html) or re.search(r'"approxDurationMs":"(\d+)"', html)
                if m_dur:
                    duration = round(float(m_dur.group(1)) / 1000.0, 1)
        except Exception:
            pass
        full_text = f"{title}. Educational video summary and analysis."
        segments = [{"start": 0.0, "end": 15.0, "text": title}]

    transcript_payload = {
        "text": full_text,
        "segments": segments,
        "language": "en",
    }
    (dest_dir / "transcript.json").write_text(json.dumps(transcript_payload, ensure_ascii=False), encoding="utf-8")

    out_mp4 = dest_dir / "source.mp4"
    if not out_mp4.exists():
        raise RuntimeError(
            "YouTube blocked video stream download on this cloud serverless IP ('Sign in to confirm you’re not a bot'). "
            "Please upload the video file directly (MP4/WebM/MOV) or run LTstudio locally via `python start_studio.py` "
            "where your residential IP downloads YouTube streams with zero restrictions."
        )

    return {
        "video_path": out_mp4,
        "title": title,
        "duration": duration,
        "width": 1280,
        "height": 720,
        "has_transcript": True,
    }


def download_source_video(url: str, dest_dir: Path) -> Dict[str, Any]:
    """Downloads genuine moving video or extracts stream from URL using yt-dlp with anti-bot bypass."""
    import yt_dlp

    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(dest_dir / "source.%(ext)s")
    ffmpeg_bin = get_ffmpeg_exe()
    vid_id = extract_youtube_id(url)

    # Primary: Fast pre-muxed mp4 (format 18 or 360p/720p) with mweb/android client bypass
    ydl_opts = {
        "outtmpl": out_tmpl,
        "format": "18/best[ext=mp4][height<=720]/best[height<=720]/best",
        "no_playlist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_bin,
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb", "android"]
            }
        },
    }

    duration = 0.0
    title = "Source Video"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or title
            duration = float(info.get("duration") or 0.0)
    except Exception as e:
        # Fallback 1: Try alternate player clients (android, ios, web)
        try:
            fallback_opts = {
                "outtmpl": str(dest_dir / "source.%(ext)s"),
                "format": "18/best[height<=720]/bestaudio/best",
                "no_playlist": True,
                "quiet": True,
                "no_warnings": True,
                "ffmpeg_location": ffmpeg_bin,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "ios", "web"]
                    }
                },
            }
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title") or title
                duration = float(info.get("duration") or 0.0)
        except Exception as e2:
            if vid_id:
                return _download_youtube_cloud_fallback(vid_id, dest_dir, ffmpeg_bin)
            raise RuntimeError(f"Video download error: {str(e)[:160]} | fallback: {str(e2)[:160]}")

    candidates = [p for p in dest_dir.glob("source.*") if p.suffix.lower() not in (".json", ".part", ".ytdl", ".txt")]
    if not candidates:
        if vid_id:
            return _download_youtube_cloud_fallback(vid_id, dest_dir, ffmpeg_bin)
        raise RuntimeError("No media file found after yt-dlp download.")
    video_path = candidates[0]

    meta = probe_media_info(video_path)
    if duration > 0 and (meta.get("duration") or 0) <= 0:
        meta["duration"] = duration
    return {"video_path": video_path, "title": title, **meta}


def transcribe_with_groq_whisper(video_path: Path, work_dir: Path) -> Dict[str, Any]:
    """Extracts 16kHz mono audio and transcribes with Groq whisper-large-v3-turbo using key failover."""
    audio_mp3 = work_dir / "extracted_speech.mp3"
    ffmpeg_bin = get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-b:a",
            "48k",
            str(audio_mp3),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    audio_bytes = audio_mp3.read_bytes()
    boundary = f"----LTstudioBoundary{uuid.uuid4().hex}"

    def _call_groq_stt(api_key: str) -> Dict[str, Any]:
        body = bytearray()
        # file field
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(b'Content-Disposition: form-data; name="file"; filename="speech.mp3"\r\n')
        body.extend(b"Content-Type: audio/mpeg\r\n\r\n")
        body.extend(audio_bytes)
        body.extend(b"\r\n")
        # model field
        for k, v in [("model", "whisper-large-v3-turbo"), ("response_format", "verbose_json")]:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        body.extend(f"--{boundary}--\r\n".encode())

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=bytes(body),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "LTstudioStudio/2.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _call_gemini_stt(api_key: str) -> Dict[str, Any]:
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        prompt = (
            "Listen to this audio carefully and transcribe the spoken words verbatim. "
            "Return a valid JSON object matching this schema:\n"
            '{"text": "Full transcribed speech text here", "segments": [{"start": 0.0, "end": 2.5, "text": "Segment text"}]}\n'
            "If the audio has no speech or only background music/silence, return: "
            '{"text": "", "segments": []}'
        )
        last_err = None
        for m in ("gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest"):
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
                payload = {
                    "contents": [
                        {
                            "parts": [
                                {"inlineData": {"mimeType": "audio/mp3", "data": audio_b64}},
                                {"text": prompt},
                            ]
                        }
                    ],
                    "generationConfig": {
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=25) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                txt = body["candidates"][0]["content"]["parts"][0]["text"].strip()
                if txt.startswith("```"):
                    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
                return json.loads(txt)
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        return {"text": "", "segments": []}

    if key_pool.has_active_keys("groq"):
        try:
            return key_pool.execute_with_failover("groq", _call_groq_stt)
        except Exception as groq_err:
            print("Groq transcription notice, attempting Gemini fallback:", groq_err)

    try:
        return key_pool.execute_with_failover("gemini", _call_gemini_stt)
    except Exception as gem_stt_err:
        print("Gemini STT fallback notice (falling back to visual narrative):", gem_stt_err)
        return {"text": "", "segments": []}


def generate_recap_or_dub_script(
    transcript_data: Dict[str, Any],
    video_duration_sec: float,
    processing_mode: str = "recap",
    target_language: str = "Burmese (Myanmar Language)",
    script_style: str = "Engaging Story",
    voice_tone: str = "Energetic",
    recap_length: str = "full_length",
) -> Dict[str, Any]:
    """
    Generates a syllable-constrained, timestamp-aligned script in Burmese (or target language)
    using Gemini 2.5 Flash (primary multi-key pool) with automatic Groq Llama-3.3-70B failover.
    """
    raw_text = transcript_data.get("text", "").strip()
    segments = transcript_data.get("segments", [])
    target_syllables = max(35, int(video_duration_sec * 3.3))

    if not raw_text:
        raw_text = "(Visual / low-dialogue video clip — craft an engaging dramatic commentary fitting the duration.)"

    if processing_mode == "recap":
        if recap_length == "full_length":
            mode_instruction = (
                f"Write a comprehensive FULL-LENGTH movie/anime story recap covering 100% of the video duration. "
                f"DO NOT cut, skip, or truncate the storyline. The narration MUST continuously and engagingly "
                f"narrate the entire visual and plot progression from 0.0s to {video_duration_sec:.1f}s without gaps or sudden stops."
            )
            target_syllables = max(45, int(video_duration_sec * 3.4))
        elif recap_length == "condensed_hook":
            mode_instruction = "Write a fast-paced 60s viral hook teaser recap"
            target_syllables = max(35, min(200, int(video_duration_sec * 3.0)))
        else:
            mode_instruction = "Write a gripping, viral movie/anime recap narration evenly pacing key turning points"
            target_syllables = max(35, int(video_duration_sec * 3.3))
    elif processing_mode == "dubbing":
        mode_instruction = "Translate and adapt the dialogue for natural lip-synced dubbing"
    else:
        mode_instruction = "Translate the transcript into accurate, punchy subtitle lines"

    system_prompt = f"""You are an elite {target_language} video narrator and scriptwriter (TBH Labs / Channel Myanmar urban conversational cadence).
Task: {mode_instruction}.
Style: {script_style} | Tone: {voice_tone}.
Video Duration: {video_duration_sec:.1f} seconds (Target approximately {target_syllables} spoken syllables so narration fits the video length naturally without rushing or trailing silence).

CRITICAL RULES:
1. Output 100% pure Myanmar Unicode (never Zawgyi) when target language is Burmese.
2. Spell out numbers in spoken words and use natural conversational endings (ပါတယ်၊ ခဲ့တယ်၊ ပေါ့နော်) instead of stiff literary bookish Burmese (သည်၊ ၏).
3. Return ONLY valid JSON with this exact schema:
{{
  "viral_title": "Snappy <25 char title for thumbnail",
  "hook_line": "1-sentence high-retention opening hook",
  "full_narration": "Complete verbatim narration script (NO speaker labels, NO stage directions, ONLY words to be spoken aloud)",
  "captions": [
    {{"start": 0.0, "end": 4.0, "text": "Short caption line"}}
  ]
}}"""

    user_prompt = f"Source Transcript ({len(segments)} segments, {video_duration_sec:.1f}s):\n{raw_text[:12000]}"

    def _call_groq_llm(api_key: str) -> Dict[str, Any]:
        url = "https://api.groq.com/openai/v1/chat/completions"
        last_err = None
        for groq_model in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"):
            try:
                payload = {
                    "model": groq_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "LTstudioStudio/2.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                content_str = body["choices"][0]["message"]["content"].strip()
                # Strip markdown json fences if present
                if content_str.startswith("```"):
                    content_str = re.sub(r"^```(?:json)?\s*|\s*```$", "", content_str, flags=re.S).strip()
                return json.loads(content_str)
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"Groq LLM models failed: {last_err}")

    def _call_gemini_llm(api_key: str) -> Dict[str, Any]:
        last_err = None
        for gem_model in ("gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest"):
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{gem_model}:generateContent?key={api_key}"
                payload = {
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"parts": [{"text": user_prompt}]}],
                    "generationConfig": {
                        "temperature": 0.4,
                        "responseMimeType": "application/json",
                    },
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                txt = body["candidates"][0]["content"]["parts"][0]["text"].strip()
                if txt.startswith("```"):
                    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
                return json.loads(txt)
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"Gemini LLM models failed: {last_err}")

    try:
        result = key_pool.execute_with_failover("groq", _call_groq_llm)
    except Exception:
        result = key_pool.execute_with_failover("gemini", _call_gemini_llm)

    if not result.get("full_narration"):
        result["full_narration"] = raw_text
    if not result.get("captions"):
        # Auto-segment full_narration into timed captions across video_duration_sec
        clauses = [c.strip() for c in re.split(r"[။\n]+", result["full_narration"]) if c.strip()]
        if not clauses:
            clauses = [result["full_narration"]]
        slot = max(2.0, video_duration_sec / len(clauses))
        result["captions"] = [
            {"start": round(i * slot, 2), "end": round(min(video_duration_sec, (i + 1) * slot), 2), "text": c}
            for i, c in enumerate(clauses)
        ]
    return result


def write_srt_file(captions: List[Dict[str, Any]], srt_path: Path) -> Path:
    def _fmt_ts(sec: float) -> str:
        hrs = int(sec // 3600)
        mins = int((sec % 3600) // 60)
        secs = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

    lines = []
    for idx, cap in enumerate(captions, 1):
        start = float(cap.get("start", 0.0))
        end = max(start + 0.8, float(cap.get("end", start + 2.5)))
        text = str(cap.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"{idx}\n{_fmt_ts(start)} --> {_fmt_ts(end)}\n{text}\n")
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return srt_path


def stitch_intro_outro(
    main_video: Path,
    intro_video: Optional[Path],
    outro_video: Optional[Path],
    out_video: Path,
    aspect_ratio: str = "original",
) -> Path:
    """
    Concatenates optional channel intro and outro videos with the main recap video,
    normalizing resolution, SAR, framerate, and audio channels in a single FFmpeg pass.
    """
    ffmpeg_bin = get_ffmpeg_exe()
    streams = []
    if intro_video and intro_video.exists():
        streams.append(intro_video)
    streams.append(main_video)
    if outro_video and outro_video.exists():
        streams.append(outro_video)

    if len(streams) == 1:
        if main_video != out_video:
            shutil.copyfile(str(main_video), str(out_video))
        return out_video

    if aspect_ratio == "9:16":
        tw, th = 1080, 1920
    elif aspect_ratio == "3:4":
        tw, th = 1080, 1440
    elif aspect_ratio == "1:1":
        tw, th = 1080, 1080
    else:
        tw, th = 1280, 720

    inputs_cmd = []
    filter_parts = []
    concat_inputs = []

    for idx, s_path in enumerate(streams):
        inputs_cmd.extend(["-i", str(s_path)])
        filter_parts.append(
            f"[{idx}:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30[v{idx}];"
            f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{idx}]"
        )
        concat_inputs.append(f"[v{idx}][a{idx}]")

    joined_parts = ";".join(filter_parts)
    joined_inputs = "".join(concat_inputs)
    concat_filter = f"{joined_parts};{joined_inputs}concat=n={len(streams)}:v=1:a=1[outv][outa]"

    cmd = [
        ffmpeg_bin,
        "-y",
        *inputs_cmd,
        "-filter_complex",
        concat_filter,
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out_video),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode("utf-8", errors="ignore") if e.stderr else str(e)
        print("Stitch intro/outro error notice:", err_msg[:200])
        if main_video != out_video:
            shutil.copyfile(str(main_video), str(out_video))
    return out_video


def generate_thumbnail_slices(video_path: Path, out_dir: Path, duration: float = 60.0) -> List[Dict[str, Any]]:
    """
    Slices 3 candidate high-engagement thumbnails at 15%, 50%, and 85% duration using FFmpeg.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = get_ffmpeg_exe()
    timestamps = [
        max(1.0, duration * 0.15),
        max(2.0, duration * 0.50),
        max(3.0, duration * 0.85),
    ]
    results = []
    for i, ts in enumerate(timestamps):
        out_jpg = out_dir / f"thumb_{i+1}.jpg"
        try:
            cmd = [
                ffmpeg_bin,
                "-y",
                "-ss",
                f"{ts:.2f}",
                "-i",
                str(video_path),
                "-vframes",
                "1",
                "-q:v",
                "2",
                str(out_jpg),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if out_jpg.exists():
                results.append({
                    "index": i + 1,
                    "timestamp": round(ts, 2),
                    "filename": out_jpg.name,
                    "size": out_jpg.stat().st_size
                })
        except Exception as e:
            print(f"Thumbnail slice notice for ts {ts}:", e)
    return results


def generate_viral_social_package(
    script: Dict[str, Any],
    duration: float = 60.0,
    title: str = "Video Recap",
    language: str = "Burmese",
) -> Dict[str, Any]:
    """
    Generates high-CTR viral titles, conversational Burmese TikTok/Reels captions, and trending hashtags.
    """
    recap_narration = script.get("full_narration", "")
    captions = script.get("captions", [])
    context_text = recap_narration[:1500] if recap_narration else " ".join([c.get("text", "") for c in captions[:25]])

    prompt = f"""
You are an elite viral social media strategist specializing in movie, anime, and story recaps for TikTok, Facebook Reels, and YouTube Shorts.
The recap language is {language}.
Context / Narration:
"{context_text}"

Generate a viral social growth package formatted strictly as JSON with:
1. "titles": Exactly 3 curiosity-gap, high-CTR viral titles in {language} with dramatic hooks (e.g. "သူငယ်ချင်းတွေကို ကယ်ဖို့အတွက်...", "အဆုံးသတ်မှာ လူတိုင်းအံ့သြသွားစေမယ့်...").
2. "caption": A high-retention TikTok/Reels caption in conversational, natural {language} with engaging emojis and a strong call-to-action (e.g. asking a discussion question or saying "Follow for Part 2!").
3. "hashtags": An array of 8-12 trending hashtags (mix of English and Burmese, e.g. #movierecap, #burmeserecap, #foryou, #fyp, #tiktokmyanmar, #animerecap).

Return ONLY valid JSON matching this schema:
{{
  "titles": ["Title 1", "Title 2", "Title 3"],
  "caption": "Viral caption text with emojis...",
  "hashtags": ["#movierecap", "#burmeserecap", "#fyp", "#foryou", "#tiktokmyanmar"]
}}
"""
    def _call_gemini_viral(api_key: str) -> Dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "responseMimeType": "application/json"}
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        return json.loads(text)

    try:
        data = key_pool.execute_with_failover("gemini", _call_gemini_viral)
        if "titles" in data and "caption" in data and "hashtags" in data:
            return data
    except Exception as e:
        print("Gemini viral package generation notice:", e)

    clean_title = script.get("viral_title") or title or "ဒီဇာတ်ကားကတော့ မဖြစ်မနေ ကြည့်သင့်တယ်"
    return {
        "titles": [
            f"🔥 {clean_title} — အဆုံးသတ်မှာ လူတိုင်းကြက်သီးထသွားစေမယ့် အလှည့်အပြောင်း",
            f"😱 တစ်ခါမှ မမြင်ဖူးသေးတဲ့ အမိုက်စား ဇာတ်လမ်းအစအဆုံး ({clean_title})",
            f"⚡ ဒီဇာတ်ကားကို မကြည့်ရသေးရင် သေချာပေါက် နောင်တရလိမ့်မယ်"
        ],
        "caption": f"ဒီဇာတ်လမ်းလေးကတော့ တကယ်ကို ရင်ခုန်စိတ်လှုပ်ရှားစရာ ကောင်းလွန်းပါတယ်။ အဆုံးထိ ကြည့်ပေးကြပါဦးနော် ✨\n\nနောက်ထပ်အပိုင်းတွေ ဆက်ကြည့်ချင်ရင် Like & Follow လုပ်ထားပေးကြပါ 🎬 👇",
        "hashtags": ["#movierecap", "#burmeserecap", "#animerecap", "#tiktokmyanmar", "#recapmyanmar", "#fyp", "#foryou", "#viralvideo"]
    }


def detect_hardsubs_in_video(video_path: Path, work_dir: Path) -> Dict[str, Any]:
    """
    Extracts a frame from the video and uses Gemini Vision to detect hardcoded foreign
    subtitles or watermarks, returning normalized percentage coordinates.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    frame_jpg = work_dir / "detect_frame.jpg"
    ffmpeg_bin = get_ffmpeg_exe()

    try:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-ss",
            "00:00:04",
            "-i",
            str(video_path),
            "-vframes",
            "1",
            "-q:v",
            "2",
            str(frame_jpg),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("Frame extraction notice:", e)

    if not frame_jpg.exists():
        return {
            "has_subtitles": True,
            "box": {"x": 10, "y": 82, "w": 80, "h": 12},
            "source": "heuristic_fallback"
        }

    try:
        import base64
        frame_bytes = frame_jpg.read_bytes()
        frame_b64 = base64.b64encode(frame_bytes).decode("utf-8")
        vision_prompt = """
Examine this video frame carefully. Locate burned-in foreign hardcoded subtitles (Chinese, Thai, Japanese, English) or watermarks in the lower third or corners.
Return a single bounding box in percentages (0 to 100) covering the subtitles:
- x: percentage from left edge (e.g. 10)
- y: percentage from top edge (e.g. 80)
- w: width percentage (e.g. 80)
- h: height percentage (e.g. 14)
Return ONLY JSON:
{"has_subtitles": true, "box": {"x": 10, "y": 82, "w": 80, "h": 12}}
"""
        def _call_gemini_vision(api_key: str) -> Dict[str, Any]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={api_key}"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": vision_prompt},
                            {
                                "inline_data": {
                                    "mime_type": "image/jpeg",
                                    "data": frame_b64
                                }
                            }
                        ]
                    }
                ],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = body.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            return json.loads(text)

        data = key_pool.execute_with_failover("gemini", _call_gemini_vision)
        if "box" in data:
            b = data["box"]
            return {
                "has_subtitles": bool(data.get("has_subtitles", True)),
                "box": {
                    "x": float(b.get("x", 10)),
                    "y": float(b.get("y", 82)),
                    "w": float(b.get("w", 80)),
                    "h": float(b.get("h", 12))
                },
                "source": "gemini_vision"
            }
    except Exception as gemini_err:
        print("Gemini vision subtitle detection notice:", gemini_err)

    return {
        "has_subtitles": True,
        "box": {"x": 10, "y": 82, "w": 80, "h": 12},
        "source": "heuristic_fallback"
    }


def render_final_video(
    source_video: Path,
    voice_audio: Optional[Path],
    srt_path: Optional[Path],
    out_video: Path,
    options: Dict[str, Any],
    media_info: Dict[str, Any],
) -> Path:
    """
    Single-pass FFmpeg renderer supporting:
    - Aspect ratio crop/pad (`original`, `9:16`, `3:4`, `1:1`)
    - Anti-ContentID Copyright Bypass (`use_zoom`, dynamic subtle pan, `flip_video`, `auto_color`)
    - Up to 3 Hardsub Blur Masks (`blur_boxes`, `blur_strength`)
    - Watermark text (`watermark_text`)
    - Burned-in Burmese/Multilingual Subtitles (`subtitle_enabled`)
    - Audio ducking / SFX preservation (`sfx_only` in dubbing/recap mode)
    """
    v_filters: List[str] = []
    w = int(media_info.get("width", 1280))
    h = int(media_info.get("height", 720))

    # 1. Anti-ContentID Crop / Zoom (use_zoom)
    if options.get("use_zoom"):
        v_filters.append("crop=w=in_w*0.88:h=in_h*0.88:x=(in_w-out_w)/2:y=(in_h-out_h)/2")

    # 2. Horizontal Flip (flip_video)
    if options.get("flip_video"):
        v_filters.append("hflip")

    # 3. Auto Color Enhancement (auto_color)
    if options.get("auto_color"):
        v_filters.append("eq=contrast=1.05:brightness=0.02:saturation=1.08")

    # 3b. Hardsub Blur Masks / Delogo Filters
    if options.get("blur_enabled"):
        blur_boxes = options.get("blur_boxes") or []
        for b in blur_boxes:
            try:
                bx = float(b.get("x", 0))
                by = float(b.get("y", 0))
                bw = float(b.get("w", 0))
                bh = float(b.get("h", 0))
                if bx <= 1.0 and by <= 1.0 and bw <= 1.0 and bh <= 1.0:
                    px = int(bx * w)
                    py = int(by * h)
                    pw = int(bw * w)
                    ph = int(bh * h)
                elif bx <= 100 and by <= 100 and bw <= 100 and bh <= 100:
                    px = int((bx / 100.0) * w)
                    py = int((by / 100.0) * h)
                    pw = int((bw / 100.0) * w)
                    ph = int((bh / 100.0) * h)
                else:
                    px, py, pw, ph = int(bx), int(by), int(bw), int(bh)

                # Clamp with 1-pixel border to avoid delogo boundary assertion
                px = max(1, min(w - 3, px))
                py = max(1, min(h - 3, py))
                pw = max(2, min(w - px - 1, pw))
                ph = max(2, min(h - py - 1, ph))

                if pw >= 4 and ph >= 4:
                    v_filters.append(f"delogo=x={px}:y={py}:w={pw}:h={ph}")
            except Exception as b_err:
                print("Delogo blur box filter notice:", b_err)

    # 4. Aspect Ratio Sizing & Padding (Zero-crash integer coordinates)
    ratio = options.get("aspect_ratio", "original")
    if ratio == "9:16":
        v_filters.append("scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black")
    elif ratio == "3:4":
        v_filters.append("scale=1080:1440:force_original_aspect_ratio=decrease,pad=1080:1440:(ow-iw)/2:(oh-ih)/2:black")
    elif ratio == "1:1":
        v_filters.append("scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:black")
    else:
        v_filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    # 5. Burned-in Subtitles (Hardsub if supported by local FFmpeg build)
    if options.get("subtitle_enabled") and srt_path and srt_path.exists() and options.get("subtitle_type", "encode") == "encode":
        escaped_srt = str(srt_path).replace("\\", "/").replace(":", "\\:")
        font_size = int(options.get("subtitle_font_size", 55)) // 2
        margin_v = int(options.get("subtitle_margin_v", 35))
        if "subtitle_y" in options:
            try:
                sub_y = float(options["subtitle_y"])
                if sub_y <= 1.0:
                    sub_y *= 100.0
                canvas_h = 1920 if ratio == "9:16" else (1440 if ratio == "3:4" else (1080 if ratio == "1:1" else h))
                margin_v = max(15, int(((100.0 - sub_y) / 100.0) * canvas_h))
            except Exception:
                margin_v = 35

        # Subtitle Color & Border Style Customization
        sub_color_val = str(options.get("subtitle_color", "white")).lower()
        if sub_color_val in ("yellow", "#ffff00", "#facc15"):
            primary_colour = "&H0000FFFF"  # Yellow in BGR format
        elif sub_color_val in ("cyan", "#00ffff", "#38bdf8"):
            primary_colour = "&H00FFFF00"  # Cyan in BGR format
        else:
            primary_colour = "&H00FFFFFF"  # White in BGR format

        sub_style_val = str(options.get("subtitle_style", "outline")).lower()
        if sub_style_val == "box":
            style_params = "BorderStyle=3,Outline=2,Shadow=1"
        else:
            style_params = "BorderStyle=1,Outline=3,Shadow=0"

        v_filters.append(
            f"subtitles='{escaped_srt}':force_style='FontName=Myanmar Text,FontSize={font_size},PrimaryColour={primary_colour},OutlineColour=&H00000000,{style_params},MarginV={margin_v}'"
        )

    vf_chain = ",".join(v_filters) if v_filters else "null"

    ffmpeg_bin = get_ffmpeg_exe()
    recap_len = options.get("recap_length", "full_length")
    src_duration = float(media_info.get("duration", 0.0))
    voice_vol = float(options.get("voice_volume", 1.0))
    bg_vol = float(options.get("bg_volume", 0.15)) if options.get("sfx_only", True) else 0.0

    if voice_audio and voice_audio.exists():
        if recap_len == "full_length" and src_duration > 0:
            # Full-Length Mode: 100% video runtime preserved. Duck background audio & pad voice.
            if bg_vol > 0:
                fc = f"[0:v]{vf_chain}[vout];[0:a]volume={bg_vol}[bg];[1:a]apad,volume={voice_vol}[voc];[voc][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                cmd = [
                    ffmpeg_bin, "-y",
                    "-i", str(source_video),
                    "-i", str(voice_audio),
                    "-filter_complex", fc,
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "128k",
                    "-t", f"{src_duration:.2f}",
                    "-movflags", "+faststart",
                    str(out_video),
                ]
            else:
                fc = f"[0:v]{vf_chain}[vout];[1:a]apad,volume={voice_vol}[aout]"
                cmd = [
                    ffmpeg_bin, "-y",
                    "-i", str(source_video),
                    "-i", str(voice_audio),
                    "-filter_complex", fc,
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "128k",
                    "-t", f"{src_duration:.2f}",
                    "-movflags", "+faststart",
                    str(out_video),
                ]
        else:
            # Short hook or custom cut mode
            if bg_vol > 0:
                fc = f"[0:v]{vf_chain}[vout];[0:a]volume={bg_vol}[bg];[1:a]volume={voice_vol}[voc];[voc][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                cmd = [
                    ffmpeg_bin, "-y",
                    "-stream_loop", "-1", "-i", str(source_video),
                    "-i", str(voice_audio),
                    "-filter_complex", fc,
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest",
                    "-movflags", "+faststart",
                    str(out_video),
                ]
            else:
                cmd = [
                    ffmpeg_bin, "-y",
                    "-stream_loop", "-1", "-i", str(source_video),
                    "-i", str(voice_audio),
                    "-vf", vf_chain,
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest",
                    "-movflags", "+faststart",
                    str(out_video),
                ]
    else:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(source_video),
            "-vf",
            vf_chain,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out_video),
        ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as err:
        stderr_msg = err.stderr.decode("utf-8", errors="ignore") if err.stderr else ""
        print("FFmpeg primary render notice:", stderr_msg[:200])
        # Fallback without subtitles filter (e.g. if static ffmpeg build lacks libass)
        clean_v_filters = [f for f in v_filters if not f.startswith("subtitles=")]
        clean_vf = ",".join(clean_v_filters) if clean_v_filters else "null"
        if voice_audio and voice_audio.exists():
            fallback_cmd = [
                ffmpeg_bin,
                "-y",
                "-stream_loop",
                "-1",
                "-i",
                str(source_video),
                "-i",
                str(voice_audio),
                "-vf",
                clean_vf,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_video),
            ]
        else:
            fallback_cmd = [
                ffmpeg_bin,
                "-y",
                "-i",
                str(source_video),
                "-vf",
                clean_vf,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(out_video),
            ]
        try:
            subprocess.run(fallback_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as fallback_err:
            fb_stderr = fallback_err.stderr.decode("utf-8", errors="ignore") if fallback_err.stderr else ""
            print("FFmpeg fallback render notice:", fb_stderr[:200])
            raise RuntimeError(f"FFmpeg render failure: {fb_stderr[-200:]}")

    # 6. Channel Intro & Outro Video Stitching (if provided)
    intro_path = options.get("intro_path")
    outro_path = options.get("outro_path")
    has_intro = bool(intro_path and Path(intro_path).exists())
    has_outro = bool(outro_path and Path(outro_path).exists())
    if has_intro or has_outro:
        try:
            core_rendered = out_video.parent / f"core_{out_video.name}"
            if out_video.exists():
                shutil.move(str(out_video), str(core_rendered))
                stitch_intro_outro(
                    main_video=core_rendered,
                    intro_video=Path(intro_path) if has_intro else None,
                    outro_video=Path(outro_path) if has_outro else None,
                    out_video=out_video,
                    aspect_ratio=ratio,
                )
                if core_rendered.exists():
                    core_rendered.unlink(missing_ok=True)
        except Exception as stitch_err:
            print("Intro/Outro stitch notice:", stitch_err)
            if not out_video.exists() and core_rendered.exists():
                shutil.move(str(core_rendered), str(out_video))

    return out_video


def push_to_telegram_storage(
    video_path: Path,
    caption: str,
) -> Dict[str, Any]:
    """
    Uploads finished MP4 to Telegram Bot API (`@hyugoustoragebot`) for free unlimited cloud storage.
    Automatically discovers `chat_id` via `getUpdates` if not yet set!
    """
    tg_cfg = key_pool.state.get("telegram", {})
    bot_token = tg_cfg.get("bot_token", "").strip()
    chat_id = str(tg_cfg.get("chat_id", "")).strip()
    if not bot_token:
        return {"pushed": False, "reason": "No Telegram bot token configured"}

    if not chat_id:
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{bot_token}/getUpdates", timeout=8) as r:
                updates = json.loads(r.read().decode("utf-8")).get("result", [])
                for u in reversed(updates):
                    msg = u.get("message") or u.get("channel_post") or {}
                    cid = msg.get("chat", {}).get("id")
                    if cid:
                        chat_id = str(cid)
                        key_pool.update_telegram(chat_id=chat_id)
                        break
        except Exception:
            pass

    if not chat_id:
        return {
            "pushed": False,
            "reason": "Send /start to @hyugoustoragebot on Telegram once so LTstudio can auto-detect your Chat ID!",
        }

    boundary = f"----LTstudioTG{uuid.uuid4().hex}"
    body = bytearray()
    for k, v in [("chat_id", chat_id), ("caption", caption[:950])]:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="video"; filename="{video_path.name}"\r\n'.encode())
    body.extend(b"Content-Type: video/mp4\r\n\r\n")
    body.extend(video_path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendVideo",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            video_obj = res_json.get("result", {}).get("video", {})
            return {
                "pushed": True,
                "chat_id": chat_id,
                "file_id": video_obj.get("file_id"),
                "message_id": res_json.get("result", {}).get("message_id"),
            }
    except Exception as exc:
        return {"pushed": False, "reason": str(exc)[:200]}
