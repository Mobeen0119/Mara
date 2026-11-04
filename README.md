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

Paste **any** API key in Settings → LLM Provider (admin only — the first account created is the admin, or set `ELOISE_ADMIN_EMAIL`). The provider is recognised from the key prefix, otherwise probed in parallel; the endpoint and a sensible model are chosen and verified with a real request. Anthropic keys use the native Messages API; everything else uses OpenAI-compatible chat. Eloise's persona prompt is the same for every provider; only the transport adapts (token-limit parameter, system-role handling).

The saved key is only ever sent to the host it was saved for, and private/metadata addresses are refused unless `ELOISE_ALLOW_PRIVATE_LLM=1` (for local servers).

`.env` fallback: `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`. Optional local Ollama via `ELOISE_ENABLE_LOCAL=1`. See `.env.example`.

## How scheduling works

The engine decides **when**, the model decides **what** goes in each slot. Days are built around a human routine: wake/sleep, meals, your always-busy windows and each goal's own constraints are carved out first; work goes into your energy windows (morning/evening person is inferred), in 40–120 minute focus blocks with breaks, never more than 6 focused hours a day across all goals, heavier as a deadline closes in, and today only gets hours still ahead of you. Everything uses your own timezone (synced from the browser).

### Long horizons

How the plan is drawn depends on how far away the deadline is: up to 8 weeks it is day-by-day; over 8 weeks it is a **weekly** roadmap; over about 2.5 months it is a **monthly** roadmap. Each roadmap period gets a phase (Foundations, Build, Pressure test, Final push), a real hours budget (the free time that period actually has) and a measurable objective. Hour-by-hour tasks are only drawn for the next 14 days and roll forward automatically (the scheduler tops the window up when fewer than 7 days remain). A redraw never touches done or missed history.

### Priorities (triage)

`GET /api/triage` ranks every goal by pressure, days left, remaining hours, slippage and stakes (exam/interview/visa > proposal/startup/launch > the rest). When one goal is on fire, it names the soft commitments that should give way (gym, hobbies, social) and never the hard ones (class, work, sleep, meals, family). The same call is placed in Eloise's chat context, so she can say it plainly: _"Fuck the gym this week - the exam is 5 days out and 11 hours are still open."_

## Docker

```bash
docker build -t eloise .
docker run -p 8000:8000 -v eloise-data:/data --env-file .env eloise
```

Keep it to one worker (the scheduler is in-process).

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
ELOISE_STORAGE_DIR=/tmp/pytest_storage python3 -m pytest -q
```

## Architecture

- `main.py` — FastAPI app, static files, scheduler
- `core/routes/` — HTTP endpoints
- `core/generation.py` — planning, chat context, guardrails
- `core/llm/` — provider abstraction, presets, fallbacks
- `core/persona.py` — tone and policy
- `core/scheduler.py` — background jobs
- `core/database.py` — schema/migrations
- `core/planner.py` — human-rhythm day planner
- `core/security.py`, `core/ratelimit.py`, `core/clock.py` — sessions/hashing/SSRF guard, rate limits, per-user time
- `static/` — SPA (Dusk Dial UI)
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
