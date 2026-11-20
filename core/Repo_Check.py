import json
import re
import urllib.parse
import urllib.request

API = "https://api.github.com"
_ASK = re.compile(r"\b(search|check|look\s*up|look\s*at|find|status|progress|review|see)\b", re.I)
_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)", re.I)
_SKIP = {
    "it",
    "my",
    "the",
    "a",
    "an",
    "repo",
    "repository",
    "github",
    "progress",
    "status",
    "project",
    "code",
    "current",
    "stats",
    "and",
    "then",
    "tell",
    "me",
    "what",
    "to",
    "do",
    "of",
    "on",
    "for",
    "again",
    "now",
}
_FILE = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,5}\b|\b(?:Makefile|Dockerfile|README)\b")


def _get(path):
    req = urllib.request.Request(
        API + path, headers={"Accept": "application/vnd.github+json", "User-Agent": "eloise-planner"}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def wanted_repo(message):
    """'search Aevros', 'check Aevros progress', or a github.com link -> 'Aevros' / 'owner/name'. None if no name."""
    m = _URL.search(message or "")
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if not _ASK.search(message or ""):
        return None
    words = re.findall(r"[A-Za-z0-9_./-]+", message)
    after = False
    for w in words:
        if _ASK.fullmatch(w):
            after = True
            continue
        if after and w.lower() not in _SKIP:
            return w
    return None


def lookup(name):
    """Find the repo and read what is in it. Returns a dict, or {'error': ...}."""
    try:
        full = name
        if "/" not in name:
            q = urllib.parse.quote(f"{name} in:name")
            items = _get(f"/search/repositories?q={q}&per_page=5&sort=updated").get("items", [])
            exact = [i for i in items if i["name"].lower() == name.lower()] or items
            if not exact:
                return {
                    "error": f"I found no public GitHub repo called {name}. Is it private, or spelled differently?"
                }
            full = exact[0]["full_name"]
        repo = _get(f"/repos/{full}")
        commits = _get(f"/repos/{full}/commits?per_page=8")
        tree = _get(f"/repos/{full}/git/trees/{repo['default_branch']}?recursive=1").get("tree", [])
    except Exception as exc:
        return {
            "error": f"I could not reach GitHub from the server ({type(exc).__name__}). Check its internet access."
        }
    return {
        "full_name": full,
        "description": repo.get("description") or "",
        "pushed_at": repo.get("pushed_at", ""),
        "files": [t["path"] for t in tree if t.get("type") == "blob"][:400],
        "commits": [
            {"date": c["commit"]["author"]["date"][:10], "msg": c["commit"]["message"].splitlines()[0][:90]}
            for c in commits
        ],
    }


def looks_done(task_title, files):
    """A task names a file (boot.asm, Makefile, linker.ld) that now exists in the repo."""
    named = {f.lower() for f in _FILE.findall(task_title or "")}
    have = {p.lower() for p in files} | {p.lower().rsplit("/", 1)[-1] for p in files}
    return sorted(f for f in named if f in have)