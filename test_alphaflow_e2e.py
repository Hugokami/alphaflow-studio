import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from backend.key_pool import key_pool
from backend.voices import VOICE_CATALOG
from backend.tts_engine import synthesize_voice
from backend.pipeline import (
    JOBS_DIR,
    probe_media_info,
    transcribe_with_groq_whisper,
    generate_recap_or_dub_script,
    write_srt_file,
    render_final_video,
)


async def main() -> None:
    print("=== 1. VERIFYING MULTI-KEY POOL & SMART TTS DURATION ROUTER ===")
    status = key_pool.get_public_status()
    print(f"Loaded Gemini Keys: {len(status['gemini_keys'])} | Groq Keys: {len(status['groq_keys'])}")
    assert len(status["gemini_keys"]) >= 4, "Expected at least 4 Google AI Studio keys"
    # Groq keys optional when Gemini audio transcription fallback is active
    if status.get("groq_keys"):
        print(f"Groq Keys present: {len(status['groq_keys'])}")
    assert len(VOICE_CATALOG) == 45, f"Expected 45 voices, got {len(VOICE_CATALOG)}"

    # Verify Smart Duration Routing rules:
    # Under 3 min (<=180s) -> gemini-3.8-flash-tts
    short_route = key_pool.resolve_gemini_tts_models(video_duration_sec=120.0, requested_model="auto_duration")
    # Over 3 min (>180s) -> gemini-3.8-flash-lite-tts
    long_route = key_pool.resolve_gemini_tts_models(video_duration_sec=240.0, requested_model="auto_duration")
    # Explicit default -> gemini-2.5-flash-preview-tts
    default_route = key_pool.resolve_gemini_tts_models(
        video_duration_sec=120.0, requested_model="gemini-2.5-flash-preview-tts"
    )
    print("Short video (120s <= 3m) route:", short_route)
    print("Long video  (240s >  3m) route:", long_route)
    print("Explicit default 2.5 TTS route:", default_route)
    assert short_route[0] == "gemini-3.8-flash-tts"
    assert long_route[0] == "gemini-3.8-flash-lite-tts"
    assert default_route[0] == "gemini-2.5-flash-preview-tts"

    # Verify automatic quota failover when key #1 hits HTTP 429
    calls = []

    def _simulated_quota_exhaustion(api_key: str) -> str:
        calls.append(api_key[:14])
        if len(calls) == 1:
            raise RuntimeError("HTTP 429 RESOURCE_EXHAUSTED: Quota exceeded for key #1")
        return f"Success on fallback key {api_key[:14]}..."

    failover_res = key_pool.execute_with_failover("gemini", _simulated_quota_exhaustion, cooldown_sec=30)
    print("Failover test result:", failover_res, "| Keys tried:", calls)
    assert len(calls) == 2, "Should have failed over from key #1 to key #2"
    key_pool.reset_cooldowns("gemini")

    print("\n=== 2. RUNNING FULL END-TO-END VIDEO RECAP PIPELINE TEST ===")
    test_dir = JOBS_DIR / "e2e_verification_job"
    test_dir.mkdir(parents=True, exist_ok=True)

    # Create a 5-second sample input video with English speech so Groq Whisper has real speech to transcribe
    en_speech_mp3 = test_dir / "en_input.mp3"
    import edge_tts

    await edge_tts.Communicate(
        "In a hidden cybernetic city, a young engineer discovers an ancient artificial intelligence that can bend time.",
        "en-US-AndrewMultilingualNeural",
    ).save(str(en_speech_mp3))

    src_video = test_dir / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=5:size=1280x720:rate=24",
            "-i",
            str(en_speech_mp3),
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(src_video),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    meta = probe_media_info(src_video)
    print("Created source video:", meta)

    # Step A: Groq Whisper Large v3 Turbo STT
    transcript = transcribe_with_groq_whisper(src_video, test_dir)
    print("Groq Whisper STT transcript:", transcript.get("text"))
    assert transcript.get("text"), "Whisper transcript should not be empty"

    # Step B: Multi-key Gemini 2.5 Flash / Groq Llama-3.3-70B Burmese Recap Script
    script_data = generate_recap_or_dub_script(
        transcript_data=transcript,
        video_duration_sec=meta["duration"],
        processing_mode="recap",
        target_language="Burmese (Myanmar Language)",
    )
    print("Generated Burmese Recap Title:", script_data.get("viral_title"))
    print("Generated Burmese Narration:", script_data.get("full_narration"))

    srt_path = test_dir / "captions.srt"
    write_srt_file(script_data.get("captions", []), srt_path)

    # Step C: Synthesize Voice using Smart Duration Routing (5s <= 180s -> gemini-3.8-flash-tts)
    tts_res = await synthesize_voice(
        text=script_data["full_narration"],
        voice_identifier="Charon",
        work_dir=test_dir,
        video_duration_sec=meta["duration"],
        requested_gemini_model="auto_duration",
        speed=1.0,
        enhancement="studio",
    )
    print("Synthesized TTS Audio:", tts_res["audio_path"], "| Model Used:", tts_res["model_used"])
    assert tts_res["audio_path"].exists() and tts_res["audio_path"].stat().st_size > 1000

    # Step D: Render Final Anti-ContentID 9:16 Video with FFmpeg
    final_mp4 = test_dir / "final_output.mp4"
    render_final_video(
        source_video=src_video,
        voice_audio=tts_res["audio_path"],
        srt_path=srt_path,
        out_video=final_mp4,
        options={
            "aspect_ratio": "9:16",
            "use_zoom": True,
            "subtitle_enabled": True,
            "subtitle_type": "encode",
            "subtitle_font_size": 52,
            "sfx_only": True,
            "auto_color": True,
        },
        media_info=meta,
    )
    final_meta = probe_media_info(final_mp4)
    print("Rendered Final 9:16 Video:", final_mp4, "| Info:", final_meta)
    assert final_mp4.exists() and final_mp4.stat().st_size > 10000
    assert final_meta["width"] == 1080 and final_meta["height"] == 1920
    print("\n✅ ALL END-TO-END VERIFICATION TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(main())
