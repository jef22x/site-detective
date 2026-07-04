# SiteDetective

A local, self-healing E2E test suite for any website. Human-authored test steps
run deterministically via Playwright; a local VLM (Ollama) is used **only** to
re-locate elements when a stored selector breaks.

## Features

- **Deterministic test execution** — human-written steps with Playwright for reliable, reproducible runs
- **Self-healing selectors** — when a selector breaks, Ollama (VLM) proposes fixes from the step intent + DOM + screenshot
- **Web UI** — dashboard, live run monitoring, test editor with validation, config management, HTML reports
- **Run history** — SQLite-backed run tracking with audit trail and healing events
- **Multi-context flows** — customer and admin contexts in a single test
- **Docker staging** — docker-compose stack for system testing
- **No external dependencies** — Ollama runs locally; runs work offline if Ollama is unavailable

## Requirements

- Python 3.11 or higher
- Chromium (installed via Playwright)
- [Ollama](https://ollama.ai) with `qwen2.5-vl:7b` (optional, for selector healing)

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

## Web UI

```powershell
.venv\Scripts\python -m uvicorn app.main:app --port 8321
```

Open http://127.0.0.1:8321 — run tests, watch live progress, browse run
history, open HTML reports, edit tests (validated on save), edit config.

## CLI

```powershell
.venv\Scripts\python -m app.cli tests\mock-shop-purchase.yaml --config config\settings.yaml
```

Add `--headed` to watch the browser. Artifacts: SQLite in `data/autoqa.db`,
screenshots + `report.html` in `reports/<run-id>/`.

## Tests

```powershell
.venv\Scripts\python -m pytest unit_tests -q
```

14 tests: runner happy/failure paths, two-context lifecycle, healing matrix
(healed / bounded failure / Ollama offline), web routes, secret masking.

## Staging stack (system tests)

```powershell
cd docker
docker compose up -d
sh provision.sh   # installs WooCommerce, sample product, COD, Mailpit SMTP
```

Store: http://localhost:8080 · Mailpit: http://localhost:8025

## Healing

Set `ollama.enabled: true` in `config/settings.yaml` with Ollama running
`qwen2.5-vl:7b`. When a selector fails, the model proposes a replacement from
the step's `intent` + DOM + screenshot; accepted fixes are written back to the
test file and logged in the healing audit trail. Ollama being offline never
blocks runs.

## Contributing

Issues and pull requests are welcome. For major changes, please open an issue first to discuss.

## License

MIT
