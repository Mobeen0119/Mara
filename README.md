# Eloise

Eloise is a sharp-tongued execution coach that turns big goals into concrete daily plans and keeps you honest about following through. It runs as a FastAPI app with a web UI.

Features

- Goal creation with deadlines and blocked windows
- Auto-generated daily schedules with check-ins and nudges
- Sarcastic, loving drill-mother persona
- LLM-powered replies with optional local and cloud models

## Quick Start

1. Install dependencies
   pip install -r requirements.txt

2. Configure environment (optional)
   Copy .env.example to .env and set values as needed.

3. Launch the app
   ./launch.sh [PORT] (default 8000)
   or
   ELOISE_STORAGE_DIR=/path/to/storage uvicorn main:app --host 127.0.0.1 --port 8000

## Model Configuration

Eloise is designed to run with minimal setup. You can use either local (Ollama) or any OpenAI-compatible cloud provider - just paste an API key and it works.

Cloud (OpenAI-compatible)

- Works with OpenRouter, Groq, OpenAI, DeepSeek, Together, Mistral, xAI, vLLM, LMStudio, etc.
- In Settings (or via API), set:
  - API Key: your provider key
  - Base URL: e.g. https://openrouter.ai/api/v1 (auto-detected from provider)
  - Model: the model name you want to use
- No other configuration needed. The app accepts API key, base URL, and model name directly.

Local (Ollama)

- Enable local inference via Settings or set ELOISE_ENABLE_LOCAL=1 in .env
- Set OLLAMA_URL (default http://localhost:11434) and OLLAMA_MODEL
- Chat can use a faster model via OLLAMA_CHAT_MODEL if desired

## Storage

- Data is stored under ELOISE_STORAGE_DIR. By default, launch.sh sets it to ./storage
- The app uses SQLite (see core/database.py)

## Notes

- The app title is "Eloise". The persona is intentionally sarcastic - a loving drill-mother.
- CORS is configured in main.py; adjust ELOISE_ALLOWED_ORIGINS as needed.
- Email is optional (SMTP settings in .env if using digest emails)
