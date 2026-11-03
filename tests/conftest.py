import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("ELOISE_TEST_TODAY", None)
for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
    os.environ.pop(k, None)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """A fresh app + database per test, plus a private static dir so file-serving
    tests can plant a 'secret' next to it."""
    monkeypatch.setenv("ELOISE_STORAGE_DIR", str(tmp_path / "storage"))
    import core.database as dbm
    from core import ratelimit
    dbm.set_storage_dir(str(tmp_path / "storage"))
    ratelimit.clear_all()
    dbm.get_connection()
    import main
    yield main
    ratelimit.clear_all()


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient
    with TestClient(app_env.app) as c:
        yield c


def signup(client, email="owner@example.com", name="Owner", password="correct-horse-battery"):
    r = client.post("/api/signup", json={"name": name, "email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}
