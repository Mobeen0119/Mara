import os

from dotenv import load_dotenv
load_dotenv()

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.database import get_connection, set_storage_dir
from core.routes import auth_routes, checkin_routes, chat_routes, goal_routes, settings_routes
from core.scheduler import eloise_scheduler

STORAGE_DIR = os.environ.get("ELOISE_STORAGE_DIR") or os.path.join(os.getcwd(), "storage")
set_storage_dir(STORAGE_DIR)
get_connection()  # init schema

app = FastAPI(title="Eloise", docs_url="/docs", openapi_url="/openapi.json")

_ALLOWED = [o.strip() for o in (os.environ.get("ELOISE_ALLOWED_ORIGINS", "").split(",") if os.environ.get("ELOISE_ALLOWED_ORIGINS") else ["http://localhost:8000", "http://127.0.0.1:8000"])]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

for r in (auth_routes.router, goal_routes.router, chat_routes.router,
          settings_routes.router, checkin_routes.router):
    app.include_router(r)


@app.get("/{full_path:path}")
def spa(full_path: str):
    base = os.path.join(os.path.dirname(__file__), "static")
    file_path = os.path.join(base, full_path)
    if full_path and os.path.isfile(file_path) and not full_path.startswith(".."):
        return FileResponse(file_path)
    return FileResponse(os.path.join(base, "index.html"))


@app.on_event("startup")
def _start():
    eloise_scheduler.start()


@app.on_event("shutdown")
def _stop():
    eloise_scheduler.shutdown()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))