import argparse
import asyncio
import json
import urllib.request
from pathlib import Path

from backend.key_pool import key_pool
from backend.pipeline import (
    JOBS_DIR,
    download_source_video,
    probe_media_info,
    transcribe_with_groq_whisper,
    generate_recap_or_dub_script,
    write_srt_file,
    render_final_video,
    push_to_telegram_storage,
)
from backend.tts_engine import synthesize_voice


async def run_cli_job(args: argparse.Namespace) -> dict:
    job_dir = JOBS_DIR / args.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    if args.video_file and Path(args.video_file).exists():
        vpath = Path(args.video_file)
        meta = probe_media_info(vpath)
        media_info = {"video_path": vpath, "title": vpath.stem, **meta}
    else:
        media_info = download_source_video(args.url, job_dir)

    duration_sec = float(media_info.get("duration") or 0.0)
    routed_models = key_pool.resolve_gemini_tts_models(
        video_duration_sec=duration_sec,
        requested_model=args.gemini_tts_model,
    )
    print(f"[AlphaFlow CLI] Duration={duration_sec:.1f}s -> Smart Routed TTS Models={routed_models}")

    transcript = transcribe_with_groq_whisper(media_info["video_path"], job_dir)
    if args.custom_script:
        script_res = {
            "viral_title": media_info.get("title", "AlphaFlow Recap")[:25],
            "hook_line": args.custom_script[:60],
            "full_narration": args.custom_script,
            "captions": [
                {"start": 0.0, "end": max(2.0, duration_sec), "text": args.custom_script}
            ],
        }
    else:
        script_res = generate_recap_or_dub_script(
            transcript_data=transcript,
            video_duration_sec=duration_sec,
            processing_mode=args.mode,
            target_language=args.target_language,
        )

    srt_path = job_dir / "captions.srt"
    write_srt_file(script_res.get("captions", []), srt_path)

    tts_res = await synthesize_voice(
        text=script_res["full_narration"],
        voice_identifier=args.voice,
        work_dir=job_dir,
        video_duration_sec=duration_sec,
        requested_gemini_model=args.gemini_tts_model,
        speed=args.speed,
        enhancement=args.enhancement,
    )

    out_video = job_dir / "final_output.mp4"
    render_final_video(
        source_video=media_info["video_path"],
        voice_audio=tts_res["audio_path"],
        srt_path=srt_path,
        out_video=out_video,
        options={
            "aspect_ratio": args.aspect_ratio,
            "use_zoom": args.use_zoom,
            "subtitle_enabled": True,
            "subtitle_type": "encode",
            "subtitle_font_size": 52,
            "sfx_only": True,
            "auto_color": True,
        },
        media_info=media_info,
    )

    tg_info = push_to_telegram_storage(
        out_video,
        caption=f"🎬 {script_res.get('viral_title', 'AlphaFlow Render')} | Voice: {args.voice} ({tts_res['model_used']})",
    )

    summary = {
        "job_id": args.job_id,
        "status": "completed",
        "duration_sec": duration_sec,
        "tts_model_used": tts_res["model_used"],
        "output_video": str(out_video),
        "srt_file": str(srt_path),
        "viral_title": script_res.get("viral_title"),
        "telegram": tg_info,
    }
    if args.webhook_url:
        try:
            req = urllib.request.Request(
                args.webhook_url,
                data=json.dumps(summary).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            print(f"[AlphaFlow CLI] Webhook callback warning: {exc}")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AlphaFlow Zero-Cost CLI & GitHub Actions Worker")
    parser.add_argument("--job-id", default="cli_job_01")
    parser.add_argument("--url", default="")
    parser.add_argument("--video-file", default="")
    parser.add_argument("--mode", default="recap", choices=["recap", "dubbing", "subtitle"])
    parser.add_argument("--voice", default="Charon")
    parser.add_argument("--gemini-tts-model", default="auto_duration")
    parser.add_argument("--aspect-ratio", default="9:16")
    parser.add_argument("--target-language", default="Burmese (Myanmar Language)")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--enhancement", default="none")
    parser.add_argument("--custom-script", default="")
    parser.add_argument("--use-zoom", action="store_true", default=True)
    parser.add_argument("--webhook-url", default="")
    parsed_args = parser.parse_args()
    asyncio.run(run_cli_job(parsed_args))
