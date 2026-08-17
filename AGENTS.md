# AGENTS.md

## Cursor Cloud specific instructions

This repo contains two independent services:

| Service | Location | Stack | Purpose |
| --- | --- | --- | --- |
| Backend (WebDAV server) | repo root (`main.py`, `tgfs/`, `asgidav/`) | Python 3.13 + Poetry, FastAPI/uvicorn | Exposes a private Telegram channel as a WebDAV server. This is the core product. |
| Frontend (docs / mini-app) | `tgfs-gh-pages/` | Next.js 15 (static export, `basePath: /tgfs`) | GitHub Pages site: config generator + Telegram mini app. Served under `/tgfs/`. |

### Environment already provisioned (do not reinstall)
- The base is Ubuntu 24.04 whose system Python is 3.12, but this project requires Python **>=3.13**. Python 3.13 (deadsnakes) and Poetry are already installed in the snapshot; Poetry lives in `~/.local/bin` and is on `PATH` via `~/.bashrc`.
- Poetry uses an **in-project venv** (`.venv/`, `virtualenvs.in-project true`). The startup update script runs `poetry install` and `npm ci`, so dependencies are refreshed automatically.

### Backend: standard commands (see `README.md` / `makefile`)
- Lint/typecheck: `make ruff` and `make mypy` (or `poetry run python -m ruff check . --exclude tests` / `poetry run python -m mypy . --follow-untyped-imports --check-untyped-defs`).
- Tests: `make test` (all 527 pass). Tests require env vars `TGFS_DATA_DIR=.` and `TGFS_CONFIG_FILE=config-test.yaml`; `make test`/`make cov` set these for you. Telegram is fully mocked in tests, so no credentials are needed.
- Run the full app: `poetry run python main.py`. **Gotcha:** this reads a real `config.yaml` (path `$TGFS_DATA_DIR/config.yaml`, default `~/.tgfs/config.yaml`) and immediately logs in to Telegram, so it needs valid `api_id`/`api_hash`, a bot token, and a private file channel. Without real Telegram credentials it cannot fully start. To exercise WebDAV logic without Telegram, use the test suite or drive the `asgidav` ASGI app directly with an in-memory backend.

### Frontend: standard commands (see `tgfs-gh-pages/package.json`)
- Dev server: `cd tgfs-gh-pages && npm run dev` (port 3000). Because of `basePath`, open `http://localhost:3000/tgfs/` — the bare `/` returns 404. The config-generator page renders client-side.
- Lint: `npm run lint`. Build static export: `npm run build` (output in `tgfs-gh-pages/out`).

### Pre-commit hooks (optional, not run automatically here)
`.pre-commit-config.yaml` runs `black` on commit and `ruff`+`mypy` on push. Install with `pre-commit install` if you want them locally.
