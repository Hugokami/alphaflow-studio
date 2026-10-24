import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .key_pool import key_pool, BASE_DIR, DATA_DIR
from .voices import VOICE_CATALOG, resolve_voice
from .tts_engine import synthesize_voice
from .pipeline import (
    JOBS_DIR,
    download_source_video,
    probe_media_info,
    transcribe_with_groq_whisper,
    generate_recap_or_dub_script,
    write_srt_file,
    render_final_video,
    push_to_telegram_storage,
)

app = FastAPI(
    title="AlphaFlow AI Studio — Zero-Cost Video Recap & Dubbing Engine",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
SAMPLES_CACHE_DIR = DATA_DIR / "voice_samples"
SAMPLES_CACHE_DIR.mkdir(parents=True, exist_ok=True)

JOBS_STORE_FILE = DATA_DIR / "jobs_registry.json"
JOBS: Dict[str, Dict[str, Any]] = {}
if JOBS_STORE_FILE.exists():
    try:
        JOBS.update(json.loads(JOBS_STORE_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass


def _save_jobs() -> None:
    try:
        JOBS_STORE_FILE.write_text(json.dumps(JOBS, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def serve_studio_ui() -> HTMLResponse:
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>AlphaFlow UI initializing...</h1>")
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


# ============================================================================
# 1. MULTI-KEY POOL & SMART GEMINI TTS ROUTING ENDPOINTS
# ============================================================================

@app.get("/api/keys")
async def get_keys_status() -> Dict[str, Any]:
    return key_pool.get_public_status()


@app.post("/api/keys/add")
async def add_api_key(request: Request) -> Dict[str, Any]:
    body = await request.json()
    provider = body.get("provider", "gemini").strip().lower()
    api_key = body.get("api_key", "").strip()
    label = body.get("label", "").strip()
    if provider not in ("gemini", "groq"):
        raise HTTPException(status_code=400, detail="Provider must be 'gemini' or 'groq'")
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")
    entry = key_pool.add_key(provider, api_key, label=label)
    return {"success": True, "added": entry, "pool": key_pool.get_public_status()}


@app.delete("/api/keys/{provider}/{key_id}")
async def delete_api_key(provider: str, key_id: str) -> Dict[str, Any]:
    removed = key_pool.remove_key(provider.lower(), key_id)
    return {"success": removed, "pool": key_pool.get_public_status()}


@app.post("/api/keys/reset-cooldowns")
async def reset_key_cooldowns() -> Dict[str, Any]:
    key_pool.reset_cooldowns()
    return {"success": True, "pool": key_pool.get_public_status()}


@app.post("/api/tts-routing")
async def update_tts_routing_config(request: Request) -> Dict[str, Any]:
    body = await request.json()
    updated = key_pool.update_tts_routing(body)
    return {"success": True, "tts_routing": updated}


@app.post("/api/telegram/config")
async def update_telegram_config(request: Request) -> Dict[str, Any]:
    body = await request.json()
    updated = key_pool.update_telegram(
        bot_token=body.get("bot_token"),
        chat_id=body.get("chat_id"),
    )
    return {"success": True, "telegram": updated}


# ============================================================================
# 2. VOICES & LIVE VOICE PREVIEW ENDPOINTS (45 Voices: 15 Edge + 30 Gemini)
# ============================================================================

@app.get("/api/voices")
async def list_voices() -> Dict[str, Any]:
    return {
        "total": len(VOICE_CATALOG),
        "free_count": sum(1 for v in VOICE_CATALOG if v["provider"] == "microsoft"),
        "gemini_count": sum(1 for v in VOICE_CATALOG if v["provider"] == "gemini"),
        "voices": VOICE_CATALOG,
        "tts_routing": key_pool.state["tts_routing"],
    }


@app.get("/api/video/voice-sample")
async def preview_voice_sample(
    voice: str = "my-MM-ThihaNeural",
    speed: float = 1.0,
    enhancement: str = "none",
    pitch: int = 0,
    tts_model: str = "gemini-2.5-flash-preview-tts",
    text: Optional[str] = None,
) -> FileResponse:
    voice_spec = resolve_voice(voice)
    sample_text = (text or voice_spec.get("sample_text") or "မင်္ဂလာပါ၊ အယ်လ်ဖာဖလိုး စတူဒီယိုမှ ကြိုဆိုပါတယ်။").strip()

    cache_key = f"{voice_spec['id']}_{tts_model}_{speed:.2f}_{pitch}_{enhancement}_{abs(hash(sample_text)) % 100000}.wav"
    cached_file = SAMPLES_CACHE_DIR / cache_key
    if not cached_file.exists():
        tmp_dir = SAMPLES_CACHE_DIR / f"tmp_{uuid.uuid4().hex[:8]}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            res = await synthesize_voice(
                text=sample_text,
                voice_identifier=voice_spec["id"],
                work_dir=tmp_dir,
                video_duration_sec=15.0,
                requested_gemini_model=tts_model,
                speed=speed,
                pitch_hz=pitch,
                pause_mode="remove",
                enhancement=enhancement,
            )
            shutil.copy2(res["audio_path"], cached_file)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return FileResponse(str(cached_file), media_type="audio/wav", filename=cached_file.name)


# ============================================================================
# 3. VIDEO UPLOAD, PREVIEW & 4-STAGE PROCESSING ENGINE
# ============================================================================

@app.post("/api/video/upload-video")
async def upload_source_video_endpoint(file: UploadFile = File(...)) -> Dict[str, Any]:
    upload_id = f"up_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    job_dir = JOBS_DIR / upload_id
    job_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    dest_path = job_dir / f"source{ext}"
    with dest_path.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    meta = await asyncio.to_thread(probe_media_info, dest_path)
    # Determine which Gemini TTS model smart routing will pick for this duration
    routed_models = key_pool.resolve_gemini_tts_models(video_duration_sec=meta["duration"])
    return {
        "success": True,
        "upload_id": upload_id,
        "video_file_path": str(dest_path),
        "filename": file.filename,
        "duration": meta["duration"],
        "width": meta["width"],
        "height": meta["height"],
        "smart_routed_tts_model": routed_models[0],
        "preview_url": f"/api/video/local-preview/{upload_id}",
    }


@app.get("/api/video/local-preview/{job_id}")
async def stream_local_preview(job_id: str) -> FileResponse:
    job_dir = JOBS_DIR / job_id
    for candidate in ["final_output.mp4", "source.mp4", "source.webm", "source.mkv", "source.mov"]:
        p = job_dir / candidate
        if p.exists():
            return FileResponse(str(p), media_type="video/mp4")
    raise HTTPException(status_code=404, detail="Video file not found")


async def _run_job_pipeline(job_id: str, params: Dict[str, Any]) -> None:
    job = JOBS[job_id]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def _update(step: int, progress: int, log_msg: str, **extra: Any) -> None:
        job["step"] = step
        job["progress"] = progress
        job["log"] = log_msg
        job.update(extra)
        _save_jobs()

    try:
        # STEP 0: Preparing Video (Download URL or Probe Uploaded File)
        _update(0, 10, "Preparing source video & probing duration...")
        if params.get("video_file_path") and Path(params["video_file_path"]).exists():
            src_path = Path(params["video_file_path"])
            if src_path.parent != job_dir:
                target_src = job_dir / f"source{src_path.suffix}"
                shutil.copy2(src_path, target_src)
                src_path = target_src
            meta = await asyncio.to_thread(probe_media_info, src_path)
            media_info = {"video_path": src_path, "title": src_path.stem, **meta}
        elif params.get("url"):
            media_info = await asyncio.to_thread(download_source_video, params["url"], job_dir)
        else:
            raise RuntimeError("Neither video URL nor uploaded video file was provided.")

        duration_sec = float(media_info.get("duration") or 0.0)
        routed_models = key_pool.resolve_gemini_tts_models(
            video_duration_sec=duration_sec,
            requested_model=params.get("gemini_tts_model"),
        )
        _update(
            1,
            28,
            f"Video ready ({duration_sec:.1f}s) · Smart TTS Route: {routed_models[0]} · Transcribing with Groq Whisper...",
            duration=duration_sec,
            routed_tts_model=routed_models[0],
        )

        # STEP 1: Transcription via Groq Whisper Large v3 Turbo
        transcript_data = await asyncio.to_thread(
            transcribe_with_groq_whisper,
            media_info["video_path"],
            job_dir,
        )
        _update(2, 50, "Generating syllable-aligned Burmese script via Multi-Key Gemini / Groq LLM...")

        # STEP 2: Recap / Dubbing Script Generation
        if params.get("custom_script") and str(params["custom_script"]).strip():
            full_narration = str(params["custom_script"]).strip()
            clauses = [c.strip() for c in full_narration.replace("။", "။\n").splitlines() if c.strip()]
            slot = max(2.0, duration_sec / max(1, len(clauses)))
            script_result = {
                "viral_title": media_info.get("title", "AlphaFlow Recap")[:25],
                "hook_line": clauses[0] if clauses else "",
                "full_narration": full_narration,
                "captions": [
                    {"start": round(i * slot, 2), "end": round(min(duration_sec, (i + 1) * slot), 2), "text": c}
                    for i, c in enumerate(clauses)
                ],
            }
        else:
            script_result = await asyncio.to_thread(
                generate_recap_or_dub_script,
                transcript_data,
                duration_sec,
                params.get("processing_mode", "recap"),
                params.get("target_language", "Burmese (Myanmar Language)"),
                params.get("script_style", "Engaging Story"),
                params.get("voice_tone", "Energetic"),
            )

        srt_path = job_dir / "captions.srt"
        await asyncio.to_thread(write_srt_file, script_result.get("captions", []), srt_path)

        # STEP 3: Voice Synthesis (Edge-TTS or Smart-Routed Gemini 3.8 / 2.5 TTS)
        voice_audio_path: Optional[Path] = None
        tts_model_used = "subtitle_only"
        if params.get("processing_mode") != "subtitle":
            _update(
                2,
                70,
                f"Synthesizing voice ({params.get('voice')}) via {routed_models[0]}...",
                script=script_result,
            )
            tts_res = await synthesize_voice(
                text=script_result["full_narration"],
                voice_identifier=params.get("voice", "my-MM-ThihaNeural"),
                work_dir=job_dir,
                video_duration_sec=duration_sec,
                requested_gemini_model=params.get("gemini_tts_model", "auto_duration"),
                speed=float(params.get("voice_speed", 1.0)),
                pitch_hz=int(params.get("microsoft_pitch", 0)),
                pause_mode=params.get("microsoft_pause_mode", "remove"),
                enhancement=params.get("audio_enhancement", "none"),
            )
            voice_audio_path = tts_res["audio_path"]
            tts_model_used = tts_res["model_used"]

        # STEP 4: FFmpeg Single-Pass Video Render + Anti-ContentID
        _update(3, 85, "Rendering final video with FFmpeg Anti-ContentID filtergraph...", tts_model_used=tts_model_used)
        out_video = job_dir / "final_output.mp4"
        await asyncio.to_thread(
            render_final_video,
            media_info["video_path"],
            voice_audio_path,
            srt_path,
            out_video,
            params,
            media_info,
        )

        # STEP 5: Optional Telegram Storage Push (@hyugoustoragebot)
        tg_result = {"pushed": False}
        if params.get("push_telegram", True):
            _update(3, 95, "Uploading rendered MP4 to Telegram Bot cloud storage (@hyugoustoragebot)...")
            caption = (
                f"🎬 {script_result.get('viral_title', 'AlphaFlow Render')}\n"
                f"🎙 Voice: {params.get('voice')} ({tts_model_used})\n"
                f"⏱ Duration: {duration_sec:.1f}s"
            )
            tg_result = await asyncio.to_thread(push_to_telegram_storage, out_video, caption)

        _update(
            4,
            100,
            "Completed! Video rendered & archived.",
            status="completed",
            download_url=f"/api/video/local-preview/{job_id}",
            srt_url=f"/api/video/subtitle/{job_id}/download",
            script=script_result,
            tts_model_used=tts_model_used,
            telegram=tg_result,
        )
    except Exception as exc:
        _update(
            job.get("step", 0),
            job.get("progress", 0),
            f"Error: {str(exc)}",
            status="failed",
            error=str(exc),
        )


# ============================================================================
# 4. STEP-BY-STEP PIPELINE ENDPOINTS (Zero Freeze on Serverless / Vercel)
# ============================================================================

@app.post("/api/video/step/prepare")
async def step_prepare_video(request: Request) -> Any:
    try:
        params = await request.json()
        job_id = params.get("job_id") or f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        if params.get("video_file_path") and Path(params["video_file_path"]).exists():
            src_path = Path(params["video_file_path"])
            if src_path.parent != job_dir:
                target_src = job_dir / f"source{src_path.suffix}"
                shutil.copy2(src_path, target_src)
                src_path = target_src
            meta = await asyncio.to_thread(probe_media_info, src_path)
            media_info = {"video_path": str(src_path), "title": src_path.stem, **meta}
        elif params.get("url"):
            info = await asyncio.to_thread(download_source_video, params["url"], job_dir)
            media_info = {
                "video_path": str(info["video_path"]),
                "title": info.get("title", "Video"),
                "duration": float(info.get("duration") or 0.0),
                "width": int(info.get("width") or 1280),
                "height": int(info.get("height") or 720),
            }
        else:
            raise HTTPException(status_code=400, detail="Neither video URL nor uploaded video file was provided.")

        duration_sec = float(media_info.get("duration") or 0.0)
        routed_models = key_pool.resolve_gemini_tts_models(
            video_duration_sec=duration_sec,
            requested_model=params.get("gemini_tts_model"),
        )

        JOBS[job_id] = {
            "job_id": job_id,
            "status": "processing",
            "step": 1,
            "progress": 25,
            "log": f"Source ready ({duration_sec:.1f}s) · Smart Route: {routed_models[0]}",
            "duration": duration_sec,
            "routed_tts_model": routed_models[0],
            "media_info": media_info,
            "created_at": int(time.time()),
            "params": params,
        }
        _save_jobs()

        return {
            "success": True,
            "job_id": job_id,
            "media_info": media_info,
            "duration": duration_sec,
            "title": media_info["title"],
            "routed_tts_model": routed_models[0],
            "progress": 25,
        }
    except Exception as exc:
        import traceback
        trace = traceback.format_exc()
        print("ERROR IN STEP_PREPARE_VIDEO:", trace)
        return JSONResponse(status_code=500, content={"error": str(exc), "traceback": trace})


@app.post("/api/video/step/transcribe")
async def step_transcribe_audio(request: Request) -> Dict[str, Any]:
    params = await request.json()
    job_id = params.get("job_id")
    if not job_id or job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    job_dir = JOBS_DIR / job_id
    media_info = JOBS[job_id].get("media_info") or {}
    video_path_str = params.get("video_path") or media_info.get("video_path")
    video_path = Path(video_path_str) if video_path_str else (job_dir / "source.mp4")
    if not video_path.exists():
        vids = [p for p in job_dir.glob("source.*") if p.suffix.lower() not in (".json", ".part", ".ytdl")]
        if vids:
            video_path = vids[0]
        else:
            raise HTTPException(status_code=400, detail="Source media file not found for transcription")

    transcript_data = await asyncio.to_thread(
        transcribe_with_groq_whisper,
        video_path,
        job_dir,
    )

    JOBS[job_id]["step"] = 2
    JOBS[job_id]["progress"] = 50
    JOBS[job_id]["log"] = f"Transcribed ({len(transcript_data.get('segments', []))} segments, language: {transcript_data.get('language')})"
    JOBS[job_id]["transcript_data"] = transcript_data
    _save_jobs()

    return {
        "success": True,
        "job_id": job_id,
        "transcript_data": transcript_data,
        "progress": 50,
    }


@app.post("/api/video/step/script")
async def step_generate_script(request: Request) -> Dict[str, Any]:
    params = await request.json()
    job_id = params.get("job_id")
    if not job_id or job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    job_dir = JOBS_DIR / job_id
    transcript_data = params.get("transcript_data") or JOBS[job_id].get("transcript_data") or {"text": "", "segments": []}
    duration_sec = float(params.get("duration_sec") or JOBS[job_id].get("duration") or 30.0)

    if params.get("custom_script") and str(params["custom_script"]).strip():
        full_narration = str(params["custom_script"]).strip()
        clauses = [c.strip() for c in full_narration.replace("။", "။\n").splitlines() if c.strip()]
        slot = max(2.0, duration_sec / max(1, len(clauses)))
        script_result = {
            "viral_title": params.get("title", "AlphaFlow Recap")[:25],
            "hook_line": clauses[0] if clauses else "",
            "full_narration": full_narration,
            "captions": [
                {"start": round(i * slot, 2), "end": round(min(duration_sec, (i + 1) * slot), 2), "text": c}
                for i, c in enumerate(clauses)
            ],
        }
    else:
        script_result = await asyncio.to_thread(
            generate_recap_or_dub_script,
            transcript_data,
            duration_sec,
            params.get("processing_mode", "recap"),
            params.get("target_language", "Burmese (Myanmar Language)"),
            params.get("script_style", "Engaging Story"),
            params.get("voice_tone", "Energetic"),
        )

    srt_path = job_dir / "captions.srt"
    await asyncio.to_thread(write_srt_file, script_result.get("captions", []), srt_path)

    JOBS[job_id]["step"] = 3
    JOBS[job_id]["progress"] = 75
    JOBS[job_id]["log"] = f"Script generated ({len(script_result.get('captions', []))} subtitle lines)"
    JOBS[job_id]["script"] = script_result
    _save_jobs()

    return {
        "success": True,
        "job_id": job_id,
        "script": script_result,
        "srt_url": f"/api/video/subtitle/{job_id}/download",
        "progress": 75,
    }


@app.post("/api/video/step/tts")
async def step_synthesize_tts(request: Request) -> Dict[str, Any]:
    params = await request.json()
    job_id = params.get("job_id")
    if not job_id or job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    job_dir = JOBS_DIR / job_id
    script = params.get("script") or JOBS[job_id].get("script") or {}
    text = script.get("full_narration") or params.get("text", "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="Narration text is empty")

    duration_sec = float(params.get("duration_sec") or JOBS[job_id].get("duration") or 0.0)

    tts_res = await synthesize_voice(
        text=text,
        voice_identifier=params.get("voice", "my-MM-ThihaNeural"),
        work_dir=job_dir,
        video_duration_sec=duration_sec,
        requested_gemini_model=params.get("gemini_tts_model", "auto_duration"),
        speed=float(params.get("voice_speed", 1.0)),
        pitch_hz=int(params.get("pitch_hz") or params.get("microsoft_pitch") or 0),
        pause_mode=params.get("pause_mode") or params.get("microsoft_pause_mode") or "remove",
        enhancement=params.get("audio_enhancement", "none"),
    )

    JOBS[job_id]["step"] = 4
    JOBS[job_id]["progress"] = 90
    JOBS[job_id]["log"] = f"Voice synthesized via {tts_res['model_used']}"
    JOBS[job_id]["voice_audio_path"] = str(tts_res["audio_path"])
    JOBS[job_id]["tts_model_used"] = tts_res["model_used"]
    _save_jobs()

    return {
        "success": True,
        "job_id": job_id,
        "model_used": tts_res["model_used"],
        "voice_audio_url": f"/api/video/voice-audio/{job_id}",
        "progress": 90,
    }


@app.post("/api/video/step/render")
async def step_render_video(request: Request) -> Dict[str, Any]:
    params = await request.json()
    job_id = params.get("job_id")
    if not job_id or job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    job_dir = JOBS_DIR / job_id
    media_info = JOBS[job_id].get("media_info") or {}
    vids = [p for p in job_dir.glob("source.*") if p.suffix.lower() not in (".json", ".part", ".mp3", ".wav", ".ytdl")]
    source_video = vids[0] if vids else (job_dir / "source.mp4")

    audio_path = job_dir / "final_voice.wav"
    if not audio_path.exists():
        candidates = list(job_dir.glob("*.wav")) + list(job_dir.glob("*.mp3"))
        audio_path = candidates[0] if candidates else None

    srt_path = job_dir / "captions.srt"
    out_video = job_dir / "final_output.mp4"

    render_options = {**JOBS[job_id].get("params", {}), **params}

    await asyncio.to_thread(
        render_final_video,
        source_video,
        audio_path,
        srt_path,
        out_video,
        render_options,
        media_info,
    )

    tg_result = {"pushed": False}
    if render_options.get("push_telegram", True):
        script = JOBS[job_id].get("script") or {}
        duration_sec = float(JOBS[job_id].get("duration") or 0.0)
        tts_model = JOBS[job_id].get("tts_model_used", "AlphaFlow TTS")
        caption = (
            f"🎬 {script.get('viral_title', 'AlphaFlow Render')}\n"
            f"🎙 Voice: {render_options.get('voice')} ({tts_model})\n"
            f"⏱ Duration: {duration_sec:.1f}s"
        )
        tg_result = await asyncio.to_thread(push_to_telegram_storage, out_video, caption)

    JOBS[job_id]["status"] = "completed"
    JOBS[job_id]["step"] = 5
    JOBS[job_id]["progress"] = 100
    JOBS[job_id]["log"] = "Completed! Video rendered & archived."
    JOBS[job_id]["download_url"] = f"/api/video/local-preview/{job_id}"
    JOBS[job_id]["srt_url"] = f"/api/video/subtitle/{job_id}/download"
    JOBS[job_id]["telegram"] = tg_result
    _save_jobs()

    return {
        "success": True,
        "job_id": job_id,
        "status": "completed",
        "download_url": f"/api/video/local-preview/{job_id}",
        "srt_url": f"/api/video/subtitle/{job_id}/download",
        "telegram": tg_result,
        "progress": 100,
    }


@app.get("/api/video/voice-audio/{job_id}")
async def stream_job_voice_audio(job_id: str) -> FileResponse:
    job_dir = JOBS_DIR / job_id
    for name in ["final_voice.wav", "final_voice.mp3", "tts_raw.wav", "tts_raw.mp3"]:
        p = job_dir / name
        if p.exists():
            return FileResponse(str(p), media_type="audio/wav")
    candidates = list(job_dir.glob("*.wav")) + list(job_dir.glob("*.mp3"))
    if candidates:
        return FileResponse(str(candidates[0]), media_type="audio/wav")
    raise HTTPException(status_code=404, detail="Audio file not found")


@app.post("/api/video/process")
async def start_video_processing(request: Request) -> Dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        params = await request.json()
    else:
        form = await request.form()
        params = dict(form)
        if "blur_boxes" in params and isinstance(params["blur_boxes"], str):
            try:
                params["blur_boxes"] = json.loads(params["blur_boxes"])
            except Exception:
                params["blur_boxes"] = []
        for bool_k in ("use_zoom", "subtitle_enabled", "blur_enabled", "flip_video", "auto_color", "sfx_only", "push_telegram"):
            if bool_k in params:
                params[bool_k] = str(params[bool_k]).lower() in ("true", "1", "on", "yes")

    job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "processing",
        "step": 0,
        "progress": 2,
        "log": "Queued in AlphaFlow Engine...",
        "created_at": int(time.time()),
        "params": params,
    }
    _save_jobs()
    asyncio.create_task(_run_job_pipeline(job_id, params))
    return {"success": True, "job_id": job_id, "job": JOBS[job_id]}


@app.get("/api/video/status/{job_id}")
async def get_job_status(job_id: str) -> Dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]


@app.get("/api/video/subtitle/{job_id}/download")
async def download_job_srt(job_id: str) -> FileResponse:
    srt_path = JOBS_DIR / job_id / "captions.srt"
    if not srt_path.exists():
        raise HTTPException(status_code=404, detail="Subtitle file not found")
    return FileResponse(str(srt_path), media_type="text/plain; charset=utf-8", filename=f"{job_id}.srt")


@app.get("/api/jobs")
async def list_all_jobs() -> Dict[str, Any]:
    ordered = sorted(JOBS.values(), key=lambda x: x.get("created_at", 0), reverse=True)
    return {"jobs": ordered[:30]}
