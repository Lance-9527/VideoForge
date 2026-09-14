# Contributing to VideoForge

First off, thank you for considering contributing to VideoForge! 🎬

This project is a Windows desktop AI short-video agent built on Python + FastAPI + pywebview. It started as a personal tool and is now being open-sourced so others can use, learn from, and extend it.

## Code of Conduct

Be respectful. We are all here to build useful software. Disagreements are fine; rudeness is not.

## How Can I Contribute?

### 🐛 Reporting Bugs
- Use [GitHub Issues](../../issues) with the **Bug** template
- Include: Windows version, Python version, `~/.videoforge/videoforge.log` last 100 lines, steps to reproduce

### 💡 Suggesting Features
- Use GitHub Issues with the **Feature** template
- Be specific about the user value — "this would be nice" is not enough; describe who needs it and why

### 🔧 Submitting Code
1. Fork the repo
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Write code + tests in `tests/test_*.py`
4. Make sure tests pass:
   ```bash
   python tests/test_your_feature.py
   ```
5. Update `docs/` if your change affects architecture / API / behavior
6. Open a Pull Request with the **PR** template

### 📝 Improving Docs
Docs are as important as code. Typos, clarity, examples — all welcome.

## Project Structure (where to put what)

```
backend/
├── main.py              # FastAPI entry — add endpoints here
├── models.py            # Pydantic schemas
├── api/                 # HTTP routers (chat, media, ...)
├── core/                # Business logic, NO I/O at import time
│   ├── adapters/        # Video model adapters (one file per provider)
│   ├── voice/           # TTS providers (dispatcher + per-engine)
│   └── *.py             # Pipeline, postprocess, ...
├── frontend/
│   ├── index.html
│   ├── js/
│   └── css/
└── tests/
    ├── test_*.py        # Real tests (these go into git)
    └── _*.py            # Debug probes (gitignored)
```

## Style Guide (Karpathy-inspired, 4 rules)

1. **Think first** — don't write code before understanding the real problem
2. **Simplicity** — minimum code that solves the problem, no "framework for frameworks' sake"
3. **Surgical** — change as little as possible; if a fix is local, keep it local
4. **Goal-driven** — every commit should move toward a stated goal

## Coding Conventions

- Python 3.11+ syntax (the project runs on 3.14.5)
- Type hints everywhere
- No silent failures — log loudly when something goes wrong
- No hardcoded API keys — always read from environment or settings
- Tests must be **real-data-driven** when possible (the project has hundreds of real test cases from its evolution)

## Commit Messages

- Imperative mood: "Add X" not "Added X"
- First line ≤ 72 chars
- Body explains **why**, not **what** (the diff shows what)
- One logical change per commit

## Security Issues

**Do not** open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).