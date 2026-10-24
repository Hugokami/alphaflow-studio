import asyncio
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


def download_source_video(url: str, dest_dir: Path) -> Dict[str, Any]:
    """Downloads video or extracts audio stream from URL using yt-dlp Python library with cloud anti-bot bypass."""
    import yt_dlp

    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(dest_dir / "source.%(ext)s")
    ffmpeg_bin = get_ffmpeg_exe()

    ydl_opts = {
        "outtmpl": out_tmpl,
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "merge_output_format": "mp4",
        "no_playlist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_bin,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "tv", "web_embedded"]
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
        # Fallback to audio-only stream download if full 720p muxing failed
        try:
            audio_opts = {
                "outtmpl": str(dest_dir / "source.%(ext)s"),
                "format": "bestaudio/best",
                "no_playlist": True,
                "quiet": True,
                "no_warnings": True,
                "ffmpeg_location": ffmpeg_bin,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "ios", "tv", "web_embedded"]
                    }
                },
            }
            with yt_dlp.YoutubeDL(audio_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title") or title
                duration = float(info.get("duration") or 0.0)
        except Exception as e2:
            raise RuntimeError(f"yt-dlp video error: {str(e)[:160]} | audio fallback: {str(e2)[:160]}")

    candidates = [p for p in dest_dir.glob("source.*") if p.suffix.lower() not in (".json", ".part", ".ytdl")]
    if not candidates:
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
    boundary = f"----AlphaFlowBoundary{uuid.uuid4().hex}"

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
                "User-Agent": "AlphaFlowStudio/2.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return key_pool.execute_with_failover("groq", _call_groq_stt)


def generate_recap_or_dub_script(
    transcript_data: Dict[str, Any],
    video_duration_sec: float,
    processing_mode: str = "recap",
    target_language: str = "Burmese (Myanmar Language)",
    script_style: str = "Engaging Story",
    voice_tone: str = "Energetic",
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

    mode_instruction = (
        "Write a gripping, viral movie/anime recap narration"
        if processing_mode == "recap"
        else "Translate and adapt the dialogue for natural lip-synced dubbing"
        if processing_mode == "dubbing"
        else "Translate the transcript into accurate, punchy subtitle lines"
    )

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
                        "User-Agent": "AlphaFlowStudio/2.0",
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
        for gem_model in ("gemini-3.8-flash", "gemini-2.5-flash"):
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

    # 1. Aspect Ratio Crop
    ratio = options.get("aspect_ratio", "original")
    if ratio == "9:16":
        target_w = int(h * 9 / 16)
        if target_w <= w:
            v_filters.append(f"crop={target_w}:{h}:(in_w-{target_w})/2:0,scale=1080:1920")
        else:
            v_filters.append("scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black")
        w, h = 1080, 1920
    elif ratio == "3:4":
        target_w = int(h * 3 / 4)
        if target_w <= w:
            v_filters.append(f"crop={target_w}:{h}:(in_w-{target_w})/2:0,scale=1080:1440")
        else:
            v_filters.append("scale=1080:1440:force_original_aspect_ratio=decrease,pad=1080:1440:(ow-iw)/2:(oh-ih)/2:black")
        w, h = 1080, 1440
    elif ratio == "1:1":
        side = min(w, h)
        v_filters.append(f"crop={side}:{side}:(in_w-{side})/2:(in_h-{side})/2,scale=1080:1080")
        w, h = 1080, 1080

    # 2. Copyright Bypass (Dynamic Zoom + Subtle Color Shift)
    if options.get("use_zoom", True):
        v_filters.append(f"crop=w=in_w*0.90:h=in_h*0.90:x='(in_w-out_w)/2+sin(t*0.4)*12':y='(in_h-out_h)/2',scale={w}:{h}")
    if options.get("flip_video", False):
        v_filters.append("hflip")
    if options.get("auto_color", False):
        v_filters.append("eq=contrast=1.05:brightness=0.015:saturation=1.08")

    # 3. Blur Boxes (Hardsub Removal)
    blur_boxes = options.get("blur_boxes") or []
    if options.get("blur_enabled") and isinstance(blur_boxes, list):
        for box in blur_boxes[:3]:
            try:
                bx = max(1, int(float(box.get("x", 10)) / 100.0 * w))
                by = max(1, int(float(box.get("y", 80)) / 100.0 * h))
                bw = max(16, min(w - bx - 2, int(float(box.get("w", 80)) / 100.0 * w)))
                bh = max(16, min(h - by - 2, int(float(box.get("h", 12)) / 100.0 * h)))
                v_filters.append(f"delogo=x={bx}:y={by}:w={bw}:h={bh}:show=0")
            except Exception:
                pass

    # 4. Burned-in Subtitles (Hardsub)
    if options.get("subtitle_enabled") and srt_path and srt_path.exists() and options.get("subtitle_type", "encode") == "encode":
        # FFmpeg Windows path escaping for subtitles filter
        escaped_srt = str(srt_path).replace("\\", "/").replace(":", "\\:")
        font_size = int(options.get("subtitle_font_size", 55)) // 2
        v_filters.append(
            f"subtitles='{escaped_srt}':force_style='FontName=Myanmar Text,FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=1,MarginV=35'"
        )

    vf_chain = ",".join(v_filters) if v_filters else "null"

    # Build Audio Mix
    ffmpeg_bin = get_ffmpeg_exe()
    cmd = [ffmpeg_bin, "-y", "-i", str(source_video)]
    if voice_audio and voice_audio.exists():
        cmd.extend(["-i", str(voice_audio)])
        sfx_bg = options.get("sfx_only", True)
        bg_vol = "0.14" if sfx_bg else "0.0"
        af_complex = (
            f"[0:a]volume={bg_vol},lowpass=f=3200[bg];"
            f"[1:a]volume=1.0[vo];"
            f"[vo][bg]amix=inputs=2:duration=first:dropout_transition=2,dynaudnorm=f=150:g=13[aout]"
        )
        cmd.extend(
            [
                "-filter_complex",
                f"[0:v]{vf_chain}[vout];{af_complex}",
                "-map",
                "[vout]",
                "-map",
                "[aout]",
            ]
        )
    else:
        cmd.extend(["-vf", vf_chain, "-map", "0:v:0", "-map", "0:a?"])

    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out_video),
        ]
    )
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as err:
        # If hardsub subtitles filter failed (e.g. libass missing in static ffmpeg build), retry without hardsub filter
        if "subtitles=" in vf_chain:
            fallback_v_filters = [f for f in v_filters if not f.startswith("subtitles=")]
            fallback_vf = ",".join(fallback_v_filters) if fallback_v_filters else "null"
            fallback_cmd = [ffmpeg_bin, "-y", "-i", str(source_video)]
            if voice_audio and voice_audio.exists():
                fallback_cmd.extend(["-i", str(voice_audio)])
                fallback_cmd.extend(
                    [
                        "-filter_complex",
                        f"[0:v]{fallback_vf}[vout];{af_complex}",
                        "-map",
                        "[vout]",
                        "-map",
                        "[aout]",
                    ]
                )
            else:
                fallback_cmd.extend(["-vf", fallback_vf, "-map", "0:v:0", "-map", "0:a?"])
            fallback_cmd.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "21",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(out_video),
                ]
            )
            subprocess.run(fallback_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise err
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
            "reason": "Send /start to @hyugoustoragebot on Telegram once so AlphaFlow can auto-detect your Chat ID!",
        }

    boundary = f"----AlphaFlowTG{uuid.uuid4().hex}"
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
