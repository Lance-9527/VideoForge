# Providers

> Per-provider notes — capabilities, costs, gotchas. Keep this in sync with `backend/core/adapters/` and `backend/core/voice/`.

## Video models

### MiniMax (`minimax`)
- **Status**: Cheapest default. 720p and 1080p available.
- **API key**: `MINIMAX_API_KEY`
- **Gotchas**: model name `speech-*` for TTS, **`M3` is a language model, not TTS** — see issue #31 in CHANGELOG.md for the full story.
- **Caps**: 4s–15s, 480p / 720p / 1080p

### Hailuo (`hailuo`)
- **API key**: `HAILUO_API_KEY`
- **Caps**: 6s/10s at 768p, **6s only at 1080p** — see `core/model_catalog.py` `caps_for("hailuo", "hailuo-2.3")`
- **Gotcha**: don't ask for 1080p + 10s; silently truncated to 6s.

### Seedance 2.0 / 2.0-fast / 2.5
- **API key**: `SEEDANCE_API_KEY`
- **Caps**: 2.0 / 2.5 → 4s–15s; **2.0-fast does NOT support 1080p**; 2.0 series **does NOT accept real-person-face reference images** (rejected by platform)
- **Gotchas**: 
  - Body params `ratio` / `resolution` / `duration` must be **top-level** — putting them in `"parameters": {...}` is the old (weakly-validated) way and gets ignored
  - `ratio` values are enumerated: `"16:9" / "4:3" / "1:1" / "3:4" / "9:16" / "21:9" / "adaptive"` — NOT `"16x9"` (this was a real bug, see `seedance.py` old line 75)
  - `reference_image` / `first_frame` / `last_frame` are **three mutually-exclusive modes** — never mix

### Kling (`kling`)
- **API key**: `KLING_ACCESS_KEY` + `KLING_SECRET_KEY`
- **Caps**: 5s/10s
- **Gotchas**: long polls (~2 min) — task_queue.py handles this with 30s polling intervals

### Jimeng (`jimeng`)
- **API key**: `JIMENG_ACCESS_KEY` + `JIMENG_SECRET_KEY`
- **Caps**: 5s/10s

### Runway / Pika / Luma / Sora / WanX / CogVideoX
- See `backend/core/adapters/<name>.py` for current caps.
- Some have not been exercised against real responses (status = "ported, untested" in code).

## TTS providers

### Edge TTS (default, no key)
- **Voice catalog**: 22 zh-CN voices + others
- **Prosody**: supports `rate`, `volume`, `pitch(Hz)`, `emotion` (limited)
- **Gotcha**: SSML in some languages; `edge-tts>=6.1` required.

### SiliconFlow
- **API key**: `SILICONFLOW_API_KEY`
- **Voice catalog**: 8 voices (zh-CN), OpenAI-compatible
- **Prosody**: `speed` + `gain` only (no pitch, no emotion)
- **Gotcha**: lower-cost but no pitch control → can't tune dramatic delivery

### MiniMax TTS
- **API key**: `MINIMAX_API_KEY`
- **Voice catalog**: 12 voices, male/female
- **Prosody**: `speed`, `vol`, `pitch` (in Hz, NOT semitone — note: was hardcoded to 0 in old code, fixed)
- **Gotcha**: requires MiniMax subscription with TTS enabled

### DashScope
- **API key**: `DASHSCOPE_API_KEY`
- **Voice catalog**: 10 voices (Chinese)
- **Prosody**: `rate`, `volume` only (no pitch)

### Silent (placeholder)
- No audio output, just duration estimation (~4.2 chars/sec Chinese)
- Use for projects where you want to add music later

## LLMs

### MiniMax
- Default. `MINIMAX_API_KEY`. Strong Chinese + script generation.

### DeepSeek
- `DEEPSEEK_API_KEY`. Fast + good at structured output.

### OpenAI-compatible (Qwen / GLM / Kimi / Doubao)
- Each has its own API key env var. Set `base_url` in Settings if not using the default endpoint.

### Ollama (local)
- No API key, just `OLLAMA_BASE_URL=http://localhost:11434`
- Models: `llama3`, `qwen2`, etc.
- **Gotcha**: very slow on first call (model load) — expect 5–10s for the first request, then normal.

## How to add a new video provider

1. Create `backend/core/adapters/<name>.py` inheriting from `VideoAdapter` (see `kling.py`)
2. Register in `backend/core/adapters/__init__.py:_KNOWN_ADAPTERS` (or rely on `@register`)
3. Add model caps in `backend/core/model_catalog.py:caps_for(<provider>, <model>)`
4. Add to Settings UI (`frontend/index.html` providers list)
5. Add a probe in `tests/_probe_<name>.py` (dev-only, gitignored)

## How to add a new TTS

1. Create `backend/core/voice/<name>_tts.py` with `@register("name")`
3. Set `default_model` + `models` + `model_note` for UI
2. Test with `tests/_probe_<name>.py`

## Cost notes

This project tries to be **cost-aware** but is not a billing system. We do not estimate or enforce budget. Every TTS / video API call costs money. Use it on short projects first.

If you want to monitor spend, check your provider dashboards — we don't proxy anything.