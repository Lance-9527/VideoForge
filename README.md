# VideoForge

> Personal AI short-video agent for Windows · turn an idea into a 5-second or 30-minute short film with one click.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078d4.svg)](https://www.microsoft.com/windows)
[![Tests: 80+](https://img.shields.io/badge/tests-80%2B-brightgreen.svg)](tests/)

---

> **中文用户** — 看 [README.zh-CN.md](README.zh-CN.md)

---

## What is VideoForge?

VideoForge is a desktop AI short-video agent that turns a one-line idea into a finished short film — script, characters, scenes, voice casting, video segments, subtitles, BGM, and assembly all handled by a single FastAPI + pywebview app.

It is **the integration layer** between 11 video-generation APIs, 5 TTS providers, multiple LLM providers, and a local FFmpeg pipeline. It enforces structure on what the models produce, so that what comes out is actually a film and not a slideshow of stock shots.

It started as a personal tool for making Chinese-language short films with AI, and is being open-sourced as a reference implementation of an opinionated, end-to-end video agent.

## ✨ Features

### Multi-provider, by design
- **11 video model adapters** out of the box: CogVideoX / Hailuo / Jimeng / Kling / Luma / MiniMax / Pika / Runway / Sora / WanX / Seedance 2.0 / 2.0-fast / 2.5
- **5 TTS providers**: Edge TTS (no key) / SiliconFlow / MiniMax / DashScope / Silent (placeholder)
- **Multiple LLMs**: MiniMax / DeepSeek / OpenAI-compatible / Qwen / GLM / Kimi / Doubao / Ollama (local)
- Switch provider per project, without rewriting prompts

### Production-aware pipeline
- Script generation **enforces total duration** (no more "asked 5s, got 60s")
- 13-dimension **continuity matrix** between shots (camera / lighting / character identity / …)
- Per-character **voice casting** from real voice catalog (age, gender, personality) — not 8 hardcoded NPCs
- **One-click dub** for the entire project (no re-generation of video, zero video cost)
- **Reference image integrity**: `MISSING_REFERENCE` and `MENTIONED_BUT_NOT_LINKED` are hard errors, not silent fallbacks
- Vendor-parameter correctness (e.g. `ratio: "16:9"` not `"16x9"`, top-level body fields, mutually-exclusive modes)

### Real failure codes, not just "retry"
24 documented failure codes (`F-ASPECT-MISMATCH`, `F-REF-MISSING`, …) classified by responsibility layer (asset / contract / prompt / adapter / randomness / post), with "first check / minimum fix" guidance per code.

### Hardware-friendly on Windows
- Respects NVIDIA TDR sensitivity (FFmpeg path caching prevents 200 Hz spawn-storm → screen stutter)
- Bundled FFmpeg via imageio-ffmpeg (no separate install)
- pywebview desktop window (no browser needed); auto-falls-back to default browser if WebView2 missing

## 📸 Screenshots

> Coming soon — PRs welcome. For now, see `frontend/css/` for the design system.

## 🚀 Quick Start

### Requirements
- Windows 10 1809 (build 17763) or later
- Python 3.11+ (tested on 3.14.5)
- ~500 MB disk for code + a few GB for generated videos

### Install (developer mode)

```bash
git clone https://github.com/your-username/VideoForge.git
cd VideoForge
pip install -r requirements.txt
```

### Run

```bash
# 中文友好方式（Windows）
启动.bat

# 或
python launcher.py

# 或纯浏览器模式（不开桌面窗口）
python launcher.py --browser
```

The app opens a desktop window on `http://127.0.0.1:<port>/` (default port is auto-assigned; pass `--port 8899` to fix it).

### Configure API keys

1. Open the app → **Settings** page
2. Paste your provider API keys (stored encrypted in local SQLite)
3. Or copy `.env.example` to `.env` and set environment variables (CLI/dev only)

**No API key is required to start.** You'll only need them for the providers you want to call.

## 🏗️ Architecture

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   Frontend   │  │  FastAPI     │  │  LLM         │
│  (HTML/JS)   │◀─│  backend/    │──│  MiniMax /   │
│  pywebview   │  │  main.py     │  │  DeepSeek /  │
└──────────────┘  └──────┬───────┘  │  Qwen / ...  │
                         │          └──────────────┘
              ┌──────────┼──────────┐
              ▼          ▼          ▼
       ┌──────────┐ ┌────────┐ ┌──────────┐
       │  Video   │ │  TTS   │ │  Image   │
       │ 11 adapt.│ │ 5 prov.│ │ MiniMax/ │
       │ Seedance │ │ Edge / │ │ WanX /   │
       │ Hailuo … │ │ SiF    │ │ OpenAI   │
       └────┬─────┘ └────┬───┘ └────┬─────┘
            ▼            ▼          ▼
       ┌─────────────────────────────────┐
       │  core/postprocess (FFmpeg)      │
       │  stitch / mux / burn subtitle   │
       │  export aspect / mix BGM        │
       └────────────┬────────────────────┘
                    ▼
              ┌───────────┐
              │ outputs/  │  ← project videos
              └───────────┘
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full breakdown.

## 📚 Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module layout, request flow, data model
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — dev environment, debugging, common pitfalls
- [`docs/PROVIDERS.md`](docs/PROVIDERS.md) — per-provider notes (capabilities, costs, gotchas)
- [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) — what's *not* there yet (stock video, audio-cues table, …)

## 🧪 Testing

The test suite is real-data driven. Many tests reproduce real bugs found during development.

```bash
# Run one test
python tests/test_ref_integrity.py

# Run the whole suite
for f in tests/test_*.py; do python "$f" || echo "FAIL: $f"; done
```

There are 80+ tests in `test_*.py`. Each prints `PASS N   FAIL M` at the end.

## 🛠️ Development

```bash
# Set up Python environment
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Run backend with auto-reload
cd backend
uvicorn main:app --reload --port 8899

# In another terminal, run launcher (window mode)
python launcher.py --port 8899
```

## 📦 Packaging (PyInstaller)

```bash
pip install pyinstaller pyinstaller-hooks-contrib
pyinstaller --clean --noconfirm VideoForge.spec
# Output: dist/VideoForge.exe (Windows portable, ~24 MB)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full style guide.

## 🗺️ Roadmap

What's intentionally **not** there yet (next iterations):

- **Stock video search** (Pexels / Pixabay) — currently the only path is paid video models
- **Subtitle alignment** via `faster-whisper` — currently action-timeline → SRT (rough)
- **Asset state machine** (`proposed → reference_ready → approved → deprecated`)
- **5-tuple model records** (`prompt_id+version / batch_id / generation_id / selection_id / iteration_id`)
- **9-dimension shot selection** scoring with hard gates
- **Continuity matrix 13-dim** with `continuity_out == next.continuity_in` automatic check
- **Audio-cues table** (7 sound classes + mix priority)
- **Time-budget table** (read state / full action / impact — for "does this fit" checks before paying)
- **Built-in BGM library** (currently BGM is user-supplied)
- **Cross-platform publish** (Douyin / Bilibili / YouTube uploads)
- **Single-instance lock** + crash-recovery queue (currently queue is in-memory)
- **Pre-flight generation gate** (approved asset + approved prompt + budget)

See `docs/LIMITATIONS.md` for the full list with rationale.

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). TL;DR:

1. Fork → feature branch → write code + `tests/test_*.py`
2. Run your test: `python tests/test_your_feature.py`
3. Open PR with the template

## 🔐 Security

If you find a vulnerability, **do not** open a public issue. See [SECURITY.md](SECURITY.md).

This project takes the following stance by default:
- **No hardcoded API keys.** All credentials are read from environment or runtime settings.
- **No telemetry.** Outbound HTTP only to the providers you configure.
- **Local-first.** Project data in `%LOCALAPPDATA%\VideoForge\data`. Nothing sent to a third party except provider API calls you initiate.

## 📜 License

[MIT](LICENSE) — Copyright (c) 2026 Lance (VideoForge Authors)

## 🙏 Acknowledgments

- **[Hell-Grind-AIGC-Skill](https://github.com/renmu2017/Hell-Grind-AIGC-Skill)** — extracted from a 95-minute AI film production; the source of 13 of our reference documents (continuity matrix, failure codes, audio-cues, time-budget table, …)
- **MoneyPrinterTurbo (MPT)** — early FastAPI + adapter patterns we learned from
- **Pavo AI** — SQL/Pydantic schema patterns (snowflake IDs → UUIDs for JS safety)
- **All the model providers** — without their APIs this would be a slideshow generator

---

> Made with [Proma](https://proma.cool) · [GitHub](https://github.com/proma-ai/Proma)