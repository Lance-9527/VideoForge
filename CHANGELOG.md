# Changelog

All notable changes to VideoForge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note:** This project is being open-sourced after ~32 batches of private evolution.
> This changelog starts from the first **public** release. Earlier history is captured
> in the `git log` of the first commit and the project structure itself.

## [1.3.0] - 2026-10-16

### Added
- **MiniMax Hailuo-03 (H3) / H3 Max 视频参考支持**：白膜/Previz 视频可作为
  `video_reference` 喂给海螺 H3，让模型按白膜的运镜/节奏/走位渲染实拍；
  旧模型（Hailuo-02 / 2.3 / 01）保持原样，给清晰中文错误
  「请换成 MiniMax-Hailuo-03 或 H3 Max」而不是静默丢
- **通用 `video_url` 字段**（`VideoGenRequest`）：5 个 adapter
  都能用，下游是否真把它当 video_reference 走，由各 adapter 自身按
  能力表判断（护栏而不是默默丢）
- **场景 `video_reference` 自动联动**：`generate_scene_video`
  自动从场景的 `reference_features.video_reference.path` 读取白膜
  视频并喂给 adapter —— 不再需要用户手填
- **OpenRouter LLM provider**（`backend/core/llm.py`）：聚合 OpenAI
  兼容的免费模型（标 `:free` 后缀，如 `nex-agi/nex-n2.5-mini:free`），
  配套 `.env.example` 加 `OPENROUTER_API_KEY`
- **Seedance 2.0 via token6688 网关**（`backend/core/adapters/seedance_t6.py`）：
  独立 `seedance_t6` provider，BASE_URL 走 token6688 网关
  (`https://agentai.token6688.com/api/v3`)，key 走 settings，绝不写进源码
- **BRAND-PROMPT.md**：跨项目/跨 agent 调出图/出视频的统一视觉 DNA
  文档（BASE_STYLE / CHARACTER_SIGNATURE / SCENE_CARD / SHOT_7ELEMENTS
  / QUALITY_TAIL / NEGATIVE_PROMPT 六段）
- **OpenRouter 别名**：`openrouter` / `or` / `nex` / `nex-agi` 都能
  识别为 OpenRouter provider

### Changed
- 文档：`backend/scripts/test_seedance_t6.py` 真实可跑的 Seedance t6 测试
  脚本（带 ASR/timeout/重试）

### Notes
- 这是开源后的**第二次公开 release**。第一次（1.2.0）只标了"开放源码
  + 文档化"，本次增加**真实功能**：H3 视频参考 + OpenRouter + token6688
  Seedance。Lance 已在场景 B（村口午后，9 秒白膜）实测 H3 路径生效，
  validate_request 通过，护栏对旧模型清晰报错，0 元付费 API 调用即
  跑通整条管线。


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