"""Regression tests for every vulnerability found in the production-readiness audit.
Each one FAILED against the original code."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.conftest import auth, signup


def test_guest_with_registered_email_cannot_take_over_account(client):
    victim = signup(client, "victim@example.com", "Victim")
    r = client.post("/api/guest", json={"name": "Attacker", "email": "victim@example.com"})
    assert r.status_code == 200
    assert r.json()["token"] != victim["token"]
    me = client.get("/api/me", headers=auth(r.json()["token"])).json()
    assert me["email"] is None and me["name"] == "Attacker" and not me["is_admin"]


def test_static_catch_all_cannot_escape_static_dir(client, app_env):
    secret = app_env.BASE_DIR / ".env"
    existed = secret.exists()
    secret.write_text("SMTP_PASS=TOP_SECRET_VALUE\n")
    try:
        for path in ("/css/../../.env", "/..%2f.env", "/%2e%2e/.env", "/static/../.env", "/../.env"):
            r = client.get(path)
            assert "TOP_SECRET_VALUE" not in r.text, path
        assert "TOP_SECRET_VALUE" not in client.get("/.env").text
    finally:
        if not existed:
            secret.unlink()


def test_unknown_api_route_is_404_not_the_spa(client):
    assert client.get("/api/nope").status_code == 404


def test_llm_settings_admin_only(client):
    admin = signup(client, "admin@example.com", "Admin")
    assert admin["is_admin"] is True
    guest = client.post("/api/guest", json={"name": "rando"}).json()
    member = signup(client, "member@example.com", "Member")
    assert member["is_admin"] is False
    for tok in (guest["token"], member["token"]):
        h = auth(tok)
        assert client.put("/api/settings/llm", headers=h, json={"base_url": "https://evil.example/v1",
                                                                  "api_key": "x", "model": "m"}).status_code == 403
        assert client.post("/api/settings/llm/test", headers=h, json={}).status_code == 403
        view = client.get("/api/settings/llm", headers=h).json()
        assert "base_url" not in view and "presets" not in view and "key_source" not in view


def test_test_endpoint_never_sends_saved_key_to_another_url(client, monkeypatch):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"pwned"}}]}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ELOISE_ALLOW_PRIVATE_LLM", "1")
    try:
        admin = signup(client, "admin@example.com", "Admin")
        import json
        from core.database import get_connection
        conn = get_connection()
        conn.execute("INSERT INTO app_settings (key,value) VALUES ('llm', ?)",
                     (json.dumps({"api_key": "sk-or-REAL-PRODUCTION-KEY", "base_url": "https://openrouter.ai/api/v1",
                                  "model": "m", "provider": "openrouter"}),))
        conn.commit()
        r = client.post("/api/settings/llm/test", headers=auth(admin["token"]),
                        json={"base_url": f"http://127.0.0.1:{srv.server_port}/v1"})
        assert r.json()["ok"] is False
        assert seen == [], "saved key was sent to the caller's server"
        # and changing the endpoint through PUT without pasting that provider's key is refused
        r = client.put("/api/settings/llm", headers=auth(admin["token"]),
                       json={"base_url": f"http://127.0.0.1:{srv.server_port}/v1"})
        assert r.status_code == 400
    finally:
        srv.shutdown()


def test_llm_endpoint_must_not_be_internal_or_metadata(client):
    admin = signup(client, "admin@example.com", "Admin")
    for url in ("http://169.254.169.254/latest", "http://127.0.0.1:9/v1", "http://10.0.0.5/v1", "ftp://x/v1"):
        r = client.put("/api/settings/llm", headers=auth(admin["token"]),
                       json={"base_url": url, "api_key": "sk-test-1234567890"})
        assert r.status_code == 400, url


def test_frontend_linkify_escapes_html():
    import re
    import shutil
    import subprocess
    import pathlib
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    js = (pathlib.Path(__file__).resolve().parent.parent / "static/js/app.js").read_text()
    a, b = js.index("function linkify"), js.index("function fmtTime")
    e, e2 = js.index("function escHtml"), js.index("function confirmModal")
    harness = (
        "global.document={createElement:()=>({set textContent(v){this._t=String(v)},"
        "get innerHTML(){return this._t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;')}})};"
        + js[e:e2] + js[a:b] +
        "console.log(linkify(process.argv[1]));"
    )
    payload = '<img src=x onerror=alert(1)> <script>x</script> https://a.example/"onmouseover="alert(1)'
    out = subprocess.run([node, "-e", harness, payload], capture_output=True, text=True).stdout
    assert "<img" not in out and "<script" not in out and '"onmouseover' not in out.replace("%22onmouseover", "")


def test_gitignore_is_utf8_and_covers_secrets_and_data():
    import pathlib
    raw = (pathlib.Path(__file__).resolve().parent.parent / ".gitignore").read_bytes()
    assert b"\x00" not in raw, ".gitignore must not be UTF-16"
    text = raw.decode()
    for needle in (".env", "*.db", "venv/", "__pycache__/"):
        assert needle in text


def test_passwords_are_salted_scrypt_and_legacy_hashes_upgrade(client):
    import hashlib
    from core.database import get_connection
    signup(client, "a@example.com", "A", "samepassword1")
    signup(client, "b@example.com", "B", "samepassword1")
    rows = get_connection().execute("SELECT password_hash FROM users ORDER BY id").fetchall()
    assert rows[0][0] != rows[1][0] and rows[0][0].startswith("scrypt$")
    # a user stored by the OLD code (unsalted sha256) can still log in once, and is upgraded
    legacy = hashlib.sha256(b"oldpassword1").hexdigest()
    conn = get_connection()
    conn.execute("INSERT INTO users (name,email,password_hash,verified) VALUES ('Old','old@example.com',?,1)", (legacy,))
    conn.commit()
    assert client.post("/api/login", json={"email": "old@example.com", "password": "oldpassword1"}).status_code == 200
    assert conn.execute("SELECT password_hash FROM users WHERE email='old@example.com'").fetchone()[0].startswith("scrypt$")


def test_logout_revokes_the_session(client):
    tok = signup(client)["token"]
    assert client.get("/api/me", headers=auth(tok)).status_code == 200
    assert client.post("/api/logout", headers=auth(tok)).status_code == 200
    assert client.get("/api/me", headers=auth(tok)).status_code == 401


def test_login_is_rate_limited_and_does_not_reveal_which_part_was_wrong(client):
    signup(client, "victim@example.com", "V")
    codes = [client.post("/api/login", json={"email": "victim@example.com", "password": f"wrong-pass-{i}"}).status_code
             for i in range(8)]
    assert codes[:5] == [401] * 5 and 429 in codes[5:]
    # even the right password is refused during lockout
    assert client.post("/api/login", json={"email": "victim@example.com",
                                           "password": "correct-horse-battery"}).status_code == 429
    unknown = client.post("/api/login", json={"email": "nobody@example.com", "password": "x"}).json()
    assert unknown["detail"] == "bad credentials"


def test_old_session_tokens_from_the_leaked_database_are_dead(tmp_path, monkeypatch):
    import sqlite3
    import core.database as dbm
    d = tmp_path / "legacy"
    d.mkdir()
    c = sqlite3.connect(d / "eloise.db")
    c.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, password_hash TEXT NOT NULL,"
                    " is_guest INTEGER DEFAULT 0, verified INTEGER DEFAULT 0, token TEXT);"
                    "INSERT INTO users (id,name,email,password_hash,token) VALUES (1,'Mobeen','m@x.com','h','LEAKED-TOKEN');")
    c.commit(); c.close()
    dbm.set_storage_dir(str(d))
    conn = dbm.get_connection()
    assert conn.execute("SELECT token FROM users").fetchone()[0] is None
    assert conn.execute("SELECT is_admin FROM users").fetchone()[0] == 1   # oldest real account administers


def test_email_verification_needs_the_emailed_single_use_token(client, monkeypatch):
    sent = {}
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com"); monkeypatch.setenv("SMTP_USER", "u"); monkeypatch.setenv("SMTP_PASS", "p")
    monkeypatch.setattr("core.mailer.send_email", lambda to, subj, text="", html=None: sent.update(to=to, text=text) or True)
    r = signup(client, "new@example.com", "New")
    assert r["verified"] is False
    # the session token must NOT verify the account (old bug)
    assert client.get("/api/verify-email", params={"token": r["token"]}).status_code == 400
    import re
    link_token = re.search(r"token=([\w-]+)", sent["text"]).group(1)
    assert client.get("/api/verify-email", params={"token": link_token}).status_code == 200
    assert client.get("/api/me", headers=auth(r["token"])).json()["verified"] is True
    assert client.get("/api/verify-email", params={"token": link_token}).status_code == 400   # single use
