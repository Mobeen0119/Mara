# Eloise

Eloise is a no-nonsense execution coach. She turns goals into realistic schedules, calls you out when you drift, and stays honest about whether the work was actually done. FastAPI backend with a single-page web UI.

## Features

- **Goal-driven planning**: file a goal with a deadline and constraints. Clarifying questions are written by the model.
- **Realistic schedules**: plans actions by day with times, respects blocked windows, and surfaces overdue work.
- **Genuinely honest**: 24-hour check-ins and overdue/missed tracking.
- **Uncanny, dry tone**: no motivational speaker fluff, no emojis.
- **Live chat**: context-aware replies from the real model (never canned). Location-aware suggestions.
- **Provider-agnostic**: OpenRouter, Gemini, OpenAI, Groq, Cerebras, DeepSeek, Together, Mistral, or any OpenAI-compatible endpoint. Optional local Ollama.
- **Prompt-leak resistant**: refuses to quote its own system instructions.
- **Email digests**: optional daily/weekly email via SMTP.

## Quick start

Install requirements:

```bash
pip install -r requirements.txt
```

Start the server:

```bash
./launch.sh
```

or directly:

```bash
ELOISE_STORAGE_DIR=./storage uvicorn main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000.

## Model configuration

In the app:

1. Settings → LLM Provider
2. Pick a provider preset, or leave Custom for OpenAI-compatible.
3. Paste your API key (common formats auto-detected).
4. Save & Test, or Test connection to probe without saving.

Keys never echo. Failures show the provider's actual error.

### .env fallback

Settings override .env. When unsaved:

```bash
OPENAI_API_KEY=
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=meta-llama/llama-3.1-8b-instruct:free
```

Legacy aliases: `OPENROUTER_API_KEY/URL/MODEL`, `DEEPSEEK_*`, `GROQ_*`, `CEREBRAS_*`.

Local (optional):
```bash
ELOISE_ENABLE_LOCAL=1
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama-3.1:8b
```

Restart after editing .env.

## Email (optional)

To get emailed digests/check-in emails, set SMTP in `.env` (both naming styles are accepted):

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your_16_char_app_password  # or SMTP_PASSWORD
FROM_EMAIL=you@gmail.com             # or MAIL_FROM
```

Note: Gmail requires a 16-char App Password. The Settings → Email screen shows precise diagnostics (host, port, from, and any configuration issues).

## Storage

`ELOISE_STORAGE_DIR` (default `./storage`) holds `eloise.db` and related files. Backup that directory to preserve data.

## Tests

```bash
python3 -m pip install pytest  # if not present
ELOISE_STORAGE_DIR=/tmp/pytest_storage python3 -m pytest tests/test_logic.py -q
```

## Architecture

- `main.py` — FastAPI app, static files, scheduler
- `core/routes/` — HTTP endpoints
- `core/generation.py` — planning, chat context, guardrails
- `core/llm/` — provider abstraction, presets, fallbacks
- `core/persona.py` — tone and policy
- `core/scheduler.py` — background jobs
- `core/database.py` — schema/migrations
- `static/` — SPA
- `tests/` — unit tests

## Deployment notes

- One worker process if you rely on the lock + scheduler.
- Put behind TLS and restrict `ELOISE_ALLOWED_ORIGINS` if internet-facing.
- Restart to re-read `.env`; Settings take effect immediately.

## Troubleshooting

- **Empty chat reply / "returned nothing usable"** — rephrase or Test connection in Settings; if down, it shows the real provider error.
- **401 bad key** — endpoint/model/key mismatch; pick the correct preset.
- **404 model not found** — model name invalid for that provider.
- **Email "not live"** — Settings → Email lists specific problems (e.g. 13-char password → need 16-char App Password).
- **Overdue not toggling** — actions marked `missed` still have the DONE button in Schedule and Goal detail.

The drill is simple: file it, do it, close it. No pep talks.

