# Changelog

All notable changes to VideoForge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note:** This project is being open-sourced after ~32 batches of private evolution.
> This changelog starts from the first **public** release. Earlier history is captured
> in the `git log` of the first commit and the project structure itself.

## [Unreleased]

### Planned
- Documented audio-cues table (7 sound classes + mix priority)
- Time-budget table (read state / full action / impact)
- Asset state machine (`proposed → reference_ready → approved → deprecated`)
- 9-dimension shot selection scoring
- 13-dimension continuity matrix
- Built-in stock video search (Pexels / Pixabay) — currently the only path is paid video models

## [1.2.0] - 2026-09-14

### Added
- **11 video model adapters**: CogVideoX / Hailuo / Jimeng / Kling / Luma / MiniMax / Pika / Runway / Sora / WanX / Seedance (2.0 / 2.0-fast / 2.5)
- **5 TTS providers**: Edge (no key) / SiliconFlow / MiniMax / DashScope / Silent
- True voice casting based on character age / gender / personality (replaces 8-NPC hardcoded pool)
- One-click dubbing for entire project
- Script generation with enforced total duration (no more "asked 5s, got 60s")
- Reference image integrity check (`MISSING_REFERENCE` + `MENTIONED_BUT_NOT_LINKED`)
- Vendor parameter correctness (`ratio: "16:9"` not `"16x9"`, top-level body fields)
- 24 documented failure codes with responsibility-layer attribution
- Cost-aware model fallback with explicit alerts

### Fixed
- First-frame / last-frame / reference_image modes were being silently mixed — now mutex-explicit
- One-line Reuse of paid API result instead of discarding (was losing real models' output on aspect-ratio mismatch)
- Per-character voice persistence across shots (was random per shot)

### Changed
- Hardware: NVIDIA driver TDR sensitivity now respected via FFmpeg path caching (avoids 200Hz spawn storms)
- Frontend: 一键配音 button shows real model used and roles cast

## [1.1.0] - 2026-09-13

### Added
- TTS module + voice task state machine
- LLM robustness: retry/backoff, error sanitization, freeze_config snapshots
- Voice dubbing UI
- Backend: 122 Python files, 3.6 MB source
- Frontend: 9 files (HTML/CSS/JS), 460 KB

### Fixed
- Frontend project switcher had silent no-op bug (`!d.data` was always true after unwrap)
- Chat send Enter-key broken (was accessing React-specific `nativeEvent` on raw DOM events)
- FFmpeg path self-healing: now overwrites stale `Desktop\VideoForge-app\...` paths

## [1.0.0] - 2026-09-10

### Added
- Initial public release (extracted from PyInstaller bundle)
- FastAPI backend + pywebview desktop window
- Script generation, shot planning, image generation, post-processing
- 11 video model adapters (initial commit)

[Unreleased]: https://github.com/lance/VideoForge/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/lance/VideoForge/releases/tag/v1.2.0
[1.1.0]: https://github.com/lance/VideoForge/releases/tag/v1.1.0
[1.0.0]: https://github.com/lance/VideoForge/releases/tag/v1.0.0