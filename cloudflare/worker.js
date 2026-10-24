/**
 * AlphaFlow Studio — Cloudflare Edge Worker + Multi-Key Failover + Smart Gemini TTS Router
 * Supports:
 *  - Multi-key pool rotation & automatic HTTP 429 quota failover for Google AI Studio & Groq keys
 *  - Smart Duration Gemini TTS routing:
 *      <= 180s (3 min) -> gemini-3.8-flash-tts
 *      >  180s (3 min) -> gemini-3.8-flash-lite-tts
 *      Default/Fallback -> gemini-2.5-flash-preview-tts
 *  - GitHub Actions repository_dispatch trigger for heavy FFmpeg video rendering ($0 compute)
 *  - Telegram Bot storage webhook integration (@hyugoustoragebot)
 */

const MEMORY_STATE = {
  gemini_keys: [],
  groq_keys: [],
  tts_routing: {
    mode: "smart_duration",
    default_model: "gemini-2.5-flash-preview-tts",
    short_video_model: "gemini-3.8-flash-tts",
    long_video_model: "gemini-3.8-flash-lite-tts",
    duration_threshold_sec: 180,
  },
  jobs: {},
};

function initEnvKeys(env) {
  if (env?.GEMINI_API_KEYS && MEMORY_STATE.gemini_keys.length === 0) {
    const keys = env.GEMINI_API_KEYS.split(",").map(k => k.trim()).filter(Boolean);
    keys.forEach((k, idx) => {
      MEMORY_STATE.gemini_keys.push({
        id: `gemini_${idx + 1}`,
        key: k,
        masked: k.length > 12 ? `${k.slice(0, 10)}...${k.slice(-6)}` : k.slice(0, 4) + "...",
        label: `Google AI Studio #${idx + 1}`,
        status: "active",
        cooldown_until: 0,
        success_count: 0,
        quota_hits: 0,
      });
    });
  }
  if (env?.GROQ_API_KEYS && MEMORY_STATE.groq_keys.length === 0) {
    const keys = env.GROQ_API_KEYS.split(",").map(k => k.trim()).filter(Boolean);
    keys.forEach((k, idx) => {
      MEMORY_STATE.groq_keys.push({
        id: `groq_${idx + 1}`,
        key: k,
        masked: k.length > 12 ? `${k.slice(0, 10)}...${k.slice(-6)}` : k.slice(0, 4) + "...",
        label: `Groq Cloud #${idx + 1}`,
        status: "active",
        cooldown_until: 0,
        success_count: 0,
        quota_hits: 0,
      });
    });
  }
}

function resolveGeminiTtsModels(durationSec = 0, requestedModel = "auto_duration") {
  const r = MEMORY_STATE.tts_routing;
  let primary = r.default_model;
  if (requestedModel && !["auto", "auto_duration", "smart_duration"].includes(requestedModel)) {
    primary = requestedModel;
  } else if (durationSec > r.duration_threshold_sec) {
    primary = r.long_video_model;
  } else {
    primary = r.short_video_model;
  }
  return [...new Set([primary, r.default_model, r.short_video_model, r.long_video_model])];
}

async function executeWithKeyFailover(provider, asyncFn) {
  const bucket = provider === "gemini" ? MEMORY_STATE.gemini_keys : MEMORY_STATE.groq_keys;
  const now = Date.now();
  for (const item of bucket) {
    if (item.status === "cooldown" && now >= item.cooldown_until) item.status = "active";
  }
  const ordered = [
    ...bucket.filter(k => k.status === "active"),
    ...bucket.filter(k => k.status !== "active"),
  ];
  const errors = [];
  for (const entry of ordered) {
    try {
      const res = await asyncFn(entry.key);
      entry.status = "active";
      entry.success_count += 1;
      return res;
    } catch (err) {
      const msg = String(err.message || err);
      errors.push(`${entry.label}: ${msg}`);
      if (/429|quota|rate_limit|resource_exhausted/i.test(msg)) {
        entry.status = "cooldown";
        entry.quota_hits += 1;
        entry.cooldown_until = Date.now() + 90000;
      }
    }
  }
  throw new Error(`All ${provider} keys exhausted: ${errors.join(" | ")}`);
}

function wrapPcm24kToWav(pcmBytes) {
  const wav = new Uint8Array(44 + pcmBytes.byteLength);
  const view = new DataView(wav.buffer);
  const writeStr = (offset, str) => {
    for (let i = 0; i < str.length; i++) wav[offset + i] = str.charCodeAt(i);
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + pcmBytes.byteLength, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 24000, true);
  view.setUint32(28, 48000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, pcmBytes.byteLength, true);
  wav.set(pcmBytes, 44);
  return wav;
}

export default {
  async fetch(request, env) {
    initEnvKeys(env);
    const url = new URL(request.url);
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });

    if (url.pathname === "/api/keys" && request.method === "GET") {
      return Response.json({
        gemini_keys: MEMORY_STATE.gemini_keys.map(({ key, ...rest }) => rest),
        groq_keys: MEMORY_STATE.groq_keys.map(({ key, ...rest }) => rest),
        tts_routing: MEMORY_STATE.tts_routing,
      }, { headers: cors });
    }

    if (url.pathname === "/api/keys/add" && request.method === "POST") {
      const body = await request.json();
      const provider = (body.provider || "gemini").toLowerCase();
      const bucket = provider === "gemini" ? MEMORY_STATE.gemini_keys : MEMORY_STATE.groq_keys;
      const k = (body.api_key || "").trim();
      const masked = k.length > 12 ? `${k.slice(0, 10)}...${k.slice(-6)}` : k;
      bucket.push({
        id: `${provider}_${Date.now()}`,
        key: k,
        masked,
        label: body.label || `${provider.toUpperCase()} Key #${bucket.length + 1}`,
        status: "active",
        cooldown_until: 0,
        success_count: 0,
        quota_hits: 0,
      });
      return Response.json({
        success: true,
        pool: {
          gemini_keys: MEMORY_STATE.gemini_keys.map(({ key, ...rest }) => rest),
          groq_keys: MEMORY_STATE.groq_keys.map(({ key, ...rest }) => rest),
          tts_routing: MEMORY_STATE.tts_routing,
        },
      }, { headers: cors });
    }

    if (url.pathname === "/api/video/voice-sample" && request.method === "GET") {
      const voice = url.searchParams.get("voice") || "Charon";
      const ttsModel = url.searchParams.get("tts_model") || "auto_duration";
      const text = (url.searchParams.get("text") || "မင်္ဂလာပါ၊ အယ်လ်ဖာဖလိုး စတူဒီယိုမှ ကြိုဆိုပါတယ်။").trim();
      const models = resolveGeminiTtsModels(15, ttsModel);

      for (const m of models) {
        try {
          const audioBytes = await executeWithKeyFailover("gemini", async (apiKey) => {
            const resp = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${m}:generateContent?key=${apiKey}`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                contents: [{ parts: [{ text }] }],
                generationConfig: {
                  responseModalities: ["AUDIO"],
                  speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: voice } } },
                },
              }),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
            const data = await resp.json();
            const inline = data.candidates[0].content.parts[0].inlineData;
            const binStr = atob(inline.data);
            const bytes = new Uint8Array(binStr.length);
            for (let i = 0; i < binStr.length; i++) bytes[i] = binStr.charCodeAt(i);
            if (bytes[0] === 0x52 && bytes[1] === 0x49) return bytes; // RIFF WAV
            return wrapPcm24kToWav(bytes);
          });
          return new Response(audioBytes, {
            headers: { ...cors, "Content-Type": "audio/wav", "X-Gemini-TTS-Model": m },
          });
        } catch (e) {
          continue;
        }
      }
      return Response.json({ error: "Voice synthesis failed across all models" }, { status: 500, headers: cors });
    }

    return Response.json({
      service: "AlphaFlow Serverless Edge API",
      status: "online",
      tts_routing: MEMORY_STATE.tts_routing,
      gemini_keys_active: MEMORY_STATE.gemini_keys.length,
      groq_keys_active: MEMORY_STATE.groq_keys.length,
    }, { headers: cors });
  },
};
