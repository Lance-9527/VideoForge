# Limitations

> What is **intentionally not** there yet, and why. This is the honest list — when something looks "missing" it usually means: "we know about it, here's the plan".

## Features that don't exist (yet)

### Stock video search
- **Status**: NOT IMPLEMENTED
- **Why it matters**: currently the only way to get a visual is to call a paid video API. This makes VideoForge useless for anyone without API keys.
- **Plan**: add Pexels / Pixabay / Coverr search to `core/adapters/` as a `StockAdapter` that bypasses the "no_api_key → empty project" failure mode.
- **Workaround**: none in-app; users need to bring their own API key.

### Subtitle alignment
- **Status**: rough SRT (action-timeline → text)
- **Why it matters**: `faster-whisper` is in `requirements.txt` but commented out. Subtitles currently don't track actual spoken words.
- **Plan**: enable `faster-whisper>=1.0`, add word-level timestamps, regenerate `generate_srt_from_timeline` from real audio.
- **Workaround**: use the "rough" subtitles; they're not perfect but readable.

### Asset state machine
- **Status**: schema-implied only (`asset.status` field exists in DB); no enforcement
- **Plan**: implement `proposed → reference_ready → approved → deprecated` with UI gates.
- **Reference**: see `Hell-Grind-AIGC-Skill/asset-lifecycle.md` (not vendored).

### 5-tuple model records
- **Status**: not implemented — currently `candidates` is one big JSON
- **Plan**: 4 separate tables (`prompts`, `batches`, `generations`, `selections`, `iterations`) with FKs
- **Why**: 115k generations vs 7k unique prompts in reference data shows the value of this separation

### 9-dimension shot selection scoring
- **Status**: not implemented — currently we pick the latest candidate
- **Plan**: scoring with hard gates that can't be averaged away
- **Reference**: `Hell-Grind-AIGC-Skill/selection-scoring.md` (not vendored)

### Continuity matrix (13 dimensions)
- **Status**: only `aspect_ratio` enforced; other 12 dims (lighting / character pose / camera height / color temperature / lens / motion / …) not checked
- **Plan**: add `continuity.py:continuity_check(shot_in, shot_out)` returning structured deltas, fail-fast on hard mismatches

### Audio-cues table
- **Status**: not implemented
- **Plan**: 7 sound classes (dialogue / foley / ambience / music / sfx / impact / silence) with mix priority + continuity key

### Time-budget table
- **Status**: not implemented
- **Plan**: per-shot duration budget (read state 0.3–0.8s / full action 1–3s / impact 0.3–1s) — check "does this shot fit" **before** paying the API

### Built-in BGM library
- **Status**: not implemented — BGM is user-supplied via `bgm_path` form field
- **Plan**: bundle ~30 royalty-free tracks in `data/bgm/`, add "random pick matching mood" heuristic
- **Reference**: MPT does this in `resource/songs/`

### Cross-platform publish
- **Status**: not implemented — `TaskState.PUBLISH` enum exists but no upload code
- **Plan**: Douyin / Bilibili / YouTube upload modules (each is its own SDK pain)

### Single-instance lock
- **Status**: not implemented — two launchers can run on different ports simultaneously
- **Plan**: `msvcrt` lockfile or `socket.bind()` on a fixed port

### Crash-recovery queue
- **Status**: not implemented — task_queue is in-memory, restart = lost tasks
- **Plan**: persist queue to DB, recover on startup

### Pre-flight generation gate
- **Status**: implemented as **warnings** only (ref-check); not hard errors
- **Plan**: user choice — currently we don't block; but offer a "strict mode" toggle

## Known bugs / quirks

### "screen stutter / flicker" when VideoForge is running and you launch another GPU app
- **Root cause**: rate-limited. Fixed in 1.2.0 via `@lru_cache` on FFmpeg path discovery.
- **If you see this on 1.2.0+**: file a bug with `~/.videoforge/videoforge.log` attached.

### `scriptgeneration` sometimes returns inconsistent total duration
- **Status**: in 1.2.0 we **enforce** total duration with `scriptplan.fit_scenes()`. Excess scenes are dropped, not silently truncated.
- **Known quirk**: 5-second target drops 4 of 5 scenes that LLM wrote. To keep all scenes, ask for longer total duration.

### Provider `ratio` must be `"16:9"` not `"16x9"`
- **Status**: fixed in 1.2.0 (`core.aspect.normalize_ratio()`)
- **If you see aspect mismatch on a fresh project**: update `seedance.py` / `jimeng.py` if you've modified them

### TTS "回退到默认女声" for a character whose name has spaces
- **Status**: fixed in 1.2.0 (`voicecast._plan_key_for` normalizes character names)
- **If you see it after 1.2.0**: check the character card has a `gender` field (or role-specific fallback)

## Things we explicitly decided NOT to do

- **Built-in music composition** — out of scope; use external tools
- **Auto-publishing to social without user approval** — too risky
- **Cloud sync of projects** — local-first by design
- **Web-based frontend (browser-only mode is a fallback)** — desktop window is the primary
- **Multi-user / team collaboration** — single-user desktop tool
- **Subscription-based feature gating** — open-source, no upsell

## What "looks missing" but is intentional

- **`core/llm.py` has if/else branches instead of a registry** — known inconsistency vs `core/adapters/` and `core/voice/` which have registries. TODO.
- **No `pytest`** — tests are run as `python tests/test_*.py`. We have 80+ tests, all work, but it's not "standard pytest". Migration is on the roadmap.

## How to read the project's history

The project went through ~32 batches of private development. The full history lives in the first commit of this repo (`git log -p --reverse`). Highlights:

1. **0–10**: Initial extraction from PyInstaller bundle, basic FastAPI structure
2. **11–20**: Multi-provider video adapters, FFmpeg postprocess pipeline
3. **21–30**: LLM robustness, voice casting, script quality gates
4. **31–32**: Aspect-ratio correctness, reference-image integrity, seedance 2.5

The CHANGELOG.md has version-level summaries.