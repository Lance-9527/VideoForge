# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.2.x   | ✅ Active          |
| < 1.2   | ❌ End-of-life     |

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.**

Email the maintainer directly at the address listed on the GitHub repository page (under "Insights → Contributors" or "Settings → Collaborators"). Include:

1. Description of the vulnerability
2. Steps to reproduce
3. Potential impact (data leak, RCE, etc.)
5. Any known mitigations

You should receive a response within 72 hours. We will:

1. Confirm receipt within 72 hours
2. Investigate and develop a patch
3. Coordinate disclosure timeline with you
4. Credit you in the fix release notes (unless you prefer to remain anonymous)

## Security Design Notes (for context)

VideoForge takes the following security stance by default:

- **No hardcoded API keys in source code.** All keys are read at runtime from `~/.videoforge/videoforge.log`-adjacent settings or environment variables.
- **No telemetry.** The app makes outbound HTTP only to the video/LLM/TTS providers you configure.
- **Local-first storage.** Project data lives in `%LOCALAPPDATA%\VideoForge\data` (or `VIDEOFORGE_DATA_DIR` if set). Nothing is sent to a third-party server except provider API calls you initiate.
- **FFmpeg sandboxing.** The bundled FFmpeg runs with `CREATE_NO_WINDOW + clean env` (see `launcher.py`) to avoid DLL hijacking on Windows.

## What counts as a vulnerability

- Anything that leaks API keys (yours or other users')
- Anything that lets an attacker execute code on a user's machine
- Anything that lets an attacker read project data (videos, scripts) without permission
- Provider adapter bugs that lead to accidental data leaks to the wrong provider

## What does NOT count

- "I think this feature should work differently" → Feature request, not a vulnerability
- Performance issues → Bug report, not a vulnerability
- Provider-side data handling (we can document it, but it's not our code)