# Development

## Setup

### Prerequisites
- Windows 10 1809+ (Python 3.14.5 is what we test on, but 3.11+ should work)
- [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) (preinstalled on Windows 11; auto-falls-back to default browser if missing)
- ~500 MB free disk

### Clone + install

```bash
git clone https://github.com/your-username/VideoForge.git
cd VideoForge
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Run in two modes (dev loop)

```bash
# Terminal 1: backend with hot-reload
cd backend
uvicorn main:app --reload --port 8899

# Terminal 2: launcher (window mode, connects to backend)
cd ..
python launcher.py --port 8899
```

Open the desktop window. Make a backend code change → uvicorn auto-reloads → refresh the window.

## Debugging

### Logs

| What | Where |
|---|---|
| Backend stdout | terminal running `uvicorn` |
| Backend file log | `~/.videoforge/videoforge.log` (set by launcher.py) |
| PyInstaller windowed stdout | `~/.videoforge/stdout.log` |
| PyInstaller windowed stderr | `~/.videoforge/stderr.log` |
| Launcher crash traceback | `~/.videoforge/crash.log` |
| Per-shot generation data | DB table `candidates` |

### Common pitfalls

#### "Backend startup timeout" on cold start
- Cause: FFmpeg not in PATH and no imageio-ffmpeg found
- Fix: `pip install imageio-ffmpeg` (already in requirements). Or set `VIDEOFORGE_FFMPEG` env var to a real ffmpeg.exe

#### "0xc0000142" on first launch (PyInstaller exe)
- Cause: subprocess inherits a corrupt env that makes the bundled DLL fail to load
- Fix: already handled by `core/proc.py:run_sync()` (CREATE_NO_WINDOW + cleaned env). If you see this, check that nothing else is in `PATH` that could hijack FFmpeg

#### "screen stutter / flicker while testing"
- **This is a known symptom** if FFmpeg path caching is broken. See `core/ffmpeg_manager.py` for the `@lru_cache` fix. If you removed the cache, put it back.

#### Provider returns "no_api_key" on chat
- Open Settings, paste the key, save. Settings are encrypted in SQLite (`settings.llm_api_keys` JSON column)

#### Generated video comes back at the wrong aspect ratio
- Check `core/adapters/<provider>.py:model_catalog` — the per-model capability table may not have the right `ratio → resolution` mapping for the model you're using.
- Add a `caps_for(provider, model)` entry and rebuild.

### Live probes

The repo includes `_probe_*.py` files in `tests/` (gitignored from public repo) — these hit a running backend on `127.0.0.1:<port>` and dump results. Use them for "what does the API actually return" debugging:

```bash
python tests/_probe_real_video_prompt.py
python tests/_probe_voice_catalog.py
```

(These are dev-only — they're not in the public repo, they're in your local checkout.)

## Code style

See [CONTRIBUTING.md](../CONTRIBUTING.md) — TL;DR is the Karpathy 4-rule style:

1. **Think first** — know the real problem before writing
2. **Simplicity** — minimum code that solves the problem
3. **Surgical** — change as little as possible
4. **Goal-driven** — every commit moves toward a stated goal

### Naming

- `*.py` modules: `snake_case`
- Classes: `PascalCase`
- Functions: `snake_case`
- Test files: `test_*.py`
- Probe/debug scripts (gitignored): `_probe_*.py` or `_*.py`

### Imports

- Standard lib first, then third-party, then local
- Local imports in `core/` use relative imports (`from .llm import …`)
- Lazy import heavy modules inside functions (`from core.prosody import plan_prosody`)

### Error handling

- **Never silent.** If something fails, log loudly and surface to UI.
- Provider adapters return `{ "error": "reason" }` instead of throwing
- API endpoints return `{ "code": "000001", "message": "...", "data": ... }` (Pavo convention)

## Submitting changes

1. Fork → branch (`feat/...` or `fix/...`)
2. Add `tests/test_your_feature.py` (real-data-driven where possible)
3. Update `docs/` if architecture/API/behavior changed
4. Open PR with the template (auto-fills)

## Releasing

```bash
# Bump version in launcher.py:APP_VERSION and main.py
# Build
pyinstaller --clean --noconfirm VideoForge.spec
# Test the built exe (not just the dev script)
./dist/VideoForge.exe --port 8896
# Smoke-test endpoints
curl http://127.0.0.1:8896/api/health
curl http://127.0.0.1:8896/api/providers
# Tag + push
git tag v1.3.0
git push origin v1.3.0
```