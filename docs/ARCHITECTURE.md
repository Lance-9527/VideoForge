# Architecture

> **TL;DR** — Frontend (pywebview) → FastAPI (`main.py`) → `core/` business logic → `core/adapters/` + `core/voice/` provider modules → FFmpeg postprocess.

## Module layout

```
backend/
├── main.py             # 5,748 lines — FastAPI app + all HTTP endpoints
├── models.py           # Pydantic schemas (request/response/data models)
├── api/
│   └── chat.py         # /api/chat router (LLM conversation)
└── core/               # ALL business logic — no I/O at import time
    ├── db.py            # SQLite wrapper + settings table
    ├── llm.py           # LLM dispatcher (provider = if/else branches — TODO: registry)
    ├── llm_robust.py    # Retry / backoff / error sanitization / freeze_config
    ├── orchestrator.py  # Project lifecycle coordinator
    ├── task_queue.py    # Background async tasks (states: pending → … → success/failed)
    ├── pipeline.py      # Per-shot generation pipeline (image → video → audio → assembly)
    ├── postprocess/     # FFmpeg wrappers (stitch / mux / burn / aspect / BGM)
    ├── adapters/        # One file per VIDEO provider (kling / hailuo / seedance / …)
    │   ├── base.py      # VideoAdapter abstract base
    │   ├── kling.py     # 11 concrete adapters
    │   └── seedance.py  # ... etc
    ├── voice/           # TTS providers
    │   ├── base.py      # VoiceProvider + @register decorator (registry)
    │   ├── dispatcher.py# synthesize() / list_providers()
    │   ├── edge_tts.py  # ... etc (5 providers)
    │   └── minimax_tts.py
    ├── voicecast.py     # Per-character voice selection
    ├── voicecatalog.py  # Parse voice labels (age / personality tags)
    ├── scriptplan.py    # Enforce total duration in generated scripts
    ├── scriptaudit.py   # Script quality gate (S0–S11 checks)
    ├── scriptlines.py   # Script → shot dialogue backfill
    ├── videoprompt.py   # Per-provider prompt building (model-agnostic, vendor params separate)
    ├── shotplan.py      # Shot-level planning
    ├── dialogue.py      # Dialogue extraction from scripts
    ├── continuity.py    # 13-dim continuity matrix (planned)
    ├── aspect.py        # Aspect ratio normalization
    ├── seamfix.py       # Seam transition / padding
    ├── refcheck.py      # Reference image integrity (MISSING_REFERENCE / MENTIONED_BUT_NOT_LINKED)
    ├── failcodes.py     # 24 failure codes with responsibility layer
    ├── ffmpeg_manager.py# FFmpeg discovery + caching (lru_cache fix)
    ├── proc.py          # Safe subprocess wrapper (CREATE_NO_WINDOW + clean env)
    ├── imagegen.py      # Image generation (MiniMax / WanX / OpenAI compatible)
    ├── docimport.py      # Import script from PDF/DOCX
    ├── assets.py        # Asset extraction (characters / scenes from script)
    ├── model_catalog.py # Per-model capability table
    ├── prosody.py       # Per-line TTS prosody planning (rate / pitch / emotion)
    ├── promptaudit.py   # Pre-generation prompt audit
    └── duraledger.py    # Project duration ledger
```

## Request flow

### Project creation → final film

```
1. POST /api/projects
   └─ db.create_project() → returns project_id

2. POST /api/projects/{pid}/script/generate
   ├─ scriptplan.scene_count_for(total)         # decide scene count
   ├─ scriptplan.distribute(total, n)          # integer distribution
   └─ LLM (MiniMax/DeepSeek/...) → ScriptDraft
       └─ scriptaudit.audit(draft)              # S0–S11 quality gate
           ├─ if errors > 0: return warning + draft
           └─ if errors == 0: persist to DB

3. POST /api/projects/{pid}/shots/generate
   ├─ shotplan.plan_shots(script)
   └─ for each shot:
       ├─ imagegen.generate(character/scene refs) → image_path
       ├─ _try_api_video(shot, image_path)
       │   ├─ refcheck.check_shot_refs(shot)    # MISSING_REFERENCE / MENTIONED_BUT_NOT_LINKED
       │   ├─ videoprompt.build_video_prompt(...)  # model-agnostic prompt + vendor params
       │   └─ adapters[provider].generate(payload)
       │       └─ returns { video_path, audio_path, vendor_response, failcodes }
       └─ store as candidate (source=api | source=local)

4. POST /api/projects/{pid}/voice/dub
   ├─ voicecatalog.cast_from_catalog(...)       # age + gender + personality
   ├─ voicecast.cast_plan(...)                  # project-wide cast
   ├─ for each shot:
   │   ├─ scriptlines.recover_and_inject(...)   # backfill missing dialogue
   │   ├─ voicecast.dub_shot(...)               # per-line prosody
   │   └─ voice[provider].synthesize(line)
   └─ mux audio into shot video (postprocess._mux_clip_audio)

5. POST /api/projects/{pid}/compose
   ├─ postprocess.stitch_videos(shots)
   ├─ postprocess.burn_subtitles(video, srt)
   ├─ postprocess.mix_bgm(video, bgm_path)
   └─ postprocess.export_aspect_ratio(video, "16:9")
       └─ final.mp4
```

## Data model (SQLite)

```sql
projects (id, title, description, status, settings_json, created_at, updated_at)
scripts  (project_id, outline_json, scenes_json, total_duration, ...)
shots    (project_id, scene_number, shot_order, prompt_json, vendor_params_json,
          character_ids[], scene_id, status, ...)
candidates (shot_id, source, video_path, audio_path, failcodes, ...)
characters (project_id, name, role, age, gender, personality_json, ref_image_path)
scenes   (project_id, name, description, ref_image_path)
assets   (...)              -- generated images, reference images, BGM
settings (key, value)       -- global + provider config (incl. encrypted API keys)
tasks    (id, project_id, type, status, failed_stage, created_at, ...)
```

IDs are UUID4 (not snowflake) — see `models.py:new_id()` rationale (JS precision).

## Key invariants

- `pipeline.generate_shot()` is **idempotent at the candidate level**: re-running doesn't re-bill if a viable candidate exists.
- Provider adapters must not throw on missing API key; they return `{"error": "no_api_key"}` for the chat-test endpoint.
- `core/` modules must not do I/O at import time (lazy import `imageio_ffmpeg` etc).
- All FFmpeg subprocesses go through `core.proc.run_sync` with `CREATE_NO_WINDOW` (prevent 0xc0000142 on Windows).
- Generated videos that don't match the planned aspect ratio are **registered as candidates** (not silently discarded) — see `main.py` aspect-fatal branch.

## Where to add new things

| You want to… | File | Look at |
|---|---|---|
| Add a new LLM | `backend/core/llm.py` | Existing if/else chain |
| Add a new video model | `backend/core/adapters/<name>.py` | `kling.py` as template |
| Add a new TTS | `backend/core/voice/<name>_tts.py` | `edge_tts.py` as template |
| Add a new endpoint | `backend/main.py` | Search for `@app.get` / `@app.post` |
| Add a new pipeline stage | `backend/core/task_queue.py` | `TASK_HANDLERS` registry |
| Add a new check / gate | `backend/core/failcodes.py` | 24 existing codes |
| Add a new setting | `backend/core/db.py` | `settings` table is key-value |