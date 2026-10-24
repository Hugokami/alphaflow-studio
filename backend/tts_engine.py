import asyncio
import base64
import io
import json
import subprocess
import shutil
import urllib.request
import urllib.error
import wave
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import edge_tts

from .key_pool import key_pool
from .voices import resolve_voice


def _pcm24k_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    """Wraps raw 16-bit mono signed PCM bytes in a standard RIFF WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _sanitize_verbatim_tts_text(text: str) -> str:
    """
    Enforces the Pure TTS Speech Delivery Invariant:
    Strips any accidental style/direction prefixes so only verbatim narration is sent.
    """
    cleaned = text.strip()
    for bad_prefix in (
        "Say expressive realist burmese narration:",
        "Say expressive realist narration:",
        "Narrator:",
        "Voiceover:",
        "Burmese narration:",
    ):
        if cleaned.lower().startswith(bad_prefix.lower()):
            cleaned = cleaned[len(bad_prefix) :].strip()
    return cleaned


def synthesize_gemini_tts_sync(
    text: str,
    voice_name: str,
    out_path: Path,
    video_duration_sec: float = 0.0,
    requested_model: Optional[str] = None,
) -> Tuple[Path, str]:
    """
    Synthesizes speech using Google AI Studio Gemini TTS models with:
    1) Smart Duration Model Routing:
       - <= 180s (3 min): `gemini-3.8-flash-tts`
       - >  180s (3 min): `gemini-3.8-flash-lite-tts`
       - Default / Fallback: `gemini-2.5-flash-preview-tts`
    2) Automatic Multi-Key Failover across all configured Google AI Studio keys.
    Returns (output_wav_path, model_used).
    """
    verbatim_text = _sanitize_verbatim_tts_text(text)
    if not verbatim_text:
        raise ValueError("TTS text cannot be empty")

    models_to_try = key_pool.resolve_gemini_tts_models(
        video_duration_sec=video_duration_sec,
        requested_model=requested_model,
    )

    last_err: Optional[Exception] = None
    for model_name in models_to_try:
        def _call_single_key(api_key: str) -> Tuple[bytes, str]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": verbatim_text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": voice_name,
                            }
                        }
                    },
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as he:
                err_body = he.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {he.code} on {model_name}: {err_body[:240]}")

            candidates = body.get("candidates") or []
            if not candidates:
                raise RuntimeError(f"No audio candidates returned from {model_name}")
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts or "inlineData" not in parts[0]:
                raise RuntimeError(f"Missing inlineData in {model_name} response")
            inline = parts[0]["inlineData"]
            raw_bytes = base64.b64decode(inline["data"])
            mime_type = inline.get("mimeType", "")
            return raw_bytes, mime_type

        try:
            raw_audio, mime_type = key_pool.execute_with_failover("gemini", _call_single_key)
            # Check if response is already a RIFF WAV container (Gemini 3.8 TTS returns audio/wav)
            if raw_audio[:4] == b"RIFF" or "wav" in mime_type.lower():
                wav_bytes = raw_audio
            else:
                # Gemini 2.5 TTS returns raw 24kHz 16-bit mono PCM (audio/L16;codec=pcm;rate=24000)
                wav_bytes = _pcm24k_to_wav_bytes(raw_audio, sample_rate=24000)

            out_path.write_bytes(wav_bytes)
            return out_path, model_name
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(f"Gemini TTS synthesis failed across all models {models_to_try}: {last_err}")


async def synthesize_edge_tts_async(
    text: str,
    voice_id: str,
    out_path: Path,
    pitch_hz: int = 0,
) -> Path:
    """Synthesizes speech using free Microsoft Edge Neural TTS (supports Burmese + MultilingualNeural)."""
    verbatim_text = _sanitize_verbatim_tts_text(text)
    pitch_str = f"{pitch_hz:+d}Hz"
    communicate = edge_tts.Communicate(verbatim_text, voice_id, pitch=pitch_str)
    await communicate.save(str(out_path))
    return out_path


def get_ffmpeg_exe() -> Optional[str]:
    """Returns path to system ffmpeg binary or bundled imageio-ffmpeg static executable."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return None


def postprocess_voice_audio(
    in_audio: Path,
    out_audio: Path,
    speed: float = 1.0,
    pause_mode: str = "remove",
    enhancement: str = "none",
) -> Path:
    """
    Applies speed adjustment, silence/pause stripping (`pause_mode == 'remove'`),
    and TeamAlpha-compatible + upgraded Voice Version audio enhancement:
      - `deepfilter` (Version 1 Crisp): High-clarity AI/FFT speech denoise + presence boost
      - `studio`     (Version 2 Clear): Balanced broadcast compressor + dynamic normalizer
      - `none`       (Version 3 Warm):  Gentle warm low-pass + smooth normalization
    """
    ffmpeg_bin = get_ffmpeg_exe()
    if not ffmpeg_bin:
        shutil.copy2(in_audio, out_audio)
        return out_audio

    filters = []

    # 1. Pause removal (strips unnatural TTS inter-sentence pauses)
    if pause_mode == "remove":
        filters.append("silenceremove=start_periods=1:start_silence=0.05:start_threshold=-48dB:stop_periods=-1:stop_duration=0.18:stop_threshold=-48dB")

    # 2. Voice Speed (0.85x to 2.0x)
    clamped_speed = max(0.5, min(2.0, float(speed or 1.0)))
    if abs(clamped_speed - 1.0) > 0.01:
        filters.append(f"atempo={clamped_speed:.3f}")

    # 3. Audio Enhancement Version
    if enhancement == "deepfilter":
        # Version 1: Crisp & Bright
        filters.append("highpass=f=85,afftdn=nf=-25,equalizer=f=3500:t=q:w=1.2:g=3.5,acompressor=threshold=-18dB:ratio=3:attack=5:release=50,dynaudnorm=f=150:g=13")
    elif enhancement == "studio":
        # Version 2: Studio Clear & Balanced
        filters.append("highpass=f=75,acompressor=threshold=-16dB:ratio=2.5:attack=8:release=70,dynaudnorm=f=150:g=11")
    else:
        # Version 3: Warm & Mellow (default)
        filters.append("highpass=f=65,lowpass=f=12000,dynaudnorm=f=200:g=11")

    filter_chain = ",".join(filters)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(in_audio),
        "-filter:a",
        filter_chain,
        "-ar",
        "44100",
        "-b:a",
        "96k",
        str(out_audio),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        shutil.copy2(in_audio, out_audio)
    return out_audio


async def synthesize_voice(
    text: str,
    voice_identifier: str,
    work_dir: Path,
    video_duration_sec: float = 0.0,
    requested_gemini_model: Optional[str] = None,
    speed: float = 1.0,
    pitch_hz: int = 0,
    pause_mode: str = "remove",
    enhancement: str = "none",
) -> Dict[str, Any]:
    """
    Unified entry point for all 45 voices (15 Microsoft + 30 Gemini) with
    automatic duration-based model routing, multi-key failover, and fallback to ThihaNeural if all keys fail.
    """
    voice_spec = resolve_voice(voice_identifier)
    raw_path = work_dir / f"tts_raw_{voice_spec['id']}.wav"
    final_path = work_dir / f"tts_final_{voice_spec['id']}.mp3"
    model_used = "edge-tts"

    if voice_spec["provider"] == "gemini":
        try:
            _, model_used = await asyncio.to_thread(
                synthesize_gemini_tts_sync,
                text,
                voice_spec["id"],
                raw_path,
                video_duration_sec,
                requested_gemini_model,
            )
        except Exception as gemini_err:
            # Ultimate zero-downtime fallback to Microsoft Burmese ThihaNeural if all Gemini keys are exhausted
            raw_path = work_dir / "tts_raw_fallback.mp3"
            await synthesize_edge_tts_async(text, "my-MM-ThihaNeural", raw_path, pitch_hz=pitch_hz)
            model_used = f"edge-tts-fallback ({str(gemini_err)[:80]})"
    else:
        raw_path = work_dir / f"tts_raw_{voice_spec['id']}.mp3"
        await synthesize_edge_tts_async(text, voice_spec["id"], raw_path, pitch_hz=pitch_hz)
        model_used = f"edge-tts ({voice_spec['id']})"

    await asyncio.to_thread(
        postprocess_voice_audio,
        raw_path,
        final_path,
        speed,
        pause_mode,
        enhancement,
    )

    canonical_voice = work_dir / "final_voice.mp3"
    try:
        if final_path.exists():
            shutil.copy2(final_path, canonical_voice)
    except Exception:
        pass

    return {
        "audio_path": canonical_voice if canonical_voice.exists() else final_path,
        "voice": voice_spec,
        "model_used": model_used,
    }
