let token = localStorage.getItem("eloise_token") || "";
let userName = localStorage.getItem("eloise_name") || "";
let currentScreen = "today";

const api = async (path, opts = {}) => {
  const headers = {
    "Content-Type": "application/json",
    ...(opts.headers || {}),
  };
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch("/api" + path, { ...opts, headers });
  if (res.status === 401 && token) {
    logout();
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const d = (await res.json()).detail;
      if (d)
        detail =
          typeof d === "string"
            ? d
            : d.error ||
              (Array.isArray(d)
                ? d.map((x) => x.msg || JSON.stringify(x)).join("; ")
                : JSON.stringify(d));
    } catch (e) {}
    throw new Error(detail);
  }
  if (res.status === 204) return {};
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : {};
};

const _EXT_ICON =
  '<svg class="lk-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17L17 7M9 7h8v8"/></svg>';
const _TOKEN_RE =
  /(\[[^\]\n]{1,120}\]\(https?:\/\/[^\s)]+\))|(https?:\/\/[^\s<>()]*[^\s<>().,!?;:'"\]])|(\*\*[^*\n]{1,200}\*\*)|(\n+)|([ \t]+)|([^\s]+)/g;

function _linkChip(href, label) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  const host = u.hostname.replace(/^www\./, "");
  let path = (u.pathname === "/" ? "" : u.pathname) + (u.search ? "?…" : "");
  if (path.length > 22) path = path.slice(0, 21) + "…";
  const inner = label
    ? `<span class="lk-label">${escHtml(label)}</span>`
    : `<span class="lk-host">${escHtml(host)}</span><span class="lk-path">${escHtml(path)}</span>`;
  return `<a class="chat-link" href="${escHtml(u.href)}" target="_blank" rel="noopener noreferrer" title="${escHtml(u.href)}">${inner}${_EXT_ICON}</a>`;
}

function chatTokens(text) {
  const out = [];
  if (typeof text !== "string") return out;
  let m;
  _TOKEN_RE.lastIndex = 0;
  while ((m = _TOKEN_RE.exec(text)) !== null) {
    if (m[1]) {
      const mm = /^\[([^\]]+)\]\((.+)\)$/.exec(m[1]);
      const chip = mm && _linkChip(mm[2], mm[1]);
      out.push(
        chip
          ? { kind: "link", html: chip }
          : { kind: "word", html: escHtml(m[1]) },
      );
    } else if (m[2]) {
      const chip = _linkChip(m[2], "");
      out.push(
        chip
          ? { kind: "link", html: chip }
          : { kind: "word", html: escHtml(m[2]) },
      );
    } else if (m[3])
      out.push({
        kind: "word",
        html: `<strong>${escHtml(m[3].slice(2, -2))}</strong>`,
      });
    else if (m[4])
      out.push({ kind: "br", html: m[4].length > 1 ? "<br><br>" : "<br>" });
    else if (m[5]) out.push({ kind: "space", html: " " });
    else out.push({ kind: "word", html: escHtml(m[6]), raw: m[6] });
  }
  return out;
}

function linkify(text) {
  return chatTokens(text)
    .map((t) => t.html)
    .join("");
}
function fmtTime(t) {
  if (!t) return "";
  const [h, m] = t.split(":");
  let hh = parseInt(h, 10) % 24;
  const am = hh < 12;
  hh = hh % 12;
  if (hh === 0) hh = 12;
  return `${hh}:${m} ${am ? "AM" : "PM"}`;
}
function fmtHours(h) {
  return `${h}h`;
}
function showToast(msg, type) {
  const wrap = document.getElementById("toasts") || document.body;
  const t = document.createElement("div");
  t.className =
    "toast" +
    (type === "error" ? " error" : type === "success" ? " success" : "");
  t.textContent = msg;
  wrap.appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

function safeLink(url) {
  const s = String(url || "")
    .trim()
    .toLowerCase();
  if (/^(javascript:|data:)/i.test(url || "")) return "#";
  return s;
}

function closeModal() {
  const m = document.getElementById("app-modal");
  if (m) m.remove();
}
function openModal(title, bodyHtml, actionsHtml) {
  closeModal();
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.id = "app-modal";
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-eyebrow">ELOISE</div>
      <h3>${escHtml(title)}</h3>
      <p class="body">${bodyHtml || ""}</p>
      <div class="actions">${actionsHtml || ""}</div>
    </div>`;
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });
  document.body.appendChild(overlay);
}
function escHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}
function confirmModal(title, body, confirmLabel, danger) {
  return new Promise((resolve) => {
    window._resolveModal = resolve;
    openModal(
      title,
      escHtml(body),
      `
      <button class="btn btn-ghost" onclick="closeModal();window._resolveModal&&(window._resolveModal(false),window._resolveModal=null)">Cancel</button>
      <button class="btn ${danger ? "btn-danger" : ""}" ${danger ? 'style="color:#fff;"' : ""} onclick="closeModal();window._resolveModal&&(window._resolveModal(true),window._resolveModal=null)">${escHtml(confirmLabel)}</button>
    `,
    );
  });
}
function askModal(title, placeholder) {
  return new Promise((resolve) => {
    window._resolveModal = resolve;
    openModal(
      title,
      `<div class="field"><input type="text" id="ask-modal-input" placeholder="${escHtml(placeholder)}"></div>`,
      `
      <button class="btn btn-ghost" onclick="closeModal();window._resolveModal&&(window._resolveModal(false),window._resolveModal=null)">Cancel</button>
      <button class="btn" onclick="window._resolveModal&&(closeModal(),window._resolveModal(document.getElementById('ask-modal-input').value.trim()),window._resolveModal=null)">Send</button>
    `,
    );
    requestAnimationFrame(() => {
      const i = document.getElementById("ask-modal-input");
      if (i) {
        i.focus();
        i.onkeypress = (e) => {
          if (e.key === "Enter") {
            const v = i.value.trim();
            closeModal();
            window._resolveModal &&
              (window._resolveModal(v), (window._resolveModal = null));
          }
        };
      }
    });
  });
}

let _checkinShown = false;
async function pollCheckIn() {
  if (!token || _checkinShown) return;
  try {
    const chk = await api("/checkin");
    if (chk.due) {
      _checkinShown = true;
      showCheckInDialog();
    }
  } catch (e) {}
}
async function showCheckInDialog() {
  let promptMsg = "It's been a day. Did you actually do what was on the board?";
  try {
    const p = await api("/checkin/prompt", { method: "POST" });
    if (p && p.message) promptMsg = p.message;
  } catch (e) {}
  openModal(
    "Check-in",
    linkify(promptMsg),
    `
    <button class="btn btn-ghost" onclick="respondCheckIn(false)">No — didn't do it</button>
    <button class="btn" onclick="respondCheckIn(true)">Yes — done</button>
  `,
  );
}
async function respondCheckIn(done) {
  closeModal();
  _checkinShown = false;
  try {
    const res = await api("/checkin/respond", {
      method: "POST",
      body: JSON.stringify({ done }),
    });
    if (res && res.message) {
      showToast(res.message, done ? "success" : "error");
      if (!done) setTimeout(() => navigate(currentScreen), 1500);
    }
  } catch (e) {}
}

let _authTab = "signin";
let _codeLogin = false;
async function renderAuth(tab) {
  if (tab) _authTab = tab;
  try {
    _codeLogin = !!(await fetch("/api/auth/config").then((r) => r.json()))
      .code_login;
  } catch (e) {
    _codeLogin = false;
  }
  const tabs = [
    ["signin", "Sign in"],
    ["signup", "New here"],
    ["guest", "Guest"],
  ];
  const head = `<div class="auth-tabs" role="tablist">${tabs
    .map(
      ([k, l]) =>
        `<button role="tab" aria-selected="${k === _authTab}" onclick="renderAuth('${k}')">${l}</button>`,
    )
    .join("")}</div>`;
  const forms = {
    signin: `<h2>Welcome back</h2>
      <div class="field"><label for="auth-email">Email</label><input type="email" id="auth-email" autocomplete="username" placeholder="you@place.com"></div>
      <div id="auth-pass-box"><div class="field"><label for="auth-pass">Password</label><input type="password" id="auth-pass" autocomplete="current-password" placeholder="Your password"></div>
      <button class="btn" onclick="doLogin()">Sign in</button></div>
      <div id="auth-code-box" class="hidden"><div class="field"><label for="auth-code">6-digit code from your email</label><input type="text" id="auth-code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="123456"></div>
      <button class="btn" onclick="verifyCode()">Sign in with code</button></div>
      ${_codeLogin ? '<button class="auth-link" id="auth-code-toggle" onclick="requestCode()">Email me a code instead of a password</button>' : ""}`,
    signup: `<h2>New here</h2>
      <div class="field"><label for="auth-name">Name</label><input type="text" id="auth-name" autocomplete="name" placeholder="What should I call you?"></div>
      <div class="field"><label for="auth-email">Email</label><input type="email" id="auth-email" autocomplete="username" placeholder="you@place.com"></div>
      <div class="field"><label for="auth-pass">Password</label><input type="password" id="auth-pass" autocomplete="new-password" placeholder="At least 8 characters"></div>
      <button class="btn" onclick="doSignup()">Create account</button>
      <p class="auth-note">Your email is only used to sign you in and send you your own plan.</p>`,
    guest: `<h2>Just try it</h2>
      <div class="field"><label for="guest-name">First name</label><input type="text" id="guest-name" autocomplete="given-name" placeholder="What do we call you?"></div>
      <button class="btn" onclick="doGuest()">Start now</button>
      <p class="auth-note">No email, no password. Your guest workspace lives in this browser, so clear it or switch device and it is gone. Create an account whenever you want to keep things.</p>`,
  };
  document.getElementById("auth-form").innerHTML = head + forms[_authTab];
  const first = document.querySelector("#auth-form input");
  if (first) first.focus();
  document.querySelectorAll("#auth-form input").forEach((i) =>
    i.addEventListener("keydown", (e) => {
      if (e.key === "Enter")
        document.querySelector("#auth-form .btn:not(.hidden)")?.click();
    }),
  );
}
function _startSession(r) {
  token = r.token;
  userName = r.name;
  localStorage.setItem("eloise_token", token);
  localStorage.setItem("eloise_name", userName);
  enterApp();
}
async function doLogin() {
  try {
    _startSession(
      await api("/login", {
        method: "POST",
        body: JSON.stringify({
          email: document.getElementById("auth-email").value.trim(),
          password: document.getElementById("auth-pass").value,
        }),
      }),
    );
  } catch (e) {
    showToast(
      e.message === "unauthorized"
        ? "That email and password do not match."
        : e.message,
      "error",
    );
  }
}
async function doSignup() {
  try {
    _startSession(
      await api("/signup", {
        method: "POST",
        body: JSON.stringify({
          name: document.getElementById("auth-name").value.trim(),
          email: document.getElementById("auth-email").value.trim(),
          password: document.getElementById("auth-pass").value,
        }),
      }),
    );
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function requestCode() {
  const email = document.getElementById("auth-email").value.trim();
  if (!email) {
    showToast("Type your email first.", "error");
    return;
  }
  try {
    await api("/auth/code/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    document.getElementById("auth-pass-box").classList.add("hidden");
    document.getElementById("auth-code-box").classList.remove("hidden");
    document.getElementById("auth-code-toggle").classList.add("hidden");
    document.getElementById("auth-code").focus();
    showToast(
      "If that address has an account, a code is on its way. It lasts 10 minutes.",
      "success",
    );
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function verifyCode() {
  try {
    _startSession(
      await api("/auth/code/verify", {
        method: "POST",
        body: JSON.stringify({
          email: document.getElementById("auth-email").value.trim(),
          code: document.getElementById("auth-code").value.trim(),
        }),
      }),
    );
  } catch (e) {
    showToast(
      e.message === "unauthorized" ? "That code did not work." : e.message,
      "error",
    );
  }
}
async function doGuest() {
  const name = document.getElementById("guest-name")?.value?.trim() || "Guest";
  try {
    _startSession(
      await api("/guest", { method: "POST", body: JSON.stringify({ name }) }),
    );
  } catch (e) {
    showToast(e.message, "error");
  }
}
function logout() {
  token = "";
  userName = "";
  localStorage.removeItem("eloise_token");
  localStorage.removeItem("eloise_name");
  document.getElementById("app-screen").classList.add("hidden");
  document.getElementById("auth-screen").classList.remove("hidden");
  closeModal();
  document.querySelectorAll(".onboarding-overlay").forEach((n) => n.remove());
  renderAuth("signin");
}
async function syncTimezone() {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz)
      await api("/settings/timezone", {
        method: "PUT",
        body: JSON.stringify({ timezone: tz }),
      });
  } catch (e) {
    /* the server falls back to UTC — harmless */
  }
}
function enterApp() {
  document.getElementById("auth-screen").classList.add("hidden");
  document.getElementById("app-screen").classList.remove("hidden");
  const ru = document.getElementById("orbit-user");
  if (ru) ru.textContent = (userName || "?").slice(0, 1).toUpperCase();
  const dn = document.getElementById("dock-name");
  if (dn) dn.textContent = userName || "";
  syncTimezone();
  navigate("today");
  mountChatPanel();
  refreshFootMomentum();
  setTimeout(maybeOnboarding, 350);
}

async function refreshFootMomentum() {
  const el = document.getElementById("orbit-user");
  if (!el) return;
  try {
    const m = await api("/momentum");
    el.title = `${userName || "you"} — momentum ${m.momentum}`;
    const dn = document.getElementById("dock-name");
    if (dn)
      dn.innerHTML = `${escHtml(userName || "")}<small>momentum ${escHtml(m.momentum)}</small>`;
    el.dataset.momentum = m.momentum;
  } catch (e) {
    /* silent */
  }
}

async function maybeOnboarding() {
  if (!token) return;
  try {
    const goals = await api("/goals");
    if (goals.length > 0) return; // already has real files
    showOnboarding(onboardSeed);
  } catch (e) {
    /* board may be loading; skip */
  }
}
function showOnboarding(onDone) {
  closeModal();
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay onboarding-overlay";
  overlay.id = "app-modal";
  overlay.innerHTML = `
    <div class="modal onboard-modal">
      <div class="modal-eyebrow">ELOISE // SEAT</div>
      <div id="onb-step" class="onb-step">
        <p class="onb-kicker" id="onb-kicker">Step 1 of 3</p>
        <h3 id="onb-title"></h3>
        <p class="onb-body" id="onb-body"></p>
        <div class="onb-actions" id="onb-actions"></div>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const intro = [
    {
      kicker: "STEP 1 — THE RIG",
      title: "You just walked into the control room.",
      body: "Eloise runs your schedule like a mom who's had too much coffee: the plan is drawn out, the pressure is scored 1-to-10, and if a deadline slips, whatever you staked comes due. No games.",
    },
    {
      kicker: "STEP 2 — REAL FILES",
      title: "I'm seeding two demo files.",
      body: "A launch build and a report you're dead on the line for. Schedules draw themselves around what's actually blocked. Tap around — it all moves.",
      button: "Seed the demos",
      fn: () => {
        if (onDone) onDone();
      },
    },
    {
      kicker: "STEP 3 — GO",
      title: "Seat's yours.",
      body: "File real goals, pin stakes, and let the momentum meter decide if you're on fire or stalling. Don't embarrass the ledger.",
      button: "Open the board",
      fn: () => {
        closeModal();
        navigate("board");
      },
    },
  ];
  let i = 0;
  function render() {
    const s = intro[i];
    document.getElementById("onb-kicker").textContent = s.kicker;
    document.getElementById("onb-title").textContent = s.title;
    document.getElementById("onb-body").textContent = s.body;
    const acts = document.getElementById("onb-actions");
    window.onbGo = s.fn || window.nextOnb;
    acts.innerHTML = `<button class="btn" onclick="window.onbGo()">${s.button || "Continue"}</button>
      <button class="btn btn-ghost" onclick="closeModal()">Skip</button>`;
  }
  window.nextOnb = () => {
    i = Math.min(i + 1, intro.length - 1);
    render();
  };
  render();
}
async function onboardSeedGo() {
  try {
    await api("/onboarding/seed", { method: "POST" });
  } catch (e) {
    showToast(e.message, "error");
  }
  if (window.nextOnb) window.onbGo = window.nextOnb;
  window.nextOnb();
}
const onboardSeed = onboardSeedGo;

const KNOWN_SCREENS = [
  "today",
  "board",
  "goals",
  "new-goal",
  "goal",
  "schedule",
  "global-chat",
  "settings",
];
let _hashLock = false;

function navigate(screen, param, fromHash) {
  // The chat is a persistent side panel now, not a screen. Legacy #global-chat
  // links and the rail button open it over whatever page you're on.
  if (screen === "global-chat") {
    openChatPanel();
    screen = "today";
  }
  if (!fromHash && KNOWN_SCREENS.includes(screen)) {
    const h =
      "#" +
      screen +
      (param !== undefined && param !== null && param !== ""
        ? "/" + encodeURIComponent(param)
        : "");
    if (location.hash !== h) {
      _hashLock = true;
      location.hash = h;
      setTimeout(() => {
        _hashLock = false;
      }, 0);
    }
  }
  currentScreen = screen;
  document
    .querySelectorAll(".itab[data-screen]")
    .forEach((n) => n.classList.toggle("active", n.dataset.screen === screen));
  const main = document.getElementById("main-content");
  main.classList.remove("scene-in");
  void main.offsetWidth; // restart the entrance animation
  main.classList.add("scene-in");
  switch (screen) {
    case "today":
      renderToday(main);
      break;
    case "board":
      renderBoard(main);
      break;
    case "goals":
      renderGoals(main);
      break;
    case "new-goal":
      renderNewGoal(main);
      break;
    case "goal":
      renderGoalDetail(main, param);
      break;
    case "schedule":
      renderSchedule(main);
      break;
    case "settings":
      renderSettings(main);
      break;
    default:
      renderToday(main);
  }
  if (token) setTimeout(pollCheckIn, 600);
}

function routeFromHash() {
  const raw = (location.hash || "").replace(/^#/, "");
  if (!raw) return false;
  const [screen, param] = raw.split("/");
  const s = screen || "today";
  if (!KNOWN_SCREENS.includes(s)) return false;
  navigate(s, param ? decodeURIComponent(param) : undefined, true);
  return true;
}
window.addEventListener("hashchange", () => {
  if (_hashLock) return;
  routeFromHash();
});

async function renderToday(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Today —</div><h1>…</h1></div>`;
  try {
    const today = await api("/today");
    const status = await api("/status");

    let html = `<div class="screen-fill">
    <div class="page-header">
      <div class="kicker">Today — ${escHtml(today.date)}</div>
      <h1>The run of show.</h1>
      <p class="sub">${escHtml(today.items.length)} tasks on the board${today.items.length ? " · none of them moved themselves" : "."}</p>
      <div class="header-rule"></div>
    </div>`;

    if (today.interventions && today.interventions.length) {
      html += `<div style="margin-bottom:22px;display:flex;flex-direction:column;gap:10px;">`;
      today.interventions.forEach((iv) => {
        html += `<div class="card" style="border-left:3px solid var(--ember);">
          <p style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;">
            <span style="flex:1;">${escHtml(iv.message)}</span>
            <button class="btn btn-sm btn-ghost" onclick="ackIntervention(${escHtml(iv.id)}, ${escHtml(iv.goal_id)})">Ack</button>
          </p>
        </div>`;
      });
      html += `</div>`;
    }

    try {
      const tr = await api("/triage");
      if (tr.call) {
        const fire = tr.cut && tr.cut.length;
        html += `<div class="card call-card ${fire ? "fire" : ""}">
          <div class="kicker">${fire ? "Eloise's call" : "Priority order"}</div>
          <p class="call-text">${escHtml(tr.call)}</p>
          <div class="call-rank">${tr.ranked
            .slice(0, 4)
            .map(
              (r, i) =>
                `<span class="chip ${i === 0 ? "hot" : "off"}" onclick="navigate('goal', ${r.goal_id})" style="cursor:pointer">${i + 1}. ${escHtml(r.title)} · ${escHtml(r.why)}</span>`,
            )
            .join("")}</div>
        </div>`;
      }
    } catch (e) {
      /* triage is optional */
    }
    try {
      html += await renderDayHero();
    } catch (e) {
      /* dial is optional */
    }
    html += `<div class="schedule-grid">`;
    if (today.items.length === 0) {
      html += `<div class="empty-state compose"><div class="big">&#x2737;</div><h3>Nothing booked today.</h3><p>File a goal and Eloise draws the lines.</p></div>`;
    } else {
      today.items.forEach((a) => {
        const timeStr =
          a.start_time && a.end_time
            ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}`
            : "";
        const cls =
          a.status === "done" ? "done" : a.status === "missed" ? "missed" : "";
        html += `<div class="schedule-day"><div class="schedule-day-head"><span>${escHtml(a.goal_title)}</span><span class="chip ${a.status === "done" ? "ok" : a.status === "missed" ? "bad" : "hot"}">${escHtml(a.status)}</span></div>
          <div class="schedule-row ${cls}">
            <span class="time">${timeStr || "—"}</span>
            <span class="title">${escHtml(a.title)}</span>
            <span class="task-name">${escHtml(a.status)}</span>
          </div>
        </div>`;
      });
    }
    html += `</div>`;

    // Risk board
    try {
      const sb = await api("/scoreboard");
      if (sb.goals && sb.goals.length) {
        html += `<div class="page-header" style="margin-top:44px;"><div class="kicker">Risk Board</div><h1>Who's sliding.</h1><div class="header-rule"></div></div>`;
        sb.goals.forEach((g) => {
          html += `<div class="sb-row">
            <div class="sb-title">${escHtml(g.title)}</div>
            <div class="sb-meta">${escHtml(g.shortfall_hours)}h short · ${escHtml(g.days_left)}d left</div>
            <div class="sb-tag ${escHtml(g.risk)}">${escHtml(g.risk)}</div>
          </div>`;
        });
      }
    } catch (e) {}

    // model status footer
    html += `<div style="margin-top:auto;padding-top:40px;display:flex;align-items:center;gap:10px;color:var(--bone-mute);font-size:12px;flex-wrap:wrap;">
      <span class="ember-dot"></span>
      ${
        status.cloud_configured
          ? `<span class="chip hot">${escHtml(status.provider || "cloud")} armed — ${escHtml(status.cloud_model || "")}</span>`
          : '<span class="chip off">&#x25CB; No API key — add one in Settings</span>'
      }
      <span>${status.cloud_error && !status.cloud_configured ? escHtml(status.cloud_error) : ""}</span>
      ${status.local_model_available ? '<span class="chip hot">local live</span>' : ""}
    </div>
    </div>`; // close .screen-fill

    el.innerHTML = html;
    staggerIn(el, ".schedule-day");
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Today</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}

const _DIAL_COLORS = [
  "#ffb25b",
  "#ff6b86",
  "#a58bff",
  "#64e0b4",
  "#6fb4ff",
  "#ffcf5c",
];
function _hm(t) {
  const [h, m] = (t || "0:0").split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}
function _pt(cx, cy, r, min) {
  const a = (min / 1440) * 2 * Math.PI - Math.PI / 2; // midnight at the top
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
function _arc(cx, cy, r, m0, m1) {
  if (m1 <= m0) m1 += 1440;
  const parts =
    m1 > 1440
      ? [
          [m0, 1440],
          [0, m1 - 1440],
        ]
      : [[m0, m1]];
  return parts
    .map(([a, b]) => {
      if (b - a < 1) return "";
      const [x0, y0] = _pt(cx, cy, r, a),
        [x1, y1] = _pt(cx, cy, r, Math.min(b, 1439.99));
      return `M${x0.toFixed(1)} ${y0.toFixed(1)}A${r} ${r} 0 ${b - a > 720 ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
    })
    .join("");
}
async function renderDayHero() {
  const d = await api("/day");
  const C = 200,
    R = 150,
    RB = 178;
  const goalIdx = {};
  let gi = 0;
  d.items.forEach((a) => {
    if (!(a.goal_id in goalIdx)) goalIdx[a.goal_id] = gi++;
  });
  let svg = `<svg viewBox="0 0 400 400" role="img" aria-label="Your day as a 24-hour dial"><circle class="dd-track" cx="${C}" cy="${C}" r="${R}"/>`;
  d.busy.forEach((b) => {
    svg += `<path class="dd-busy ${escHtml(b.kind)}" d="${_arc(C, C, RB, _hm(b.start), _hm(b.end))}"><title>${escHtml(b.label)} ${escHtml(b.start)}–${escHtml(b.end)}</title></path>`;
  });
  for (let h = 0; h < 24; h += 3) {
    const [x0, y0] = _pt(C, C, R - 20, h * 60),
      [x1, y1] = _pt(C, C, R - 14, h * 60),
      [tx, ty] = _pt(C, C, R - 36, h * 60);
    svg += `<line class="dd-tick" x1="${x0}" y1="${y0}" x2="${x1}" y2="${y1}"/><text class="dd-hour" x="${tx}" y="${ty}">${h === 0 ? "24" : h}</text>`;
  }
  let nowTask = null,
    nextTask = null;
  const nowM = _hm(d.now);
  d.items.forEach((a) => {
    if (!a.start_time || !a.end_time) return;
    const s0 = _hm(a.start_time),
      e0 = _hm(a.end_time);
    const col = _DIAL_COLORS[goalIdx[a.goal_id] % _DIAL_COLORS.length];
    svg += `<path class="dd-task ${escHtml(a.status)}" style="color:${col}" stroke="${col}" d="${_arc(C, C, R, s0, e0)}"><title>${escHtml(fmtTime(a.start_time))} · ${escHtml(a.title)}</title></path>`;
    if (nowM >= s0 && nowM < e0) nowTask = a;
    else if (
      s0 > nowM &&
      a.status === "pending" &&
      (!nextTask || s0 < _hm(nextTask.start_time))
    )
      nextTask = a;
  });
  if (d.is_today) {
    const deg = (nowM / 1440) * 360;
    svg += `<g class="dd-needle" style="transform:rotate(${deg}deg)"><line class="dd-nline" x1="${C}" y1="${C - R + 28}" x2="${C}" y2="${C - RB - 10}"/></g><circle class="dd-needle-dot" cx="${_pt(C, C, RB + 10, nowM)[0]}" cy="${_pt(C, C, RB + 10, nowM)[1]}" r="5"/>`;
  }
  svg += `</svg>`;
  const doneN = d.items.filter((a) => a.status === "done").length;
  const focus = nowTask
    ? `Now: ${nowTask.title}`
    : nextTask
      ? `Next ${fmtTime(nextTask.start_time)}: ${nextTask.title}`
      : d.items.length
        ? "Nothing left to start."
        : "A clear day.";
  const legend = [
    ["#3a2f66", "sleep"],
    ["#5b4a2e", "meals"],
    ["#6b2f46", "busy"],
  ]
    .map(([c, l]) => `<span><i style="background:${c}"></i>${l}</span>`)
    .join("");
  const goals = Object.keys(goalIdx)
    .map((id) => {
      const a = d.items.find((x) => String(x.goal_id) === id);
      return `<span><i style="background:${_DIAL_COLORS[goalIdx[id] % _DIAL_COLORS.length]}"></i>${escHtml(a.goal_title)}</span>`;
    })
    .join("");
  return `<div class="day-hero">
    <div class="daydial">${svg}
      <div class="dd-center"><div class="dd-clock">${escHtml(fmtTime(d.now).replace(/ [AP]M/, ""))}</div>
        <div class="dd-sub">${doneN}/${d.items.length} done</div>
        <div class="dd-now">${escHtml(focus)}</div></div>
    </div>
    <div><div class="kicker">Your 24 hours</div>
      <p class="sub" style="margin-top:0">Outer rail is the stuff you can't touch — sleep, meals, your busy windows. Colour on the track is the work, planned only into hours you actually have.</p>
      <div class="dd-legend">${legend}${goals}</div></div>
  </div>`;
}

async function ackIntervention(interventionId, goalId) {
  try {
    await api(`/interventions/${interventionId}/acknowledge`, {
      method: "POST",
    });
    showToast("Acknowledged", "success");
  } catch (e) {
    showToast("Could not acknowledge", "error");
  }
  navigate("today");
}

async function renderBoard(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Board</div><h1>…</h1></div>`;
  try {
    const [mom, goals] = await Promise.all([api("/momentum"), api("/goals")]);
    let html = `<div class="screen-fill"><div class="page-header"><div class="kicker">Control board</div>
      <h1>The meter's open.</h1>
      <p class="sub">Momentum, pressure, and the bets on the line — all live, all straight.</p>
      <div class="header-rule"></div>
    </div>`;

    // momentum dial
    const m = mom;
    const momCls = m.momentum >= 70 ? "ok" : m.momentum >= 40 ? "hot" : "bad";
    html += `<div class="card momentum-card">
      <div style="display:flex;gap:28px;align-items:center;flex-wrap:wrap;">
        <div class="dial" style="--p:${m.momentum}">
          <div class="dial-in"><span class="dial-num">${m.momentum}</span><span class="dial-cap">/100</span></div>
        </div>
        <div style="flex:1;min-width:220px;">
          <div class="kicker">Momentum</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;margin:10px 0;">
            <span class="chip ${momCls}">${m.momentum >= 70 ? "on fire" : m.momentum >= 40 ? "ticking" : "stalled"}</span>
            <span class="chip off">best run ${m.best_streak}d</span>
            <span class="chip off">current streak ${m.current_streak}d</span>
            <span class="chip off">${m.burn_hours_per_day}h/day burn</span>
          </div>
          <p class="sub" style="margin:0;">${m.done_days_total} days with real completed tasks count toward this.</p>
        </div>
      </div>
    </div>`;

    // pressure ranked list
    html += `<div class="page-header" style="margin-top:36px;"><div class="kicker">Pressure ranking</div>
      <h1>Who's sliding.</h1><div class="header-rule"></div></div>`;
    const withP = goals
      .filter((g) => g.pressure)
      .sort((a, b) => b.pressure.score - a.pressure.score);
    if (withP.length === 0) {
      html += `<div class="empty-state"><div class="big">&#x2737;</div><h3>No pressure yet.</h3><p>File a goal and it starts registering.</p></div>`;
    } else {
      html += `<div class="board-rows">`;
      withP.forEach((g) => {
        const pc = g.pressure.score;
        const cls = pc >= 8 ? "bad" : pc >= 6 ? "hot" : pc >= 4 ? "warn" : "ok";
        const barCls =
          pc >= 8 ? "bad" : pc >= 6 ? "hot" : pc >= 4 ? "warn" : "ok";
        html += `<div class="goal-card pressure-row" onclick="navigate('goal', ${g.id})">
          <div style="flex:1;">
            <div class="title">${escHtml(g.display_title)}</div>
            <div class="meta">
              <span class="chip off">${g.days_left}d left</span>
              ${g.pressure.remaining_hours ? `<span class="chip off">${escHtml(g.pressure.remaining_hours)}h open</span>` : ""}
              <span class="chip ${cls}">${pc}/10 — ${escHtml(g.pressure.label)}</span>
            </div>
          </div>
          <div class="pressure-meter"><span class="seg ${pc >= 8 ? "on" : ""}"></span><span class="seg ${pc >= 7 ? "on" : ""}"></span><span class="seg ${pc >= 5 ? "on" : ""}"></span><span class="seg ${pc >= 3 ? "on" : ""}"></span><span class="seg ${pc >= 1 ? "on" : ""}"></span><span class="seg-label">${pc}</span></div>
        </div>`;
      });
      html += `</div>`;
    }

    html += `</div>`; // close .screen-fill
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Board</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}
async function callInDebt(goalId) {
  try {
    const r = await api(`/goals/${goalId}/stake/enforce`, { method: "POST" });
    if (r.roast) showToast(r.roast, "error");
    navigate("board");
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function renderGoals(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Goals</div><h1>…</h1></div>`;
  try {
    const goals = await api("/goals");
    let html = `<div class="screen-fill"><div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-end;">
      <div><div class="kicker">Archive</div><h1>Open files.</h1><p class="sub">${goals.length} total</p></div>
      <button class="btn" style="width:auto;" onclick="navigate('new-goal')">+ File</button>
    </div>`;
    if (goals.length === 0) {
      html += `<div class="empty-state compose"><div class="big">&#x2737;</div><h3>Nothing on file.</h3><p>Give Eloise something real to chase.</p><p style="margin-top:14px;"><button class="btn" style="width:auto;" onclick="navigate('new-goal')">File a goal</button></p></div>`;
    } else {
      html += `<div class="goal-list">`;
      goals.forEach((g) => {
        const riskCls =
          g.days_left < 0 ? "overdue" : g.days_left <= 2 ? "at_risk" : "";
        const statusText =
          g.status === "succeeded"
            ? "DONE"
            : g.status === "failed"
              ? "FAILED"
              : g.days_left < 0
                ? "OVERDUE"
                : `${g.days_left}d`;
        const tagCls =
          g.status === "succeeded"
            ? "ok"
            : g.days_left < 0
              ? "bad"
              : g.days_left <= 2
                ? "hot"
                : "";
        html += `<div class="goal-card ${riskCls}" onclick="navigate('goal', ${g.id})">
          <div class="title">${escHtml(g.display_title)}</div>
          <div class="meta">
            <span class="chip ${tagCls}">${escHtml(statusText)}</span>
            <span>Due ${escHtml(g.deadline)}</span>
            <span class="chip off">${escHtml(g.plan_status)}</span>
            ${g.open_intervention_type ? `<span class="chip bad">flag: ${escHtml(g.open_intervention_type)}</span>` : ""}
          </div>
        </div>`;
      });
      html += `</div>`;
    }
    html += `</div>`; // close .screen-fill
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Goals</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}

async function renderGoalDetail(el, id) {
  el.innerHTML = `<div class="page-header"><div class="kicker">On file</div><h1>…</h1></div>`;
  try {
    const g = await api(`/goals/${id}`);
    let html = `<div class="page-header">
      <div class="kicker">On file · #${escHtml(g.id)}</div>
      <h1>${escHtml(g.display_title)}</h1>
      <p class="sub">Due ${escHtml(g.deadline)} · ${g.days_left >= 0 ? escHtml(g.days_left) + " days left" : "OVERDUE"} · ${escHtml(g.plan_status)}</p>
      <div class="header-rule"></div>
    </div>`;

    // actions
    html += `<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px;">
      <button class="btn btn-sm btn-ghost" onclick="regenPlan(${g.id})">Redraw schedule</button>
      <button class="btn btn-sm btn-ghost" onclick="completeGoalFlow(${g.id})">Mark done</button>
      <button class="btn btn-sm btn-ghost" onclick="cancelGoalFlow(${g.id})">Cancel / Delay</button>
    </div>`;

    // pressure meter header strip
    if (g.pressure) {
      const pc = g.pressure.score;
      const pcl = pc >= 8 ? "bad" : pc >= 6 ? "hot" : pc >= 4 ? "warn" : "ok";
      html += `<div class="card pressure-row" style="margin-bottom:24px;">
        <div style="flex:1;">
          <div class="kicker">Pressure</div>
          <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
            <div style="font-size:34px;font-weight:800;color:${pc >= 8 ? "#ff8b7a" : pc >= 6 ? "var(--ember)" : "var(--copper)"};">${pc}<span style="font-size:15px;color:var(--bone-dim);">/10</span></div>
            <div class="pressure-meter"><span class="seg ${pc >= 8 ? "on" : ""}"></span><span class="seg ${pc >= 7 ? "on" : ""}"></span><span class="seg ${pc >= 5 ? "on" : ""}"></span><span class="seg ${pc >= 3 ? "on" : ""}"></span><span class="seg ${pc >= 1 ? "on" : ""}"></span></div>
            <span class="chip ${pcl}">${escHtml(g.pressure.label)}</span>
            <span class="chip off">${g.pressure.remaining_hours}h open</span>
          </div>
        </div>
      </div>`;
    }

    try {
      const rt = await api("/routine");
      html += routineCardHtml(rt);
    } catch (e) {
      /* optional */
    }

    // roadmap: monthly/weekly outline for far-off goals
    try {
      const ro = await api(`/outline/${g.id}`);
      if (ro.outline && ro.outline.periods && ro.outline.periods.length) {
        const t0 = localISO(new Date());
        html += `<div class="card roadmap"><div class="kicker">${ro.scope === "monthly" ? "Month-by-month roadmap" : "Week-by-week roadmap"}</div>
          <p class="sub" style="margin:0 0 14px;">Hour-by-hour tasks are drawn for the next two weeks and roll forward every day. Everything past that is the plan's shape.</p>`;
        ro.outline.periods.forEach((pr) => {
          const now = pr.start <= t0 && t0 <= pr.end;
          html += `<div class="road-row ${now ? "now" : ""}">
            <div class="road-when">${escHtml(pr.start.slice(5))} → ${escHtml(pr.end.slice(5))}${now ? ' <span class="chip hot">now</span>' : ""}</div>
            <div class="road-body"><strong>${escHtml(pr.phase)}</strong> <span class="chip off">${escHtml(pr.hours)}h available</span>
              <div class="road-obj">${pr.objective ? escHtml(pr.objective) : '<span style="color:var(--text-mute)">objective not written yet — Redraw schedule</span>'}</div></div>
          </div>`;
        });
        html += `</div>`;
      }
    } catch (e) {
      /* roadmap is optional */
    }

    // tabs
    html += `<div class="tabs"><span class="tab active" onclick="switchTab('schedule','${g.id}')">Schedule</span><span class="tab" onclick="switchTab('progress','${g.id}')">Progress</span><span class="tab" onclick="switchTab('constraints','${g.id}')">Constraints</span></div>`;

    // schedule
    html += `<div id="detail-schedule"><div class="schedule-grid">`;
    const byDate = {};
    (g.plan || []).forEach((a) => {
      if (a.date) (byDate[a.date] = byDate[a.date] || []).push(a);
    });
    const dates = Object.keys(byDate).sort();
    if (dates.length === 0) {
      if (String(g.plan_status).startsWith("blocked") && g.plan_summary) {
        html += `<div class="empty-state"><h3>No free time to schedule</h3><p style="max-width:420px;font-size:14px;line-height:1.7;color:var(--bone-dim);">${escHtml(g.plan_summary)}</p><p style="font-size:13px;color:var(--copper);">Eloise plans every goal around the same windows — tell Eloise in chat which window to free up, then Redraw schedule.</p></div>`;
      } else {
        html += `<div class="empty-state"><h3>No schedule drawn yet</h3></div>`;
      }
    } else {
      dates.forEach((d) => {
        const isPast = d < localISO(new Date());
        html += `<div class="schedule-day"><div class="schedule-day-head"><span>${escHtml(d)}</span><span class="chip hot">${byDate[d].length}</span></div>`;
        byDate[d].forEach((a) => {
          const timeStr =
            a.start_time && a.end_time
              ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}`
              : "";
          const done = a.status === "done";
          const missed = a.status === "missed" && !done;
          const cls = done ? "done" : missed ? "missed" : "";
          html += `<div class="schedule-row ${cls}">
            <span class="time">${timeStr || "—"}</span>
            <span class="title">${escHtml(a.title)}${missed ? '<span class="chip warn row-flag">FAILED</span>' : ""}</span>
            <span class="task-name"><button class="btn btn-sm ${done ? "btn-ghost" : missed ? "btn-warn" : ""}" onclick="toggleTask(${a.id}, this)">${done ? "REOPEN" : "DONE"}</button></span>
          </div>`;
        });
        html += `</div>`;
      });
    }
    html += `</div></div>`;

    // progress (hidden)
    const p = g.progress;
    html += `<div id="detail-progress" style="display:none;">
      <div class="card" style="margin-bottom:16px;"><p style="font-size:14px;line-height:1.7;color:var(--bone-dim);">${escHtml(p.narrative)}</p></div>
      <div class="stat-grid">
        <div class="stat-tile"><div class="k">Planned</div><div class="v">${fmtHours(p.total_planned_hours)}</div></div>
        <div class="stat-tile"><div class="k">Completed</div><div class="v">${fmtHours(p.completed_hours)}</div></div>
        <div class="stat-tile"><div class="k">Available</div><div class="v">${fmtHours(p.available_hours)}</div></div>
        <div class="stat-tile"><div class="k">Status</div><div class="v" style="color:${p.status === "safe" ? "var(--ok)" : p.status === "overdue" ? "#ff8b7a" : "var(--copper)"};">${escHtml(String(p.status).toUpperCase())}</div></div>
      </div>
    </div>`;

    // constraints (hidden)
    html += `<div id="detail-constraints" style="display:none;">`;
    const cons = g.constraints || [];
    if (cons.length === 0) {
      html += `<div class="empty-state"><h3>No constraints</h3><p>Tell Eloise what's off-limits.</p></div>`;
    } else {
      cons.forEach(
        (c, ci) =>
          (html += `<div class="note-card"><span class="who">blocked</span><div style="flex:1">${escHtml(c)}</div><button class="btn btn-sm btn-ghost" onclick="removeGoalConstraint(${g.id}, ${ci})">&times;</button></div>`),
      );
    }
    html += `<div class="field" style="margin-top:16px;"><label>Add a constraint</label>
      <div style="display:flex;gap:10px;"><input type="text" id="new-cons-input" placeholder="e.g. Gym 5-7pm"><button class="btn btn-sm btn-ghost" onclick="addGoalConstraint(${g.id})">+</button></div>
    </div></div>`;

    html += `<div class="goal-chat">
      <div class="kicker">Talk on this file</div>
      <div id="goalchat-container" class="chat-container"></div>
      <div class="chat-input"><input type="text" id="goalchat-input" placeholder="Ask Eloise about this goal..."><button class="btn btn-sm" onclick="sendGoalChat(${g.id})">Send</button></div>
    </div>`;

    el.innerHTML = html;
    staggerIn(el, ".schedule-day");
    if (g.plan_status === "generating") {
      const note = document.createElement("div");
      note.className = "plan-generating";
      note.innerHTML = planProgressHtml(["Starting"], 0, 0);
      el.prepend(note);
      waitPlanSettles(g.id, 150, (prog, sec) => {
        if (currentScreen !== "goal") return;
        note.innerHTML = planProgressHtml(
          prog.steps,
          prog.at < 0 ? 0 : prog.at,
          sec,
        );
      }).then((r) => {
        if (r && currentScreen === "goal") renderGoalDetail(el, g.id);
      });
    }
    mountGoalChat(g.id);
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">On file</div><h1>Missing file.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}
function switchTab(name, id) {
  document
    .querySelectorAll("#main-content .tab")
    .forEach((t) => t.classList.remove("active"));
  event.target.classList.add("active");
  document.getElementById("detail-schedule").style.display =
    name === "schedule" ? "" : "none";
  document.getElementById("detail-progress").style.display =
    name === "progress" ? "" : "none";
  document.getElementById("detail-constraints").style.display =
    name === "constraints" ? "" : "none";
}
async function addGoalConstraint(id) {
  const txt = document.getElementById("new-cons-input").value;
  if (!txt) return;
  try {
    await api(`/goals/${id}/constraints`, {
      method: "POST",
      body: JSON.stringify({ text: txt }),
    });
  } catch (e) {}
  navigate("goal", id);
}
async function removeGoalConstraint(id, idx) {
  try {
    await api(`/goals/${id}/constraints`, {
      method: "POST",
      body: JSON.stringify({ removeIndex: idx }),
    });
  } catch (e) {}
  navigate("goal", id);
}
async function completeGoal(id) {
  try {
    await api(`/goals/${id}/complete`, {
      method: "POST",
      body: JSON.stringify({ claimed_success: true }),
    });
    showToast("Closed out", "success");
  } catch (e) {}
  navigate("goals");
}
async function completeGoalFlow(id) {
  const reason = await askModal(
    "Mark done?",
    "Confirm this goal is actually complete. No lies — the next check-in will find out.",
  );
  if (reason === null) return; // user cancelled
  try {
    await api(`/goals/${id}/complete`, {
      method: "POST",
      body: JSON.stringify({
        claimed_success: true,
        reason: reason || "User confirmed completion",
      }),
    });
    showToast(
      "Closed out. If you're lying, the next check-in will find out.",
      "success",
    );
    navigate("goals");
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function cancelGoalFlow(id) {
  const reason = await askModal(
    "Cancel or delay?",
    'Give a real reason — exam cancelled, project delayed, scope changed. Not "I don\'t feel like it".',
  );
  if (reason === null || !reason) return;
  try {
    const r = await api(`/goals/${id}/complete`, {
      method: "POST",
      body: JSON.stringify({ claimed_success: false, reason }),
    });
    showToast(r.roast || "File closed.", "success");
    navigate("goals");
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function pinStake(id) {
  const punishment = await askModal(
    "Pin a stake",
    "What happens if this slips? e.g. \u00A350 to a charity I hate",
  );
  if (!punishment) return;
  try {
    await api(`/goals/${id}/stake`, {
      method: "POST",
      body: JSON.stringify({ goal_id: id, punishment }),
    });
    showToast("Staked. It\u2019s on the line now.", "success");
    navigate("goal", id);
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function refreshGoal(id) {
  const main = document.getElementById("main-content");
  if (main && currentScreen === "goal") await renderGoalDetail(main, id);
}

function planProgressHtml(steps, at, sec, title) {
  return `<div><strong>${escHtml(title || "Drawing your schedule")}</strong>
    <ul class="plan-steps">${steps
      .map((t, i) => {
        const st = i < at ? "done" : i === at ? "live" : "";
        return `<li class="${st}"><i></i><span>${escHtml(t)}</span><b>${i === at ? sec + "s" : ""}</b></li>`;
      })
      .join("")}</ul></div>`;
}

async function waitPlanSettles(id, attempts = 150, onTick) {
  const t0 = Date.now();
  for (let i = 0; i < attempts; i++) {
    let prog = null;
    try {
      prog = await api(`/goals/${id}/plan-progress`);
    } catch (e) {}
    if (prog && onTick) onTick(prog, Math.floor((Date.now() - t0) / 1000));
    if (i % 2 === 0) {
      try {
        const g = await api(`/goals/${id}`);
        if (g.plan_status !== "generating") return g;
      } catch (e) {}
    }
    await new Promise((r) => setTimeout(r, 1200));
  }
  return null;
}

async function regenPlan(id) {
  const main = document.getElementById("main-content");
  if (!main) return;
  const note = document.createElement("div");
  note.className = "plan-generating";
  note.id = "plan-generating-note";
  note.innerHTML = planProgressHtml(
    ["Waiting for a free slot in the queue"],
    0,
    0,
    "Redraw started",
  );
  main.prepend(note);
  note.scrollIntoView({ behavior: "smooth", block: "center" });
  let posted = false;
  try {
    await api(`/goals/${id}/plan`, { method: "POST" });
    posted = true;
  } catch (e) {
    showToast(e.message, "error");
  }
  let settled = null;
  if (posted)
    settled = await waitPlanSettles(id, 150, (prog, sec) => {
      note.innerHTML = planProgressHtml(
        prog.steps,
        prog.at < 0 ? 0 : prog.at,
        sec,
        "Your current schedule stays until the new one lands",
      );
    });
  const nx = document.getElementById("plan-generating-note");
  if (nx) nx.remove();
  if (posted && settled && String(settled.plan_status).startsWith("blocked")) {
    showToast(
      "No free time to schedule. Tell Eloise in chat which window to free up, then redraw: " +
        (settled.plan_summary || ""),
      "error",
    );
  } else if (
    posted &&
    settled &&
    String(settled.plan_summary).startsWith("kept previous")
  ) {
    showToast(
      "Couldn’t reach the model — kept your current schedule. Retrying in a minute.",
      "error",
    );
  } else if (posted && settled) {
    showToast(
      "New schedule drawn" +
        (settled.plan_summary ? " — " + settled.plan_summary : "") +
        ".",
      "success",
    );
  } else if (posted) {
    showToast(
      "Still drawing in the background — it will appear here shortly.",
      "info",
    );
  }
  try {
    if (currentScreen === "goal") await renderGoalDetail(main, id);
  } catch (e) {}
}

function staggerIn(root, sel) {
  (root || document).querySelectorAll(sel).forEach((n, i) => {
    n.style.setProperty("--i", Math.min(i, 24));
    n.classList.add("stagger-in");
  });
}

async function deleteGoal(id) {
  const ok = await confirmModal(
    "Burn the file?",
    "This deletes the goal and every schedule tied to it. Eloise will not miss it.",
    "Burn it",
    true,
  );
  if (!ok) return;
  try {
    const r = await api(`/goals/${id}/delete`, {
      method: "POST",
      body: JSON.stringify({ reason: "User deleted" }),
    });
    if (r.roast) showToast(r.roast, "error");
    navigate("goals");
  } catch (e) {}
}

function renderNewGoal(el) {
  let html = `<div class="page-header"><div class="kicker">New file</div><h1>What's on the table.</h1><p class="sub">Give it a name, a date, and what's off-limits.</p><div class="header-rule"></div></div>
    <div class="card">
      <div class="field"><label>What needs to get done?</label><input type="text" id="new-goal-title" placeholder="e.g. Study networking exam"></div>
      <div class="field"><label>Deadline</label><input type="date" id="new-goal-deadline"></div>
      <div class="field"><label>Daily reminder time</label><input type="time" id="new-goal-reminder" value="08:00"></div>
      <div class="field"><label>Constraints</label>
        <div id="constraints-list"></div>
        <div style="display:flex;gap:8px;margin-top:8px;">
          <input type="text" id="new-constraint" placeholder="e.g. Gym 5-7pm">
          <button class="btn btn-sm btn-ghost" onclick="addConstraint()">+ Add</button>
        </div>
      </div>
      <button class="btn" style="margin-top:8px;" onclick="createGoal()">File it</button>
    </div>`;
  window._constraints = [];
  el.innerHTML = html;
}
function addConstraint() {
  const inp = document.getElementById("new-constraint");
  const val = (inp.value || "").trim();
  if (!val) return;
  window._constraints.push(val);
  inp.value = "";
  renderConstraintChips();
}
function renderConstraintChips() {
  const wrap = document.getElementById("constraints-list");
  if (!wrap) return;
  wrap.innerHTML = (window._constraints || [])
    .map(
      (c, i) =>
        `<span class="chip hot" style="margin:3px 4px 3px 0;cursor:pointer;" onclick="removeConstraint(${i})">${escHtml(c)} &times;</span>`,
    )
    .join("");
}
function removeConstraint(i) {
  window._constraints.splice(i, 1);
  renderConstraintChips();
}
async function createGoal() {
  const title = document.getElementById("new-goal-title").value;
  const deadline = document.getElementById("new-goal-deadline").value;
  const reminder = document.getElementById("new-goal-reminder").value;
  if (!title || !deadline) {
    showToast("Name and deadline needed", "error");
    return;
  }
  try {
    const r = await api("/goals", {
      method: "POST",
      body: JSON.stringify({
        title,
        deadline,
        reminder_time: reminder,
        constraints: window._constraints || [],
      }),
    });
    let assumed = [];
    try {
      const c = await api(`/goals/${r.id}/clarify`, { method: "POST" });
      assumed = c.assumed || [];
      if (c.questions && c.questions.length > 0) {
        showQuestionnaire(r.id, title, c.questions);
        return;
      }
    } catch (qe) {
      /* model unreachable: no questions, assumptions below */
    }
    showToast(
      assumed.length
        ? `Filed. No questions (model offline), so I assumed ${assumed.join("; ")}. Tell Eloise in chat to change it.`
        : "Filed. Eloise draws the lines.",
      "success",
    );
    navigate("goal", r.id);
  } catch (e) {
    showToast(e.message, "error");
  }
}

function showQuestionnaire(goalId, goalTitle, questions) {
  const main = document.getElementById("main-content");
  let html = `<div class="page-header"><div class="kicker">Filing: ${escHtml(goalTitle)}</div>
    <h1>Quick ones.</h1><p class="sub">Only what I can't work out myself. Skip any you like.</p>
    <div class="header-rule"></div></div>`;
  html += `<form id="questionnaire-form" onsubmit="submitQuestionnaire(event, ${goalId})">`;
  questions.forEach((q, i) => {
    html += `<div class="card" style="margin-bottom:16px;">
      <label style="display:block;font-weight:700;color:var(--bone);margin-bottom:6px;">${escHtml(q.q)}</label>
      <input type="text" name="${escHtml(q.key)}" placeholder="${escHtml(q.hint)}" style="width:100%;padding:10px;background:var(--ink);border:1px solid var(--line);border-radius:4px;color:var(--bone);font-size:14px;">
    </div>`;
  });
  html += `<div style="display:flex;gap:10px;">
    <button type="submit" class="btn">Build my schedule</button>
    <button type="button" class="btn btn-ghost" onclick="navigate('goals')">Skip</button>
  </div></form>`;
  main.innerHTML = html;
}

async function submitQuestionnaire(e, goalId) {
  e.preventDefault();
  const form = e.target;
  const data = {};
  new FormData(form).forEach((v, k) => {
    if (v.trim()) data[k] = v.trim();
  });
  try {
    await api(`/goals/${goalId}/details`, {
      method: "POST",
      body: JSON.stringify(data),
    });
    showToast("Details saved. Schedule regenerating...", "success");
  } catch (err) {
    showToast(err.message, "error");
  }
  navigate("goal", goalId);
}

async function renderSchedule(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Schedule</div><h1>…</h1></div>`;
  try {
    const payload = await api("/schedule");
    const today = payload.today || localISO(new Date());
    const missedTotal = payload.missed_total || 0;
    let html = `<div class="page-header"><div class="kicker">15 days out</div><h1>The run of show.</h1><p class="sub">Tap a day to open it. Tick tasks as they close. Overdue sits on top until you deal with it.</p><div class="header-rule"></div></div>`;
    if (missedTotal) {
      html += `<div class="banner banner-bad"><strong>${missedTotal} task${missedTotal === 1 ? "" : "s"} slipped.</strong> They're marked failed until you close them out or redraw the plan.</div>`;
    }
    const dates = {};
    const goalsMap = payload.dates || {};
    for (const goalId of Object.keys(goalsMap)) {
      const data = goalsMap[goalId];
      for (const dateStr of Object.keys(data.by_date)) {
        (dates[dateStr] = dates[dateStr] || []).push(
          ...data.by_date[dateStr].map((a) => ({ ...a, goal: data.title })),
        );
      }
    }
    // Past-due first (most recent miss on top), today, then the future.
    const sorted = Object.keys(dates).sort((a, b) => {
      const ap = a < today,
        bp = b < today;
      if (ap !== bp) return ap ? -1 : 1;
      return ap ? b.localeCompare(a) : a.localeCompare(b);
    });
    if (sorted.length === 0) {
      html += `<div class="empty-state"><h3>Nothing booked.</h3><p>File a goal to start the run.</p></div>`;
    } else {
      html += `<div class="schedule-grid">`;
      sorted.forEach((d, di) => {
        const list = dates[d].sort((x, y) =>
          (x.start_time || "").localeCompare(y.start_time || ""),
        );
        const doneCount = list.filter((a) => a.status === "done").length;
        const missedCount = list.filter((a) => a.status === "missed").length;
        const total = list.length;
        const isToday = d === today;
        const isPast = d < today;
        const pct = total ? Math.round((doneCount / total) * 100) : 0;
        const open = isToday || isPast || di < 2 ? "open" : "";
        const dayChip = isToday
          ? '<span class="chip bad">NOW</span>'
          : isPast && missedCount
            ? `<span class="chip warn">${missedCount} MISSED</span>`
            : "";
        html += `<div class="schedule-day ${open} ${isPast && missedCount ? "has-missed" : ""}">
          <div class="schedule-day-head sdh-click" onclick="toggleDay(this)">
            <span class="sdh-left">
              <span class="caret">&#9656;</span>
              <span>${escHtml(d)}</span>
              ${dayChip}
            </span>
            <span class="sdh-right"><span class="chip ${doneCount === total ? (doneCount ? "ok" : "") : doneCount ? "hot" : "off"}">${doneCount}/${total} done</span><span class="prog-bar"><span style="width:${pct}%"></span></span></span>
          </div>
          <div class="sdh-body">`;
        list.forEach((a) => {
          const timeStr =
            a.start_time && a.end_time
              ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}`
              : "";
          const done = a.status === "done";
          const missed = a.status === "missed" && !done;
          const cls = done ? "done" : missed ? "missed" : "";
          const nowHere =
            isToday &&
            a.start_time &&
            a.end_time &&
            inNowRange(a.start_time, a.end_time);
          const rowLabel = missed
            ? '<span class="chip warn row-flag">FAILED</span>'
            : "";
          html += `<div class="schedule-row ${cls} ${nowHere ? "now" : ""}">
            <span class="time">${nowHere ? '<span class="pulse-now">&#9679;</span> ' : ""}${timeStr || "—"}</span>
            <span class="title">${escHtml(a.title)}${rowLabel}<span class="task-name-inline">${escHtml(a.goal)}</span></span>
            <span class="task-name">
              <button class="btn btn-sm ${done ? "btn-ghost" : missed ? "btn-warn" : ""}" onclick="toggleTask(${a.id}, this)">${done ? "REOPEN" : "DONE"}</button>
            </span>
          </div>`;
        });
        html += `</div></div>`;
      });
      html += `</div>`;
    }
    el.innerHTML = html;
    staggerIn(el, ".schedule-day");
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Schedule</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}

function localISO(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function inNowRange(s, e) {
  const now = new Date();
  const mins = now.getHours() * 60 + now.getMinutes();
  const sc = (s || "").split(":").map(Number),
    ec = (e || "").split(":").map(Number);
  if (sc.length < 2 || ec.length < 2 || isNaN(sc[0])) return false;
  const sm = sc[0] * 60 + sc[1],
    em = ec[0] * 60 + ec[1];
  return mins >= sm && mins < em;
}
function toggleDay(head) {
  head.parentElement.classList.toggle("open");
}
async function toggleTask(id, btn) {
  const row = btn.closest(".schedule-row");
  try {
    const r = await api(`/actions/${id}/toggle`, { method: "POST" });
    const done = r.status === "done";
    row.classList.toggle("done", done);
    row.classList.toggle("missed", !done && r.status === "missed");
    btn.textContent = done ? "REOPEN" : "DONE";
    btn.classList.toggle("btn-ghost", done);
    const flag = row.querySelector(".row-flag");
    if (flag) flag.style.display = done ? "none" : "";
    const day = row.closest(".schedule-day");
    const head = day && day.querySelector(".sdh-right");
    if (!head) return;
    const bar = head.querySelector(".prog-bar>span");
    // refetch to recompute counters
    const items = Array.from(
      row.parentElement.querySelectorAll(".schedule-row"),
    );
    const t = items.length,
      dnc = items.filter((r2) => r2.classList.contains("done")).length;
    const chip = head.querySelector(".chip");
    chip.textContent = `${dnc}/${t} done`;
    chip.className = "chip " + (dnc === t ? "ok" : dnc ? "hot" : "off");
    if (bar) bar.style.width = Math.round((dnc / t) * 100) + "%";
  } catch (e) {
    showToast(e.message, "error");
  }
}

let _chatMounted = false;

function _bindKeys() {
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      const d = document.getElementById("desk-input");
      if (d) d.focus();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (e.target && e.target.tagName) || "";
    if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
    if (e.key === "Escape") {
      toggleChatPanel(false);
      return;
    }
    const map = {
      1: "today",
      2: "board",
      3: "goals",
      4: "new-goal",
      5: "schedule",
      6: "settings",
    };
    if (map[e.key]) {
      navigate(map[e.key]);
      return;
    }
    if (e.key === "c" || e.key === "C") toggleChatPanel();
    if (e.key === "/") {
      e.preventDefault();
      const d = document.getElementById("desk-input");
      if (d) d.focus();
    }
  });
}

const _DESK_HINTS = [
  "Tell Eloise what you finished...",
  "Ask what to cut today...",
  'Say "show my schedule"...',
  'Say "I sleep 1am to 9am"...',
  'Say "cancel the exam goal"...',
];
let _deskHint = 0;
function deskSend(e) {
  if (e) e.preventDefault();
  const inp = document.getElementById("desk-input");
  const v = (inp.value || "").trim();
  if (!v) {
    toggleChatPanel();
    return false;
  }
  inp.value = "";
  sendGlobalChat(v);
  return false;
}
function _deskInit() {
  const inp = document.getElementById("desk-input");
  if (!inp || inp._init) return;
  inp._init = true;
  setInterval(() => {
    if (document.activeElement === inp || inp.value) return;
    _deskHint = (_deskHint + 1) % _DESK_HINTS.length;
    inp.placeholder = _DESK_HINTS[_deskHint];
  }, 6000);
}

function toggleChatPanel(force) {
  const p = document.getElementById("chat-panel");
  if (!p) return;
  const open = force === undefined ? !p.classList.contains("open") : force;
  p.classList.toggle("open", open);
  p.setAttribute("aria-hidden", open ? "false" : "true");
  document.body.classList.toggle("chat-open", open);
  const dot = document.getElementById("orbit-dot");
  if (dot) dot.classList.toggle("on", open);
  if (open) {
    if (!_chatMounted) mountChatPanel();
    const inp = document.getElementById("global-chat-input");
    if (inp) setTimeout(() => inp.focus(), 120);
  }
}
function openChatPanel() {
  toggleChatPanel(true);
}

async function mountChatPanel() {
  if (_chatMounted) return;
  _chatMounted = true;
  const chatEl = document.getElementById("chat-container");
  const inp = document.getElementById("global-chat-input");
  if (inp) {
    inp.onkeypress = (e) => {
      if (e.key === "Enter") sendGlobalChat();
    };
  }
  refreshPanelModel();
  if (!chatEl) return;
  try {
    const h = await api("/chat");
    if (h.messages && h.messages.length) {
      h.messages.forEach((m) => appendChatMsg(chatEl, m.role, m.content));
    } else {
      appendChatMsg(
        chatEl,
        "eloise",
        "Seat's warm. Say something — or don't, I'll wait.",
      );
    }
  } catch (e) {
    const err = document.createElement("div");
    err.className = "chat-msg eloise chat-error";
    err.textContent = `Couldn't load the thread: ${e.message}`;
    chatEl.appendChild(err);
  }
}

/* Live model/provider line in the panel header — so "is chat even on?" is
   answerable at a glance instead of guessing from a dead screen. */
async function refreshPanelModel() {
  const el = document.getElementById("panel-model");
  if (!el) return;
  try {
    const s = await api("/status");
    if (s.cloud_configured) {
      el.textContent = `${s.provider || "cloud"} · ${s.cloud_model || "model"} — live`;
      el.className = "panel-model live";
    } else {
      el.textContent = "no model armed — open Settings";
      el.className = "panel-model dead";
    }
  } catch (e) {
    el.textContent = "line check failed";
    el.className = "panel-model dead";
  }
}
function _srcTag(source) {
  if (
    !source ||
    [
      "fallback",
      "completion",
      "guardrail",
      "proposal",
      "routine",
      "quick",
      "nav",
    ].includes(source)
  )
    return null;
  const tag = document.createElement("span");
  tag.className = "chat-src";
  tag.textContent = source === "ollama" ? "local" : source;
  return tag;
}

function appendChatMsg(container, role, text, source) {
  const div = document.createElement("div");
  div.className = "chat-msg " + role;
  div.innerHTML = linkify(text);
  const tag = role === "eloise" ? _srcTag(source) : null;
  if (tag) div.appendChild(tag);
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

/* Progress card: only steps that really happened. Server stage frames tick the
   board step and name the model; the timer is the real wait. */
function addTyping(container, opts) {
  const o = Object.assign({ llm: true }, opts || {});
  const div = document.createElement("div");
  div.className = "chat-msg eloise working";
  const steps = o.llm
    ? [
        ["got", "Got your message", "done"],
        ["board", "Reading your board and routine", "live"],
        ["model", "Waiting on the model", "idle"],
      ]
    : [
        ["got", "Got it", "done"],
        ["board", "Checking your board", "live"],
      ];
  div.innerHTML =
    '<ul class="steps">' +
    steps
      .map(
        ([k, t, st]) =>
          `<li data-k="${k}" class="${st}"><i></i><span>${t}</span><b></b></li>`,
      )
      .join("") +
    '</ul><div class="work-note"></div>';
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  const t0 = Date.now();
  div._label = "the model";
  div._timer = setInterval(() => {
    if (!div.parentNode) {
      clearInterval(div._timer);
      return;
    }
    const sec = Math.floor((Date.now() - t0) / 1000);
    const live = div.querySelector("li.live b");
    if (live) live.textContent = sec + "s";
    const note = div.querySelector(".work-note");
    if (note) {
      if (sec >= 45)
        note.textContent = `Slow answer from ${div._label}. Still waiting. It won't get lost, keep working if you want.`;
      else if (sec >= 14)
        note.textContent = "Long one. I am still on it, you can keep working.";
    }
  }, 500);
  return div;
}
function stageEvent(bubble, ev) {
  if (!bubble || !bubble.classList.contains("working")) return;
  const set = (k, st, text) => {
    const li = bubble.querySelector(`li[data-k="${k}"]`);
    if (!li) return;
    li.className = st;
    if (text) li.querySelector("span").textContent = text;
    if (st === "done") li.querySelector("b").textContent = "";
  };
  if (ev.stage === "board") {
    set("board", "done");
  }
  if (ev.stage === "model") {
    bubble._label = ev.label || "the model";
    set("board", "done");
    set("model", "live", "Waiting on " + bubble._label);
  }
}
function _stopWorking(bubble) {
  if (bubble._timer) {
    clearInterval(bubble._timer);
    bubble._timer = null;
  }
  bubble.classList.remove("working");
}
function finishTyping(bubble, text, source) {
  if (bubble._writer) bubble._writer.stop();
  _stopWorking(bubble);
  bubble.innerHTML = "";
  const txt = document.createElement("span");
  txt.innerHTML = linkify(text);
  bubble.appendChild(txt);
  const tag = _srcTag(source);
  if (tag) bubble.appendChild(tag);
  if (bubble.parentNode)
    bubble.parentNode.scrollTop = bubble.parentNode.scrollHeight;
}

function makeWriter(bubble, scrollEl) {
  const w = {
    tokens: [],
    shown: 0,
    text: "",
    final: false,
    timer: null,
    t0: Date.now(),
    body: null,
    caret: null,
    meta: null,
    words: 0,
    done: null,
    source: null,
  };
  const ensure = () => {
    if (w.body) return;
    _stopWorking(bubble);
    bubble.innerHTML = "";
    w.body = document.createElement("span");
    w.body.className = "say";
    w.caret = document.createElement("i");
    w.caret.className = "say-caret";
    w.meta = document.createElement("div");
    w.meta.className = "say-meta";
    bubble.append(w.body, w.caret, w.meta);
  };
  const delayFor = (t) => {
    const backlog = w.tokens.length - w.shown;
    const speed = backlog > 80 ? 0.2 : backlog > 40 ? 0.5 : 1;
    let d = 30;
    if (t.kind === "link") d = 140;
    else if (t.kind === "br") d = 420;
    else if (t.raw) {
      d = 22 + Math.min(t.raw.length, 12) * 4;
      if (/[.!?…]["')\]]*$/.test(t.raw)) d += 300;
      else if (/[,;:—-]$/.test(t.raw)) d += 130;
    }
    return d * speed;
  };
  const sync = () => {
    const cut = w.final
      ? w.text
      : w.text.slice(
          0,
          Math.max(w.text.lastIndexOf(" "), w.text.lastIndexOf("\n")) + 1,
        );
    const toks = chatTokens(cut);
    if (toks.length > w.tokens.length) w.tokens = toks;
  };
  const tick = () => {
    w.timer = null;
    ensure();
    while (w.shown < w.tokens.length && w.tokens[w.shown].kind === "space") {
      w.body.appendChild(document.createTextNode(" "));
      w.shown++;
    }
    if (w.shown < w.tokens.length) {
      const t = w.tokens[w.shown++];
      const span = document.createElement("span");
      span.className = "w";
      span.innerHTML = t.html;
      w.body.appendChild(span);
      if (t.kind === "word" || t.kind === "link") w.words++;
      const sec = Math.round((Date.now() - w.t0) / 1000);
      w.meta.textContent = `writing - ${w.words} words - ${sec}s`;
      scrollEl.scrollTop = scrollEl.scrollHeight;
      w.timer = setTimeout(tick, delayFor(t));
    } else if (w.final) {
      w.caret.remove();
      w.meta.remove();
      const tag = _srcTag(w.source);
      if (tag) bubble.appendChild(tag);
      if (w.done) w.done();
    }
  };
  w.feed = (full) => {
    w.text = full;
    sync();
    if (!w.timer) w.timer = setTimeout(tick, 0);
  };
  w.end = (source) =>
    new Promise((resolve) => {
      w.source = source;
      w.final = true;
      w.done = resolve;
      sync();
      if (!w.timer) w.timer = setTimeout(tick, 0);
    });
  w.stop = () => {
    if (w.timer) clearTimeout(w.timer);
    w.timer = null;
    if (w.caret) w.caret.remove();
    if (w.meta) w.meta.remove();
  };
  bubble._writer = w;
  return w;
}

function doUi(ui) {
  if (!ui) return;
  setTimeout(() => {
    if (ui.screen === "goal") navigate("goal", ui.id);
    else navigate(ui.screen);
  }, 450);
}

async function quickChat(cmd) {
  const chatEl = document.getElementById("chat-container");
  if (!chatEl) return;
  openChatPanel();
  if (chatEl._busy) {
    showToast("I'm still answering the last one", "error");
    return;
  }
  chatEl._busy = true;
  const bubble = addTyping(chatEl, { llm: false });
  try {
    const r = await api("/chat/quick", {
      method: "POST",
      body: JSON.stringify({ command: cmd.toLowerCase() }),
    });
    const wr = makeWriter(bubble, chatEl);
    wr.feed(r.reply);
    await wr.end(r.source);
    if (r.proposal) addProposal(bubble, r.proposal);
  } catch (e) {
    finishTyping(bubble, e.message, null);
    bubble.classList.add("chat-error");
  } finally {
    chatEl._busy = false;
  }
}

function addProposal(bubble, prop) {
  const row = document.createElement("div");
  row.className = "proposal";
  const mk = (label, accept, cls) => {
    const b = document.createElement("button");
    b.className = cls;
    b.textContent = label;
    b.onclick = async () => {
      row.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try {
        const r = await api("/chat/confirm", {
          method: "POST",
          body: JSON.stringify({ proposal_id: prop.id, accept }),
        });
        row.textContent = r.message;
        row.className = "proposal done";
        if (r.applied && r.kind === "create_goal") navigate("goal", r.goal_id);
        else if (r.applied) {
          try {
            navigate(
              currentScreen,
              decodeURIComponent(location.hash.split("/")[1] || ""),
              true,
            );
          } catch (e) {}
        }
      } catch (e) {
        row.textContent = e.message;
        row.className = "proposal done";
      }
    };
    return b;
  };
  row.appendChild(mk("Confirm", true, "btn btn-primary"));
  row.appendChild(mk("Never mind", false, "btn btn-ghost"));
  bubble.appendChild(row);
  if (bubble.parentNode)
    bubble.parentNode.scrollTop = bubble.parentNode.scrollHeight;
}

async function streamChatReply(goalId, message, onDelta, onEvent) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 95000);
  let res;
  try {
    res = await fetch("/api/chat/stream", {
      method: "POST",
      headers,
      body: JSON.stringify({ goal_id: goalId, message }),
      signal: ctrl.signal,
    });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === "AbortError") {
      throw new Error(
        "The model went quiet for 95 seconds — the connection dropped. Try again; if it keeps stalling, open Settings and hit Test connection.",
      );
    }
    throw new Error("Network dropped mid-reply. Send it again.");
  }
  clearTimeout(timer);
  if (res.status === 401) {
    logout();
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const d = (await res.json()).detail;
      if (d)
        detail =
          typeof d === "string"
            ? d
            : d.error ||
              (Array.isArray(d)
                ? d.map((x) => x.msg || JSON.stringify(x)).join("; ")
                : JSON.stringify(d));
    } catch (e) {}
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const idle = setTimeout(() => ctrl.abort(), 95000);
  let buf = "";
  let full = "";
  let source = null;
  let proposal = null;
  let ui = null;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!raw.startsWith("data: ")) continue;
        let ev;
        try {
          ev = JSON.parse(raw.slice(6));
        } catch (e) {
          continue;
        }
        if (ev.error) {
          const msg = ev.detail || "model connection dropped";
          throw new Error(msg);
        }
        if (ev.stage && onEvent) onEvent(ev);
        if (ev.delta) {
          full += ev.delta;
          onDelta(full);
        }
        if (ev.done) {
          source = ev.source || source;
          if (ev.proposal) proposal = ev.proposal;
          if (ev.ui) ui = ev.ui;
        }
      }
    }
  } catch (e) {
    clearTimeout(idle);
    if (e.name === "AbortError") {
      throw new Error(
        "The reply stalled mid-stream. Send it again — if it keeps stalling, check Settings > LLM Provider → Test connection.",
      );
    }
    throw e;
  }
  clearTimeout(idle);
  if (!full) {
    throw new Error(
      "The model returned nothing usable — it may have echoed my prompt back or come out blank. Try rephrasing; if every reply is empty, open Settings and hit Test connection.",
    );
  }
  return { text: full, source: source || "llm", proposal, ui };
}

async function sendGlobalChat(override) {
  const inp = document.getElementById("global-chat-input");
  const chatEl = document.getElementById("chat-container");
  if (!chatEl) return;
  openChatPanel();
  if (chatEl._busy) {
    showToast("I'm still answering the last one", "error");
    return;
  }
  const msg = (
    typeof override === "string" ? override : (inp && inp.value) || ""
  ).trim();
  if (!msg) return;
  if (inp) inp.value = "";
  chatEl._busy = true;
  document.body.classList.add("eloise-busy");
  appendChatMsg(chatEl, "user", msg);
  const bubble = addTyping(chatEl);
  const writer = makeWriter(bubble, chatEl);
  try {
    const r = await streamChatReply(
      null,
      msg,
      (p) => writer.feed(p),
      (ev) => stageEvent(bubble, ev),
    );
    await writer.end(r.source);
    if (r.proposal) addProposal(bubble, r.proposal);
    doUi(r.ui);
    refreshPanelModel();
  } catch (e) {
    finishTyping(bubble, e.message, null);
    bubble.classList.add("chat-error");
  } finally {
    chatEl._busy = false;
    document.body.classList.remove("eloise-busy");
  }
}

async function mountGoalChat(goalId) {
  const cc = document.getElementById("goalchat-container");
  if (!cc) return;
  try {
    const h = await api(`/chat/goals/${goalId}`);
    h.messages.forEach((m) => appendChatMsg(cc, m.role, m.content));
  } catch (e) {}
  const inp = document.getElementById("goalchat-input");
  if (inp)
    inp.onkeypress = (e) => {
      if (e.key === "Enter") sendGoalChat(goalId);
    };
}
async function sendGoalChat(goalId) {
  const inp = document.getElementById("goalchat-input");
  const cc = document.getElementById("goalchat-container");
  if (!inp || !cc) return;
  if (cc._busy) {
    showToast("I'm still answering the last one", "error");
    return;
  }
  const msg = (inp.value || "").trim();
  if (!msg) return;
  inp.value = "";
  cc._busy = true;
  appendChatMsg(cc, "user", msg);
  const bubble = addTyping(cc);
  const writer = makeWriter(bubble, cc);
  try {
    const r = await streamChatReply(
      goalId,
      msg,
      (p) => writer.feed(p),
      (ev) => stageEvent(bubble, ev),
    );
    await writer.end(r.source);
    if (r.proposal) addProposal(bubble, r.proposal);
    doUi(r.ui);
    if (r.source === "completion") setTimeout(() => navigate("goals"), 1000);
  } catch (e) {
    finishTyping(bubble, e.message, null);
    bubble.classList.add("chat-error");
  } finally {
    cc._busy = false;
  }
}

async function renderSettings(el) {
  let html = `<div class="page-header"><div class="kicker">Settings</div><h1>Tuning the rig.</h1><div class="header-rule"></div></div>`;
  try {
    const [llm, status] = await Promise.all([
      api("/settings/llm"),
      api("/status"),
    ]);
    window._lastLlm = llm;
    window._lastStatus = status;
    const presets = (llm && llm.presets) || [];
    const presetOpts = presets
      .map(
        (p) => `<option value="${escHtml(p.id)}">${escHtml(p.label)}</option>`,
      )
      .join("");
    const keyChip = llm.api_key_set
      ? `<span class="chip ${llm.key_source === "env" ? "warn" : "ok"}">${llm.key_source === "env" ? "key from .env" : "key saved"}</span>`
      : '<span class="chip off">no key yet</span>';
    const lastTest = window._llmTest;
    const testChip = lastTest
      ? `<span class="chip ${lastTest.ok ? "ok" : "bad"}">${lastTest.ok ? "last test: replies" : "last test: failed"}</span>`
      : "";
    if (!llm.is_admin) {
      html += `<div class="settings-section"><h3>Model</h3><div class="card">
        <div class="llm-status">${status.cloud_configured ? '<span class="chip hot"><span class="ember-dot"></span> Eloise is live</span>' : '<span class="chip off">No model connected yet</span>'}
        <span class="chip">${escHtml(llm.model || "no model")}</span></div>
        <p class="field-hint">The model and API key are shared by the whole app and managed by the admin (the first account created). Her voice is identical whichever model sits behind it.</p>
      </div></div>`;
    } else
      html += `<div class="settings-section"><h3>LLM Provider</h3>
      <div class="card">
        <div class="llm-status">
          ${status.cloud_configured ? '<span class="chip hot"><span class="ember-dot"></span> Cloud engine armed</span>' : '<span class="chip off">No model talking — paste a key</span>'}
          ${keyChip}
          ${testChip}
          ${status.local_model_available ? '<span class="chip hot">Local engine live</span>' : ""}
          <span class="chip">${escHtml(status.cloud_model || "no model")}</span>
        </div>
        <div class="field"><label>Provider</label>
          <select id="set-provider" onchange="applyProviderPreset()">
            ${presetOpts}
            <option value="">Auto-detect from my key</option>
          </select>
        </div>
        <div class="field"><label>API Key</label>
          <input type="password" id="set-api-key" placeholder="paste it — the provider snaps to match" autocomplete="off">
          <p class="field-hint">Paste any key — OpenAI, Anthropic, Gemini, OpenRouter, Groq, Mistral, DeepSeek, xAI, Together and more. The endpoint and model are worked out and verified for you. Same persona on every provider. Blank keeps the saved key.</p>
        </div>
        <details class="adv"><summary>Advanced: endpoint, model, local engine</summary>
        <div class="field"><label>API Base URL</label><input type="text" id="set-base-url" placeholder="https://openrouter.ai/api/v1"></div>
        <div class="field"><label>Model</label><input type="text" id="set-model" placeholder="e.g. meta-llama/llama-3.1-8b-instruct:free"></div>
        <div class="field"><label><input type="checkbox" id="set-enable-local" style="width:auto;margin-right:8px;">Also use a local Ollama engine (optional)</label></div>
        </details>
        <div class="llm-actions">
          <button class="btn" style="width:auto;" onclick="saveLLMSettings()">Save &amp; Test</button>
          <button class="btn btn-ghost" style="width:auto;" onclick="testLLMSettings()">Test connection</button>
          ${llm.api_key_set && llm.key_source === "settings" ? '<button class="btn btn-ghost" style="width:auto;" onclick="clearLLMKey()">Forget saved key</button>' : ""}
        </div>
        <div id="llm-test-result" class="llm-test"></div>
      </div>
    </div>`;
    let locVal = "";
    try {
      const lc = await api("/settings/location");
      locVal = lc.location || "";
    } catch (e) {}
    let rt = null;
    try {
      rt = await api("/routine");
    } catch (e) {}
    if (rt)
      html += `<div class="settings-section"><h3>Your routine</h3>${routineCardHtml(rt)}</div>`;
    html += `<div class="settings-section"><h3>Location</h3>
      <div class="card">
        <p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">Where you actually live. Chat and schedules only suggest services, shops and apps that exist here — no DoorDash in Lahore.</p>
        <div class="field"><label>Country / city</label>
          <input type="text" id="set-location" value="${escHtml(locVal)}" placeholder="e.g. Pakistan"></div>
        <button class="btn" style="width:auto;" onclick="saveLocation()">Save Location</button>
      </div>
    </div>`;
    try {
      const me = await api("/me");
      const inp = document.getElementById("set-checkin-time");
      if (me.checkin_time && inp) inp.value = me.checkin_time;
    } catch (e) {}
    // Always-busy windows list
    window._busyWindows = [];
    try {
      const bw = await api("/settings/blocked-windows");
      window._busyWindows = bw.blocked_windows || [];
    } catch (e) {}
    // Email channel section + on-demand digest
    let em = null;
    try {
      em = await api("/email/status");
    } catch (e) {}
    const emLive = em && em.configured && em.user_has_email;
    const emCls = emLive ? "ok" : em && em.configured ? "hot" : "off";
    const emLabel = emLive
      ? "Mail channel live"
      : em && em.configured
        ? `Armed, needs your email${em.host ? " via " + escHtml(em.host) : ""}`
        : "SMTP not usable yet";
    const emProblems =
      em && em.problems && em.problems.length
        ? `<ul class="problem-list">${em.problems.map((p) => `<li>${escHtml(p)}</li>`).join("")}</ul>`
        : "";
    html += `<div class="settings-section"><h3>Check-in and email</h3>
      <div class="card">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
          <span class="chip ${emCls}">${emLabel}</span>
          ${em && em.host ? `<span class="chip">${escHtml(em.host)}:${em.port || 587}</span>` : ""}
          ${em && em.from ? `<span class="chip">from ${escHtml(em.from)}</span>` : ""}
        </div>
        ${emProblems}
        ${em && em.hint && !emLive ? `<p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">${escHtml(em.hint)}</p>` : ""}
        <div class="field"><label>Daily check-in and email time</label><input type="time" id="set-checkin-time"></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
          <button class="btn btn-ghost" style="width:auto;" onclick="saveCheckinTime()">Save time</button>
          <button class="btn" style="width:auto;" onclick="sendDigest()">Email me the week</button>
          <span class="chip ${em && em.user_has_email ? "ok" : "off"}">${em && em.user_has_email ? escHtml(userName || "you") : "add an email to get mail"}</span>
        </div>
      </div>
    </div>`;
  } catch (e) {
    html += `<p style="color:#ff8b7a;">Failed to load settings.</p>`;
  }
  html += `<div class="settings-section"><div class="card" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;"><span style="font-size:14px;color:var(--bone-dim);">Signed in as <strong style="color:var(--bone);">${escHtml(userName || "")}</strong></span><button class="btn btn-danger" style="width:auto;color:#fff;" onclick="logout()">Sign out</button></div></div>`;
  el.innerHTML = html;
  // populate LLM provider fields
  const llmState = window._lastLlm;
  if (llmState) {
    const sel = document.getElementById("set-provider");
    const bl = document.getElementById("set-base-url");
    const ml = document.getElementById("set-model");
    const prov = (llmState.provider || "").trim().toLowerCase();
    if (sel) {
      const known = (llmState.presets || []).some((p) => p.id === prov);
      sel.value = known ? prov : "";
    }
    if (bl) bl.value = llmState.base_url || "";
    if (ml) ml.value = llmState.model || "";
    const elc = document.getElementById("set-enable-local");
    if (elc) elc.checked = !!llmState.enable_local;
  }
  const keyInput = document.getElementById("set-api-key");
  if (keyInput) keyInput.addEventListener("input", _detectProviderFromKey);
  renderBusyWindows();
}
function _llmPresets() {
  return (window._lastLlm && window._lastLlm.presets) || [];
}
function _presetById(id) {
  return _llmPresets().find((p) => p.id === id) || null;
}
function _detectProviderFromKey() {
  const key = (
    (document.getElementById("set-api-key") || {}).value || ""
  ).trim();
  const sel = document.getElementById("set-provider");
  if (!key || !sel) return;
  // Longest prefixes first: sk-or- must win over the generic sk- of OpenAI.
  const probes = ["sk-or-", "AIza", "gsk_", "csk-", "sk-"];
  const ids = ["openrouter", "gemini", "groq", "cerebras", "openai"];
  const idx = probes.findIndex((p) => key.startsWith(p));
  if (idx < 0) return;
  const want = ids[idx];
  if (sel.value === want) return;
  if (!_presetById(want)) return;
  sel.value = want;
  applyProviderPreset();
  showToast(
    `Looks like a ${_presetById(want).label} key — preset applied`,
    "success",
  );
}
function applyProviderPreset() {
  const sel = document.getElementById("set-provider");
  const preset = _presetById((sel && sel.value) || "");
  if (!preset) return; // custom endpoint: the operator owns both fields
  const bl = document.getElementById("set-base-url");
  const ml = document.getElementById("set-model");
  // Always overwrite: a provider switch must never inherit the previous
  // provider's endpoint or model.
  if (bl) bl.value = preset.url;
  if (ml) ml.value = preset.model;
}
function _llmFormBody() {
  return {
    provider: (document.getElementById("set-provider") || {}).value || "",
    base_url: (document.getElementById("set-base-url") || {}).value || "",
    model: (document.getElementById("set-model") || {}).value || "",
    api_key: (document.getElementById("set-api-key") || {}).value || "",
    enable_local: !!(document.getElementById("set-enable-local") || {}).checked,
  };
}
function _setTestResult(kind, text) {
  const box = document.getElementById("llm-test-result");
  if (!box) return;
  box.className = "llm-test " + (kind || "");
  box.textContent = text || "";
}
async function saveLLMSettings() {
  try {
    await api("/settings/llm", {
      method: "PUT",
      body: JSON.stringify(_llmFormBody()),
    });
    showToast("Settings saved", "success");
  } catch (e) {
    showToast(e.message, "error");
    return;
  }
  await testLLMSettings();
}
function routineCardHtml(rt) {
  const rows = (rt.rows || [])
    .map(
      (r) => `<div class="rt-row" data-key="${r.key}">
      <span class="rt-name">${escHtml(r.label)}</span>
      <span class="rt-time"><input type="time" value="${r.from}" aria-label="${escHtml(r.label)} start"> to <input type="time" value="${r.to}" aria-label="${escHtml(r.label)} end"></span>
      <span class="rt-src ${r.source === "assumed" ? "guess" : ""}">${r.source === "assumed" ? "my guess" : escHtml(r.source)}</span>
    </div>`,
    )
    .join("");
  const fixed = (rt.fixed || [])
    .map(
      (f) =>
        `<span class="chip off">${escHtml(f.days)} ${f.from}-${f.to}</span>`,
    )
    .join(" ");
  const guess = (rt.assumed || []).length;
  return `<div class="card routine-card" id="routine-card">
    <div class="kicker">${guess ? "Your day, part guessed" : "Your day, as I have it"}</div>
    ${rows}
    ${fixed ? `<div class="rt-fixed">Also blocked: ${fixed}</div>` : ""}
    <div class="rt-actions"><button class="btn" style="width:auto;" onclick="saveRoutine()">Save and replan</button>
    <span class="field-hint">Every goal plans around this one day. Or just tell me in chat.</span></div>
  </div>`;
}

async function saveRoutine() {
  const body = {};
  document.querySelectorAll("#routine-card .rt-row").forEach((row) => {
    const t = row.querySelectorAll("input");
    if (t[0].value && t[1].value)
      body[row.dataset.key] = [t[0].value, t[1].value];
  });
  try {
    await api("/routine", { method: "POST", body: JSON.stringify(body) });
    showToast("Saved. Redrawing every plan around it.");
  } catch (e) {
    showToast(e.message || "Could not save that.");
  }
}

async function testLLMSettings() {
  _setTestResult("pending", "Contacting the provider…");
  try {
    const r = await api("/settings/llm/test", {
      method: "POST",
      body: JSON.stringify(_llmFormBody()),
    });
    window._llmTest = { ok: r.ok, at: Date.now() };
    if (r.ok) {
      _setTestResult(
        "ok",
        `${r.provider} answers — ${r.model} replied in ${r.latency_ms}ms. Eloise is live.`,
      );
      showToast("Provider replies — you are live", "success");
    } else {
      _setTestResult("bad", r.error || "Provider did not reply.");
      showToast("Provider failed — details on the card", "error");
    }
  } catch (e) {
    _setTestResult("bad", e.message);
    showToast("Test failed", "error");
  }
}
async function clearLLMKey() {
  try {
    await api("/settings/llm", {
      method: "PUT",
      body: JSON.stringify({ clear_key: true }),
    });
    showToast("Saved key forgotten — falling back to .env", "success");
    navigate("settings");
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function saveCheckinTime() {
  const t = document.getElementById("set-checkin-time").value;
  if (!t) {
    showToast("Pick a time", "error");
    return;
  }
  try {
    await api("/settings/checkin-time", {
      method: "PUT",
      body: JSON.stringify({ checkin_time: t }),
    });
    showToast("Check-in time saved", "success");
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function savePersona() {
  const p = (document.getElementById("set-persona") || {}).value || "";
  try {
    await api("/settings/persona", {
      method: "PUT",
      body: JSON.stringify({ persona: p }),
    });
    showToast("Persona saved — Eloise obeys it from now on", "success");
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function saveLocation() {
  const v = (
    (document.getElementById("set-location") || {}).value || ""
  ).trim();
  try {
    const r = await api("/settings/location", {
      method: "PUT",
      body: JSON.stringify({ location: v }),
    });
    showToast(
      `Location saved — suggestions now match ${r.location}`,
      "success",
    );
  } catch (e) {
    showToast(e.message, "error");
  }
}
function renderBusyWindows() {
  const wrap = document.getElementById("busy-list");
  if (!wrap) return;
  wrap.innerHTML =
    (window._busyWindows || [])
      .map(
        (w, i) =>
          `<div class="cons-chip"><span>${escHtml(w)}</span><button class="x-btn" onclick="removeBusyWindow(${i})" title="Remove">&times;</button></div>`,
      )
      .join("") ||
    '<p style="font-size:13px;color:var(--bone-dim);margin:0;">None set — Eloise will plan around nothing.</p>';
}
function addBusyWindow() {
  const inp = document.getElementById("busy-new");
  const val = ((inp && inp.value) || "").trim();
  if (!val) {
    showToast("Type a window like gym 5-7pm", "error");
    return;
  }
  window._busyWindows = window._busyWindows || [];
  if (!window._busyWindows.includes(val.toLowerCase()))
    window._busyWindows.push(val.toLowerCase());
  if (inp) inp.value = "";
  renderBusyWindows();
}
function removeBusyWindow(i) {
  (window._busyWindows || []).splice(i, 1);
  renderBusyWindows();
}
async function saveBusyWindows() {
  try {
    const r = await api("/settings/blocked-windows", {
      method: "PUT",
      body: JSON.stringify({ blocked_windows: window._busyWindows || [] }),
    });
    window._busyWindows = r.blocked_windows || [];
    renderBusyWindows();
    showToast("Saved — every schedule now avoids those windows", "success");
  } catch (e) {
    showToast(e.message, "error");
  }
}
async function sendDigest() {
  try {
    const r = await api("/email/digest", { method: "POST" });
    if (r.hint) showToast(r.hint, r.ok ? "success" : "error");
    else showToast(r.reason || "done", r.ok ? "success" : "error");
  } catch (e) {
    showToast(e.message, "error");
  }
}

window.addEventListener("DOMContentLoaded", () => {
  _bindKeys();
  _deskInit();
  if (token) {
    const ru = document.getElementById("orbit-user");
    if (ru) ru.textContent = (userName || "?").slice(0, 1).toUpperCase();
    enterApp();
    // Restore the page from the URL hash (refresh keeps your place).
    if (!routeFromHash()) navigate("today");
  } else {
    document.getElementById("auth-screen").classList.remove("hidden");
    renderAuth();
  }
  setInterval(() => {
    if (token) pollCheckIn();
  }, 120000);
});
