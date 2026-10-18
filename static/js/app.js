/* ============================================================
   ELOISE — control desk
   ============================================================ */

let token = localStorage.getItem('eloise_token') || '';
let userName = localStorage.getItem('eloise_name') || '';
let currentScreen = 'today';

const api = async (path, opts = {}) => {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch('/api' + path, { ...opts, headers });
  if (res.status === 401) {
    logout();
    throw new Error('unauthorized');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  if (res.status === 204) return {};
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : {};
};

/* ---------------- helpers ---------------- */
function linkify(text) {
  if (typeof text !== 'string') return text;
  return text.replace(/https?:\/\/[^\s<>()]+(?:[^\s<().,!?;:'"\]])?/gi, (m) => {
    try {
      const url = new URL(m);
      let label = 'link';
      const host = url.hostname.toLowerCase();
      if (host.includes('youtube') || host.includes('youtu.be') || host.includes('vimeo')) label = 'video';
      else if (/\.(mp4|webm|mov|avi)(\?|#|$)/i.test(url.pathname)) label = 'video';
      else if (host.includes('drive.google') || host.includes('docs.google') || host.includes('notion')) label = 'doc';
      else if (/\.(pdf)(\?|#|$)/i.test(url.pathname)) label = 'pdf';
      else if (/\.(jpg|jpeg|png|webp|gif|svg)(\?|#|$)/i.test(url.pathname)) label = 'image';
      return `<a class="chat-link" href="${url.href}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    } catch (e) {
      return `<a class="chat-link" href="${m}" target="_blank" rel="noopener noreferrer">link</a>`;
    }
  });
}
function fmtTime(t) {
  if (!t) return '';
  const [h, m] = t.split(':');
  let hh = parseInt(h, 10) % 24;
  const am = hh < 12;
  hh = hh % 12; if (hh === 0) hh = 12;
  return `${hh}:${m} ${am ? 'AM' : 'PM'}`;
}
function fmtHours(h) {
  return `${h}h`;
}
function showToast(msg, type) {
  const wrap = document.getElementById('toasts') || document.body;
  const t = document.createElement('div');
  t.className = 'toast' + (type === 'error' ? ' error' : type === 'success' ? ' success' : '');
  t.textContent = msg;
  wrap.appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

function safeLink(url) {
  const s = String(url || '').trim().toLowerCase();
  if (/^(javascript:|data:)/i.test(url || '')) return '#';
  return s;
}

/* ============================================================
   Modals
   ============================================================ */
function openModal(title, bodyHtml, actionsHtml) {
  closeModal();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'app-modal';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-eyebrow">ELOISE</div>
      <h3>${escHtml(title)}</h3>
      <p class="body">${bodyHtml || ''}</p>
      <div class="actions">${actionsHtml || ''}</div>
    </div>`;
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
  document.body.appendChild(overlay);
}
function escHtml(s) {
  const div = document.createElement('div');
  div.textContent = (s == null ? '' : String(s));
  return div.innerHTML;
}
function confirmModal(title, body, confirmLabel, danger) {
  return new Promise((resolve) => {
    window._resolveModal = resolve;
    openModal(title, escHtml(body), `
      <button class="btn btn-ghost" onclick="closeModal();window._resolveModal&&(window._resolveModal(false),window._resolveModal=null)">Cancel</button>
      <button class="btn ${danger ? 'btn-danger' : ''}" ${danger ? 'style="color:#fff;"' : ''} onclick="closeModal();window._resolveModal&&(window._resolveModal(true),window._resolveModal=null)">${escHtml(confirmLabel)}</button>
    `);
  });
}
function askModal(title, placeholder) {
  return new Promise((resolve) => {
    window._resolveModal = resolve;
    openModal(title, `<div class="field"><input type="text" id="ask-modal-input" placeholder="${escHtml(placeholder)}"></div>`, `
      <button class="btn btn-ghost" onclick="closeModal();window._resolveModal&&(window._resolveModal(false),window._resolveModal=null)">Cancel</button>
      <button class="btn" onclick="window._resolveModal&&(closeModal(),window._resolveModal(document.getElementById('ask-modal-input').value.trim()),window._resolveModal=null)">Send</button>
    `);
    requestAnimationFrame(() => {
      const i = document.getElementById('ask-modal-input');
      if (i) { i.focus(); i.onkeypress = (e) => { if (e.key === 'Enter') { const v = i.value.trim(); closeModal(); window._resolveModal && (window._resolveModal(v), window._resolveModal = null); } }; }
    });
  });
}

/* ============================================================
   24h check-in dialog
   ============================================================ */
let _checkinShown = false;
async function pollCheckIn() {
  if (!token || _checkinShown) return;
  try {
    const chk = await api('/checkin');
    if (chk.due) { _checkinShown = true; showCheckInDialog(); }
  } catch (e) {}
}
async function showCheckInDialog() {
  let promptMsg = "It's been a day. Did you actually do what was on the board?";
  try {
    const p = await api('/checkin/prompt', { method: 'POST' });
    if (p && p.message) promptMsg = p.message;
  } catch (e) {}
  openModal('Check-in', escHtml(promptMsg), `
    <button class="btn btn-ghost" onclick="respondCheckIn(false)">No — didn't do it</button>
    <button class="btn" onclick="respondCheckIn(true)">Yes — done</button>
  `);
}
async function respondCheckIn(done) {
  closeModal();
  _checkinShown = false;
  try {
    const res = await api('/checkin/respond', { method: 'POST', body: JSON.stringify({ done }) });
    if (res && res.message) {
      showToast(res.message, done ? 'success' : 'error');
      if (!done) setTimeout(() => navigate(currentScreen), 1500);
    }
  } catch (e) {}
}

/* ============================================================
   Auth
   ============================================================ */
function renderAuth() {
  const form = document.getElementById('auth-form');
  form.innerHTML = `
    <div id="auth-signin">
      <h2>Claim a seat</h2>
      <p class="sub">Eloise keeps the ledger. You keep time.</p>
      <div class="field"><label>Name</label><input type="text" id="auth-name" placeholder="Your name"></div>
      <div class="field"><label>Email</label><input type="email" id="auth-email" placeholder="You@place.com"></div>
      <div class="field"><label>Password</label><input type="password" id="auth-pass" placeholder="At least 6 characters"></div>
      <button class="btn" onclick="doSignup()">Sign in</button>
      <div class="rule"><span>or</span></div>
      <button class="btn btn-ghost" onclick="showGuest()">Guest pass — no email</button>
    </div>
    <div id="auth-guest" class="hidden">
      <h2>Guest pass</h2>
      <p class="sub">Walk in. No password, no verification.</p>
      <div class="field"><label>Just a name</label><input type="text" id="guest-name" placeholder="What do we call you?"></div>
      <div class="field"><label>Email (needed to get schedules emailed)</label><input type="email" id="guest-email" placeholder="You@place.com"></div>
      <button class="btn" onclick="doGuest()">Take a seat</button>
      <div class="rule"><span>or</span></div>
      <button class="btn btn-ghost" onclick="showSignin()">&#8617; Sign in instead</button>
    </div>
  `;
}
function showGuest() {
  document.getElementById('auth-signin').classList.add('hidden');
  document.getElementById('auth-guest').classList.remove('hidden');
  const n = document.getElementById('guest-name');
  if (n) {
    n.value = document.getElementById('auth-name').value || '';
    n.focus();
  }
}
function showSignin() {
  document.getElementById('auth-guest').classList.add('hidden');
  document.getElementById('auth-signin').classList.remove('hidden');
}
async function doSignup() {
  const body = {
    name: document.getElementById('auth-name').value,
    email: document.getElementById('auth-email').value,
    password: document.getElementById('auth-pass').value,
  };
  try {
    const r = await api('/signup', { method: 'POST', body: JSON.stringify(body) });
    token = r.token; userName = r.name;
    localStorage.setItem('eloise_token', token);
    localStorage.setItem('eloise_name', userName);
    enterApp();
  } catch (e) { showToast(e.message, 'error'); }
}
async function doGuest() {
  const name = document.getElementById('guest-name')?.value?.trim() || 'Guest';
  const email = document.getElementById('guest-email')?.value?.trim() || undefined;
  try {
    const r = await api('/guest', { method: 'POST', body: JSON.stringify({ name, email }) });
    token = r.token; userName = r.name;
    localStorage.setItem('eloise_token', token);
    localStorage.setItem('eloise_name', userName);
    if (email) showToast("Seat's taken. You'll get the list emailed.", 'success');
    enterApp();
  } catch (e) { showToast(e.message, 'error'); }
}
function logout() {
  token = ''; userName = '';
  localStorage.removeItem('eloise_token');
  localStorage.removeItem('eloise_name');
  document.getElementById('app-screen').classList.add('hidden');
  document.getElementById('auth-screen').classList.remove('hidden');
  renderAuth();
}
function enterApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  const ru = document.getElementById('rail-user');
  if (ru) ru.textContent = (userName || '?').slice(0, 1).toUpperCase();
  navigate('today');
  mountChatPanel();
  refreshFootMomentum();
  setTimeout(maybeOnboarding, 350);
}

async function refreshFootMomentum() {
  const el = document.getElementById('rail-user');
  if (!el) return;
  try {
    const m = await api('/momentum');
    el.title = `${userName || 'you'} — momentum ${m.momentum}`;
    el.dataset.momentum = m.momentum;
  } catch (e) { /* silent */ }
}

/* ============================================================
   Cinematic onboarding — first-run seat & demo files
   ============================================================ */
async function maybeOnboarding() {
  if (!token) return;
  try {
    const goals = await api('/goals');
    if (goals.length > 0) return;  // already has real files
    showOnboarding(onboardSeed);
  } catch (e) { /* board may be loading; skip */ }
}
function showOnboarding(onDone) {
  closeModal();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay onboarding-overlay';
  overlay.id = 'app-modal';
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
    { kicker: 'STEP 1 — THE RIG', title: 'You just walked into the control room.',
      body: 'Eloise runs your schedule like a mom who\'s had too much coffee: the plan is drawn out, the pressure is scored 1-to-10, and if a deadline slips, whatever you staked comes due. No games.' },
    { kicker: 'STEP 2 — REAL FILES', title: 'I\'m seeding two demo files.',
      body: 'A launch build and a report you\'re dead on the line for. Schedules draw themselves around what\'s actually blocked. Tap around — it all moves.', button: 'Seed the demos',
      fn: () => { if (onDone) onDone(); } },
    { kicker: 'STEP 3 — GO', title: 'Seat\'s yours.',
      body: 'File real goals, pin stakes, and let the momentum meter decide if you\'re on fire or stalling. Don\'t embarrass the ledger.', button: 'Open the board',
      fn: () => { closeModal(); navigate('board'); } },
  ];
  let i = 0;
  function render() {
    const s = intro[i];
    document.getElementById('onb-kicker').textContent = s.kicker;
    document.getElementById('onb-title').textContent = s.title;
    document.getElementById('onb-body').textContent = s.body;
    const acts = document.getElementById('onb-actions');
    window.onbGo = s.fn || window.nextOnb;
    acts.innerHTML = `<button class="btn" onclick="window.onbGo()">${s.button || 'Continue'}</button>
      <button class="btn btn-ghost" onclick="closeModal()">Skip</button>`;
  }
  window.nextOnb = () => { i = Math.min(i + 1, intro.length - 1); render(); };
  render();
}
async function onboardSeedGo() {
  try {
    await api('/onboarding/seed', { method: 'POST' });
  } catch (e) { showToast(e.message, 'error'); }
  if (window.nextOnb) window.onbGo = window.nextOnb;
  window.nextOnb();
}
const onboardSeed = onboardSeedGo;

/* ============================================================
   Navigation & router — hash-backed so refresh keeps the page and
   the browser back button works.
   ============================================================ */
const KNOWN_SCREENS = ['today', 'board', 'goals', 'new-goal', 'goal', 'schedule',
                       'global-chat', 'settings'];
let _hashLock = false;

function navigate(screen, param, fromHash) {
  // The chat is a persistent side panel now, not a screen. Legacy #global-chat
  // links and the rail button open it over whatever page you're on.
  if (screen === 'global-chat') {
    openChatPanel();
    screen = 'today';
  }
  if (!fromHash && KNOWN_SCREENS.includes(screen)) {
    const h = '#' + screen + (param !== undefined && param !== null && param !== ''
      ? '/' + encodeURIComponent(param) : '');
    if (location.hash !== h) {
      _hashLock = true;
      location.hash = h;
      setTimeout(() => { _hashLock = false; }, 0);
    }
  }
  currentScreen = screen;
  document.querySelectorAll('.rail-item').forEach(n =>
    n.classList.toggle('active', n.dataset.screen === screen)
  );
  const main = document.getElementById('main-content');
  main.classList.remove('scene-in');
  void main.offsetWidth; // restart the entrance animation
  main.classList.add('scene-in');
  switch (screen) {
    case 'today': renderToday(main); break;
    case 'board': renderBoard(main); break;
    case 'goals': renderGoals(main); break;
    case 'new-goal': renderNewGoal(main); break;
    case 'goal': renderGoalDetail(main, param); break;
    case 'schedule': renderSchedule(main); break;
    case 'settings': renderSettings(main); break;
    default: renderToday(main);
  }
  if (token) setTimeout(pollCheckIn, 600);
}

function routeFromHash() {
  const raw = (location.hash || '').replace(/^#/, '');
  if (!raw) return false;
  const [screen, param] = raw.split('/');
  const s = screen || 'today';
  if (!KNOWN_SCREENS.includes(s)) return false;
  navigate(s, param ? decodeURIComponent(param) : undefined, true);
  return true;
}
window.addEventListener('hashchange', () => {
  if (_hashLock) return;
  routeFromHash();
});

/* ============================================================
   Today
   ============================================================ */
async function renderToday(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Today —</div><h1>…</h1></div>`;
  try {
    const today = await api('/today');
    const status = await api('/status');

    let html = `<div class="screen-fill">
    <div class="page-header">
      <div class="kicker">Today — ${escHtml(today.date)}</div>
      <h1>The run of show.</h1>
      <p class="sub">${escHtml(today.items.length)} tasks on the board${today.items.length ? ' · none of them moved themselves' : '.'}</p>
      <div class="header-rule"></div>
    </div>`;

    if (today.interventions && today.interventions.length) {
      html += `<div style="margin-bottom:22px;display:flex;flex-direction:column;gap:10px;">`;
      today.interventions.forEach(iv => {
        html += `<div class="card" style="border-left:3px solid var(--ember);">
          <p style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;">
            <span style="flex:1;">${escHtml(iv.message)}</span>
            <button class="btn btn-sm btn-ghost" onclick="ackIntervention(${escHtml(iv.id)}, ${escHtml(iv.goal_id)})">Ack</button>
          </p>
        </div>`;
      });
      html += `</div>`;
    }

    html += `<div class="schedule-grid">`;
    if (today.items.length === 0) {
      html += `<div class="empty-state compose"><div class="big">&#x2737;</div><h3>Nothing booked today.</h3><p>File a goal and Eloise draws the lines.</p></div>`;
    } else {
      today.items.forEach(a => {
        const timeStr = a.start_time && a.end_time ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}` : '';
        const cls = a.status === 'done' ? 'done' : a.status === 'missed' ? 'missed' : '';
        html += `<div class="schedule-day"><div class="schedule-day-head"><span>${escHtml(a.goal_title)}</span><span class="chip ${a.status==='done'?'ok':a.status==='missed'?'bad':'hot'}">${escHtml(a.status)}</span></div>
          <div class="schedule-row ${cls}">
            <span class="time">${timeStr || '—'}</span>
            <span class="title">${escHtml(a.title)}</span>
            <span class="task-name">${escHtml(a.status)}</span>
          </div>
        </div>`;
      });
    }
    html += `</div>`;

    // Risk board
    try {
      const sb = await api('/scoreboard');
      if (sb.goals && sb.goals.length) {
        html += `<div class="page-header" style="margin-top:44px;"><div class="kicker">Risk Board</div><h1>Who's sliding.</h1><div class="header-rule"></div></div>`;
        sb.goals.forEach(g => {
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
      ${status.cloud_configured
        ? `<span class="chip hot">${escHtml(status.provider || 'cloud')} armed — ${escHtml(status.cloud_model || '')}</span>`
        : '<span class="chip off">&#x25CB; No API key — add one in Settings</span>'}
      <span>${status.cloud_error && !status.cloud_configured ? escHtml(status.cloud_error) : ''}</span>
      ${status.local_model_available ? '<span class="chip hot">local live</span>' : ''}
    </div>
    </div>`;  // close .screen-fill

    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Today</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}

async function ackIntervention(interventionId, goalId) {
  try {
    await api(`/interventions/${interventionId}/acknowledge`, { method: 'POST' });
    showToast('Acknowledged', 'success');
  } catch (e) { showToast('Could not acknowledge', 'error'); }
  navigate('today');
}

/* ============================================================
   Board — momentum, pressure, stakes (the "how'd they do that" view)
   ============================================================ */
async function renderBoard(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Board</div><h1>…</h1></div>`;
  try {
    const [mom, goals] = await Promise.all([api('/momentum'), api('/goals')]);
    let html = `<div class="screen-fill"><div class="page-header"><div class="kicker">Control board</div>
      <h1>The meter's open.</h1>
      <p class="sub">Momentum, pressure, and the bets on the line — all live, all straight.</p>
      <div class="header-rule"></div>
    </div>`;

    // momentum dial
    const m = mom;
    const momCls = m.momentum >= 70 ? 'ok' : m.momentum >= 40 ? 'hot' : 'bad';
    html += `<div class="card momentum-card">
      <div style="display:flex;gap:28px;align-items:center;flex-wrap:wrap;">
        <div class="dial" style="--p:${m.momentum}">
          <div class="dial-in"><span class="dial-num">${m.momentum}</span><span class="dial-cap">/100</span></div>
        </div>
        <div style="flex:1;min-width:220px;">
          <div class="kicker">Momentum</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;margin:10px 0;">
            <span class="chip ${momCls}">${m.momentum >= 70 ? 'on fire' : m.momentum >= 40 ? 'ticking' : 'stalled'}</span>
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
    const withP = goals.filter(g => g.pressure).sort((a, b) => b.pressure.score - a.pressure.score);
    if (withP.length === 0) {
      html += `<div class="empty-state"><div class="big">&#x2737;</div><h3>No pressure yet.</h3><p>File a goal and it starts registering.</p></div>`;
    } else {
      html += `<div class="board-rows">`;
      withP.forEach(g => {
        const pc = g.pressure.score;
        const cls = pc >= 8 ? 'bad' : pc >= 6 ? 'hot' : pc >= 4 ? 'warn' : 'ok';
        const barCls = pc >= 8 ? 'bad' : pc >= 6 ? 'hot' : pc >= 4 ? 'warn' : 'ok';
        html += `<div class="goal-card pressure-row" onclick="navigate('goal', ${g.id})">
          <div style="flex:1;">
            <div class="title">${escHtml(g.display_title)}</div>
            <div class="meta">
              <span class="chip off">${g.days_left}d left</span>
              ${g.pressure.remaining_hours ? `<span class="chip off">${escHtml(g.pressure.remaining_hours)}h open</span>` : ''}
              <span class="chip ${cls}">${pc}/10 — ${escHtml(g.pressure.label)}</span>
            </div>
          </div>
          <div class="pressure-meter"><span class="seg ${pc>=8?'on':''}"></span><span class="seg ${pc>=7?'on':''}"></span><span class="seg ${pc>=5?'on':''}"></span><span class="seg ${pc>=3?'on':''}"></span><span class="seg ${pc>=1?'on':''}"></span><span class="seg-label">${pc}</span></div>
        </div>`;
      });
      html += `</div>`;
    }

    // — stakes removed per user request — //
    /* if (m.stakes && m.stakes.length > 0) {
      html += `<div class="page-header" style="margin-top:36px;"><div class="kicker">On the line</div>
        <h1>Debt.</h1><div class="header-rule"></div></div>`;
      m.stakes.forEach(s => {
        html += `<div class="card stake-card">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
            <div style="flex:1;"><span class="chip bad">STAKED</span> <strong style="color:var(--bone);">${escHtml(s.punishment)}</strong>
              <span class="sub" style="margin:6px 0 0;display:block;color:var(--bone-dim);">if <strong style="color:var(--bone);">${escHtml(s.display_title)}</strong> slips, this comes due.</span></div>
            <button class="btn btn-sm btn-danger" style="color:#fff;" onclick="callInDebt(${escHtml(s.goal_id)})">Call it in</button>
          </div>
        </div>`;
      });
    } */

    html += `</div>`;  // close .screen-fill
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Board</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}
async function callInDebt(goalId) {
  try {
    const r = await api(`/goals/${goalId}/stake/enforce`, { method: 'POST' });
    if (r.roast) showToast(r.roast, 'error');
    navigate('board');
  } catch (e) { showToast(e.message, 'error'); }
}

/* ============================================================
   Goals
   ============================================================ */
async function renderGoals(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Goals</div><h1>…</h1></div>`;
  try {
    const goals = await api('/goals');
    let html = `<div class="screen-fill"><div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-end;">
      <div><div class="kicker">Archive</div><h1>Open files.</h1><p class="sub">${goals.length} total</p></div>
      <button class="btn" style="width:auto;" onclick="navigate('new-goal')">+ File</button>
    </div>`;
    if (goals.length === 0) {
      html += `<div class="empty-state compose"><div class="big">&#x2737;</div><h3>Nothing on file.</h3><p>Give Eloise something real to chase.</p><p style="margin-top:14px;"><button class="btn" style="width:auto;" onclick="navigate('new-goal')">File a goal</button></p></div>`;
    } else {
      html += `<div class="goal-list">`;
      goals.forEach(g => {
        const riskCls = g.days_left < 0 ? 'overdue' : g.days_left <= 2 ? 'at_risk' : '';
        const statusText = g.status === 'succeeded' ? 'DONE' : g.status === 'failed' ? 'FAILED' : g.days_left < 0 ? 'OVERDUE' : `${g.days_left}d`;
        const tagCls = g.status === 'succeeded' ? 'ok' : g.days_left < 0 ? 'bad' : g.days_left <= 2 ? 'hot' : '';
        html += `<div class="goal-card ${riskCls}" onclick="navigate('goal', ${g.id})">
          <div class="title">${escHtml(g.display_title)}</div>
          <div class="meta">
            <span class="chip ${tagCls}">${escHtml(statusText)}</span>
            <span>Due ${escHtml(g.deadline)}</span>
            <span class="chip off">${escHtml(g.plan_status)}</span>
            ${g.open_intervention_type ? `<span class="chip bad">flag: ${escHtml(g.open_intervention_type)}</span>` : ''}
          </div>
        </div>`;
      });
      html += `</div>`;
    }
    html += `</div>`;  // close .screen-fill
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Goals</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}

/* ============================================================
   Goal detail
   ============================================================ */
async function renderGoalDetail(el, id) {
  el.innerHTML = `<div class="page-header"><div class="kicker">On file</div><h1>…</h1></div>`;
  try {
    const g = await api(`/goals/${id}`);
    let html = `<div class="page-header">
      <div class="kicker">On file · #${escHtml(g.id)}</div>
      <h1>${escHtml(g.display_title)}</h1>
      <p class="sub">Due ${escHtml(g.deadline)} · ${g.days_left >= 0 ? escHtml(g.days_left) + ' days left' : 'OVERDUE'} · ${escHtml(g.plan_status)}</p>
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
      const pcl = pc >= 8 ? 'bad' : pc >= 6 ? 'hot' : pc >= 4 ? 'warn' : 'ok';
      html += `<div class="card pressure-row" style="margin-bottom:24px;">
        <div style="flex:1;">
          <div class="kicker">Pressure</div>
          <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
            <div style="font-size:34px;font-weight:800;color:${pc>=8?'#ff8b7a':pc>=6?'var(--ember)':'var(--copper)'};">${pc}<span style="font-size:15px;color:var(--bone-dim);">/10</span></div>
            <div class="pressure-meter"><span class="seg ${pc>=8?'on':''}"></span><span class="seg ${pc>=7?'on':''}"></span><span class="seg ${pc>=5?'on':''}"></span><span class="seg ${pc>=3?'on':''}"></span><span class="seg ${pc>=1?'on':''}"></span></div>
            <span class="chip ${pcl}">${escHtml(g.pressure.label)}</span>
            <span class="chip off">${g.pressure.remaining_hours}h open</span>
          </div>
        </div>
      </div>`;
    }

    // tabs
    html += `<div class="tabs"><span class="tab active" onclick="switchTab('schedule','${g.id}')">Schedule</span><span class="tab" onclick="switchTab('progress','${g.id}')">Progress</span><span class="tab" onclick="switchTab('constraints','${g.id}')">Constraints</span></div>`;

    // schedule
    html += `<div id="detail-schedule"><div class="schedule-grid">`;
    const byDate = {};
    (g.plan || []).forEach(a => { if (a.date) (byDate[a.date] = byDate[a.date] || []).push(a); });
    const dates = Object.keys(byDate).sort();
    if (dates.length === 0) {
      if (String(g.plan_status).startsWith('blocked') && g.plan_summary) {
        html += `<div class="empty-state"><h3>No free time to schedule</h3><p style="max-width:420px;font-size:14px;line-height:1.7;color:var(--bone-dim);">${escHtml(g.plan_summary)}</p><p style="font-size:13px;color:var(--copper);">Eloise plans every goal around the same windows — relax one in Settings &rarr; Always-Busy Windows, then Redraw schedule.</p></div>`;
      } else {
        html += `<div class="empty-state"><h3>No schedule drawn yet</h3></div>`;
      }
    } else {
      dates.forEach(d => {
        const isPast = d < new Date().toISOString().slice(0, 10);
        html += `<div class="schedule-day"><div class="schedule-day-head"><span>${escHtml(d)}</span><span class="chip hot">${byDate[d].length}</span></div>`;
        byDate[d].forEach(a => {
          const timeStr = a.start_time && a.end_time ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}` : '';
          const done = a.status === 'done';
          const missed = a.status === 'missed' && !done;
          const cls = done ? 'done' : missed ? 'missed' : '';
          html += `<div class="schedule-row ${cls}">
            <span class="time">${timeStr || '—'}</span>
            <span class="title">${escHtml(a.title)}${missed ? '<span class="chip warn row-flag">FAILED</span>' : ''}</span>
            <span class="task-name"><button class="btn btn-sm ${done ? 'btn-ghost' : missed ? 'btn-warn' : ''}" onclick="toggleTask(${a.id}, this)">${done ? 'REOPEN' : 'DONE'}</button></span>
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
        <div class="stat-tile"><div class="k">Status</div><div class="v" style="color:${p.status==='safe'?'var(--ok)':p.status==='overdue'?'#ff8b7a':'var(--copper)'};">${escHtml(String(p.status).toUpperCase())}</div></div>
      </div>
    </div>`;

    // constraints (hidden)
    html += `<div id="detail-constraints" style="display:none;">`;
    const cons = g.constraints || [];
    if (cons.length === 0) {
      html += `<div class="empty-state"><h3>No constraints</h3><p>Tell Eloise what's off-limits.</p></div>`;
    } else {
      cons.forEach((c, ci) => html += `<div class="note-card"><span class="who">blocked</span><div style="flex:1">${escHtml(c)}</div><button class="btn btn-sm btn-ghost" onclick="removeGoalConstraint(${g.id}, ${ci})">&times;</button></div>`);
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
    mountGoalChat(g.id);
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">On file</div><h1>Missing file.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}
function switchTab(name, id) {
  document.querySelectorAll('#main-content .tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('detail-schedule').style.display = name === 'schedule' ? '' : 'none';
  document.getElementById('detail-progress').style.display = name === 'progress' ? '' : 'none';
  document.getElementById('detail-constraints').style.display = name === 'constraints' ? '' : 'none';
}
async function addGoalConstraint(id) {
  const txt = document.getElementById('new-cons-input').value;
  if (!txt) return;
  try { await api(`/goals/${id}/constraints`, { method: 'POST', body: JSON.stringify({ text: txt }) }); } catch (e) {}
  navigate('goal', id);
}
async function removeGoalConstraint(id, idx) {
  try { await api(`/goals/${id}/constraints`, { method: 'POST', body: JSON.stringify({ removeIndex: idx }) }); } catch (e) {}
  navigate('goal', id);
}
async function completeGoal(id) {
  try { await api(`/goals/${id}/complete`, { method: 'POST', body: JSON.stringify({ claimed_success: true }) }); showToast('Closed out', 'success'); } catch (e) {}
  navigate('goals');
}
async function completeGoalFlow(id) {
  const reason = await askModal('Mark done?', 'Confirm this goal is actually complete. No lies — the next check-in will find out.');
  if (reason === null) return;  // user cancelled
  try {
    await api(`/goals/${id}/complete`, { method: 'POST', body: JSON.stringify({ claimed_success: true, reason: reason || 'User confirmed completion' }) });
    showToast('Closed out. If you\'re lying, the next check-in will find out.', 'success');
    navigate('goals');
  } catch (e) { showToast(e.message, 'error'); }
}
async function cancelGoalFlow(id) {
  const reason = await askModal('Cancel or delay?', 'Give a real reason — exam cancelled, project delayed, scope changed. Not "I don\'t feel like it".');
  if (reason === null || !reason) return;
  try {
    const r = await api(`/goals/${id}/complete`, { method: 'POST', body: JSON.stringify({ claimed_success: false, reason }) });
    showToast(r.roast || 'File closed.', 'success');
    navigate('goals');
  } catch (e) { showToast(e.message, 'error'); }
}
async function pinStake(id) {
  const punishment = await askModal('Pin a stake', 'What happens if this slips? e.g. \u00A350 to a charity I hate');
  if (!punishment) return;
  try {
    await api(`/goals/${id}/stake`, { method: 'POST', body: JSON.stringify({ goal_id: id, punishment }) });
    showToast('Staked. It\u2019s on the line now.', 'success');
    navigate('goal', id);
  } catch (e) { showToast(e.message, 'error'); }
}
async function refreshGoal(id) {
  const main = document.getElementById('main-content');
  if (main && currentScreen === 'goal') await renderGoalDetail(main, id);
}

async function regenPlan(id) {
  const main = document.getElementById('main-content');
  if (!main) return;
  const note = document.createElement('div');
  note.className = 'plan-generating';
  note.id = 'plan-generating-note';
  note.innerHTML = `<span class="typing-bars"><span class="bar"></span><span class="bar"></span><span class="bar"></span><span class="bar"></span></span>
    <div><strong>Redraw kicked off.</strong><br>
    <span style="font-size:13px;color:var(--bone-dim);">The brain is redrawing in the background. Your current schedule stays until the new one lands.</span></div>`;
  main.prepend(note);
  note.scrollIntoView({ behavior: 'smooth', block: 'center' });
  let posted = false;
  try {
    await api(`/goals/${id}/plan`, { method: 'POST' });
    posted = true;
  } catch (e) { showToast(e.message, 'error'); }
  // Keep showing the current schedule while the model works. When the redraw
  // settles, render the result: the NEW schedule on success, or the OLD one if
  // the model couldn't be reached (the server keeps it and queues a retry).
  let settled = null;
  if (posted) settled = await waitPlanSettles(id);
  const nx = document.getElementById('plan-generating-note');
  if (nx) nx.remove();
  if (posted && settled && String(settled.plan_status).startsWith('blocked')) {
    showToast('No free time to schedule \u2014 adjust your Always-Busy Windows once and redraw: ' + (settled.plan_summary || ''), 'error');
  } else if (posted && settled && String(settled.plan_summary).startsWith('kept previous')) {
    showToast('Couldn\u2019t reach the model \u2014 kept your current schedule. Retrying in a minute.', 'error');
  } else if (posted && settled) {
    showToast('New schedule drawn' + (settled.plan_summary ? ' \u2014 ' + settled.plan_summary : '') + '.', 'success');
  } else if (posted) {
    showToast('Still drawing in the background \u2014 it will appear here shortly.', 'info');
  }
  try {
    if (currentScreen === 'goal') await renderGoalDetail(main, id);
  } catch (e) {}
}

async function waitPlanSettles(id, attempts = 36) {
  // Poll the goal until the background draw is no longer 'generating'
  // (it flips back to 'active' on success OR failure — old plan kept).
  for (let i = 0; i < attempts; i++) {
    try {
      const g = await api(`/goals/${id}`);
      if (g.plan_status !== 'generating') return g;
    } catch (e) {}
    await new Promise(r => setTimeout(r, 2500));
  }
  return null;
}
async function deleteGoal(id) {
  const ok = await confirmModal('Burn the file?', 'This deletes the goal and every schedule tied to it. Eloise will not miss it.', 'Burn it', true);
  if (!ok) return;
  try {
    const r = await api(`/goals/${id}/delete`, { method: 'POST', body: JSON.stringify({ reason: 'User deleted' }) });
    if (r.roast) showToast(r.roast, 'error');
    navigate('goals');
  } catch (e) {}
}

/* ============================================================
   New goal
   ============================================================ */
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
  const inp = document.getElementById('new-constraint');
  const val = (inp.value || '').trim();
  if (!val) return;
  window._constraints.push(val);
  inp.value = '';
  renderConstraintChips();
}
function renderConstraintChips() {
  const wrap = document.getElementById('constraints-list');
  if (!wrap) return;
  wrap.innerHTML = (window._constraints || []).map((c, i) =>
    `<span class="chip hot" style="margin:3px 4px 3px 0;cursor:pointer;" onclick="removeConstraint(${i})">${escHtml(c)} &times;</span>`
  ).join('');
}
function removeConstraint(i) {
  window._constraints.splice(i, 1);
  renderConstraintChips();
}
async function createGoal() {
  const title = document.getElementById('new-goal-title').value;
  const deadline = document.getElementById('new-goal-deadline').value;
  const reminder = document.getElementById('new-goal-reminder').value;
  if (!title || !deadline) { showToast('Name and deadline needed', 'error'); return; }
  try {
    const r = await api('/goals', { method: 'POST', body: JSON.stringify({ title, deadline, reminder_time: reminder, constraints: window._constraints || [] }) });
    // The MODEL writes the clarifying questions for this specific goal (not a
    // hardcoded category set). If the model is unreachable, skip straight to
    // the board — creation never blocks on it.
    try {
      const c = await api(`/goals/${r.id}/clarify`, { method: 'POST' });
      if (c.questions && c.questions.length > 0) {
        showQuestionnaire(r.id, title, c.questions);
        return;
      }
    } catch (qe) { /* model down: file without questions */ }
    showToast('Filed. Eloise draws the lines.', 'success');
    navigate('goals');
  } catch (e) { showToast(e.message, 'error'); }
}

function showQuestionnaire(goalId, goalTitle, questions) {
  const main = document.getElementById('main-content');
  let html = `<div class="page-header"><div class="kicker">Filing: ${escHtml(goalTitle)}</div>
    <h1>Answer these.</h1><p class="sub">Eloise needs specifics to build a real schedule. Not vibes.</p>
    <div class="header-rule"></div></div>`;
  html += `<form id="questionnaire-form" onsubmit="submitQuestionnaire(event, ${goalId})">`;
  questions.forEach((q, i) => {
    html += `<div class="card" style="margin-bottom:16px;">
      <label style="display:block;font-weight:700;color:var(--bone);margin-bottom:6px;">${escHtml(q.q)}</label>
      <p style="font-size:12px;color:var(--bone-dim);margin:0 0 8px;">${escHtml(q.hint)}</p>
      <input type="text" name="${escHtml(q.key)}" placeholder="${escHtml(q.hint)}" style="width:100%;padding:10px;background:var(--ink);border:1px solid var(--line);border-radius:4px;color:var(--bone);font-size:14px;">
    </div>`;
  });
  html += `<div style="display:flex;gap:10px;">
    <button type="submit" class="btn">Save & regenerate schedule</button>
    <button type="button" class="btn btn-ghost" onclick="navigate('goals')">Skip</button>
  </div></form>`;
  main.innerHTML = html;
}

async function submitQuestionnaire(e, goalId) {
  e.preventDefault();
  const form = e.target;
  const data = {};
  new FormData(form).forEach((v, k) => { if (v.trim()) data[k] = v.trim(); });
  try {
    await api(`/goals/${goalId}/details`, { method: 'POST', body: JSON.stringify(data) });
    showToast('Details saved. Schedule regenerating...', 'success');
  } catch (err) { showToast(err.message, 'error'); }
  navigate('goal', goalId);
}

/* ============================================================
   Schedule
   ============================================================ */
async function renderSchedule(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Schedule</div><h1>…</h1></div>`;
  try {
    const payload = await api('/schedule');
    const today = new Date().toISOString().slice(0, 10);
    const missedTotal = payload.missed_total || 0;
    let html = `<div class="page-header"><div class="kicker">15 days out</div><h1>The run of show.</h1><p class="sub">Tap a day to open it. Tick tasks as they close. Overdue sits on top until you deal with it.</p><div class="header-rule"></div></div>`;
    if (missedTotal) {
      html += `<div class="banner banner-bad"><strong>${missedTotal} task${missedTotal === 1 ? '' : 's'} slipped.</strong> They're marked failed until you close them out or redraw the plan.</div>`;
    }
    const dates = {};
    const goalsMap = payload.dates || {};
    for (const goalId of Object.keys(goalsMap)) {
      const data = goalsMap[goalId];
      for (const dateStr of Object.keys(data.by_date)) {
        (dates[dateStr] = dates[dateStr] || []).push(...data.by_date[dateStr].map(a => ({ ...a, goal: data.title })));
      }
    }
    // Past-due first (most recent miss on top), today, then the future.
    const sorted = Object.keys(dates).sort((a, b) => {
      const ap = a < today, bp = b < today;
      if (ap !== bp) return ap ? -1 : 1;
      return ap ? b.localeCompare(a) : a.localeCompare(b);
    });
    if (sorted.length === 0) {
      html += `<div class="empty-state"><h3>Nothing booked.</h3><p>File a goal to start the run.</p></div>`;
    } else {
      html += `<div class="schedule-grid">`;
      sorted.forEach((d, di) => {
        const list = dates[d].sort((x, y) => (x.start_time || '').localeCompare(y.start_time || ''));
        const doneCount = list.filter(a => a.status === 'done').length;
        const missedCount = list.filter(a => a.status === 'missed').length;
        const total = list.length;
        const isToday = d === today;
        const isPast = d < today;
        const pct = total ? Math.round(doneCount / total * 100) : 0;
        const open = isToday || isPast || di < 2 ? 'open' : '';
        const dayChip = isToday
          ? '<span class="chip bad">NOW</span>'
          : (isPast && missedCount ? `<span class="chip warn">${missedCount} MISSED</span>` : '');
        html += `<div class="schedule-day ${open} ${isPast && missedCount ? 'has-missed' : ''}">
          <div class="schedule-day-head sdh-click" onclick="toggleDay(this)">
            <span class="sdh-left">
              <span class="caret">&#9656;</span>
              <span>${escHtml(d)}</span>
              ${dayChip}
            </span>
            <span class="sdh-right"><span class="chip ${doneCount===total?doneCount?'ok':'':doneCount?'hot':'off'}">${doneCount}/${total} done</span><span class="prog-bar"><span style="width:${pct}%"></span></span></span>
          </div>
          <div class="sdh-body">`;
        list.forEach(a => {
          const timeStr = a.start_time && a.end_time ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}` : '';
          const done = a.status === 'done';
          const missed = a.status === 'missed' && !done;
          const cls = done ? 'done' : missed ? 'missed' : '';
          const nowHere = isToday && a.start_time && a.end_time && inNowRange(a.start_time, a.end_time);
          const rowLabel = missed
            ? '<span class="chip warn row-flag">FAILED</span>'
            : '';
          html += `<div class="schedule-row ${cls} ${nowHere ? 'now' : ''}">
            <span class="time">${nowHere ? '<span class="pulse-now">&#9679;</span> ' : ''}${timeStr || '—'}</span>
            <span class="title">${escHtml(a.title)}${rowLabel}<span class="task-name-inline">${escHtml(a.goal)}</span></span>
            <span class="task-name">
              <button class="btn btn-sm ${done ? 'btn-ghost' : missed ? 'btn-warn' : ''}" onclick="toggleTask(${a.id}, this)">${done ? 'REOPEN' : 'DONE'}</button>
            </span>
          </div>`;
        });
        html += `</div></div>`;
      });
      html += `</div>`;
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Schedule</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}

function inNowRange(s, e) {
  const now = new Date();
  const mins = now.getHours() * 60 + now.getMinutes();
  const sc = (s || '').split(':').map(Number), ec = (e || '').split(':').map(Number);
  if (sc.length < 2 || ec.length < 2 || isNaN(sc[0])) return false;
  const sm = sc[0] * 60 + sc[1], em = ec[0] * 60 + ec[1];
  return mins >= sm && mins < em;
}
function toggleDay(head) {
  head.parentElement.classList.toggle('open');
}
async function toggleTask(id, btn) {
  const row = btn.closest('.schedule-row');
  try {
    const r = await api(`/actions/${id}/toggle`, { method: 'POST' });
    const done = r.status === 'done';
    row.classList.toggle('done', done);
    row.classList.toggle('missed', !done && r.status === 'missed');
    btn.textContent = done ? 'REOPEN' : 'DONE';
    btn.classList.toggle('btn-ghost', done);
    const flag = row.querySelector('.row-flag');
    if (flag) flag.style.display = done ? 'none' : '';
    const day = row.closest('.schedule-day');
    const head = day && day.querySelector('.sdh-right');
    if (!head) return;
    const bar = head.querySelector('.prog-bar>span');
    // refetch to recompute counters
    const items = Array.from(row.parentElement.querySelectorAll('.schedule-row'));
    const t = items.length, dnc = items.filter(r2 => r2.classList.contains('done')).length;
    const chip = head.querySelector('.chip');
    chip.textContent = `${dnc}/${t} done`;
    chip.className = 'chip ' + (dnc === t ? 'ok' : (dnc ? 'hot' : 'off'));
    if (bar) bar.style.width = Math.round(dnc / t * 100) + '%';
  } catch (e) { showToast(e.message, 'error'); }
}

/* ============================================================
   Chat side panel — persistent, sits beside whatever page is open.
   ============================================================ */
let _chatMounted = false;

function toggleChatPanel(force) {
  const p = document.getElementById('chat-panel');
  if (!p) return;
  const open = force === undefined ? p.classList.contains('closed') : force;
  p.classList.toggle('closed', !open);
  p.setAttribute('aria-hidden', open ? 'false' : 'true');
  document.getElementById('app-screen').classList.toggle('panel-open', open);
  const railDot = document.getElementById('rail-dot');
  if (railDot) railDot.classList.toggle('on', open);
  if (open) {
    if (!_chatMounted) mountChatPanel();
    const inp = document.getElementById('global-chat-input');
    if (inp) setTimeout(() => inp.focus(), 120);
  }
}
function openChatPanel() { toggleChatPanel(true); }

async function mountChatPanel() {
  if (_chatMounted) return;
  _chatMounted = true;
  const chatEl = document.getElementById('chat-container');
  const inp = document.getElementById('global-chat-input');
  if (inp) {
    inp.onkeypress = (e) => { if (e.key === 'Enter') sendGlobalChat(); };
    inp.oninput = () => {
      const dot = document.getElementById('rail-dot');
      if (dot) dot.classList.toggle('unread', !inp.value && chatEl && chatEl._lastUserMsg);
    };
  }
  refreshPanelModel();
  if (!chatEl) return;
  try {
    const h = await api('/chat');
    if (h.messages && h.messages.length) {
      h.messages.forEach(m => appendChatMsg(chatEl, m.role, m.content));
    } else {
      appendChatMsg(chatEl, 'eloise', "Seat's warm. Say something — or don't, I'll wait.");
    }
  } catch (e) {
    const err = document.createElement('div');
    err.className = 'chat-msg eloise chat-error';
    err.textContent = `Couldn't load the thread: ${e.message}`;
    chatEl.appendChild(err);
  }
}

/* Live model/provider line in the panel header — so "is chat even on?" is
   answerable at a glance instead of guessing from a dead screen. */
async function refreshPanelModel() {
  const el = document.getElementById('panel-model');
  if (!el) return;
  try {
    const s = await api('/status');
    if (s.cloud_configured) {
      el.textContent = `${s.provider || 'cloud'} · ${s.cloud_model || 'model'} — live`;
      el.className = 'panel-model live';
    } else {
      el.textContent = 'no model armed — open Settings';
      el.className = 'panel-model dead';
    }
  } catch (e) {
    el.textContent = 'line check failed';
    el.className = 'panel-model dead';
  }
}
function appendChatMsg(container, role, text, source) {
  const div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  div.innerHTML = linkify(text);
  if (role === 'eloise' && source && source !== 'fallback' && source !== 'completion') {
    const tag = document.createElement('span');
    tag.className = 'chat-src';
    tag.textContent = (source === 'ollama' ? 'local' : source);
    div.appendChild(tag);
  }
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

/* Live typing indicator. Instead of static dots that read as "stalled", we show
   animated equalizer bars + rotating status phrases so the wait feels like motion.
   People tolerate waits when they see believable progress, not empty bouncing dots. */
function addTyping(container) {
  const div = document.createElement('div');
  div.className = 'chat-msg eloise typing';
  const phrases = [
    'reading your message...',
    'checking the board...',
    'thinking...',
    'writing the reply...',
    'wrapping up...',
    'nearly there...',
  ];
  div.innerHTML = `<span class="typing-bars"><span class="bar"></span><span class="bar"></span><span class="bar"></span><span class="bar"></span></span><span class="typing-status">${phrases[0]}</span>`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  let i = 0;
  const statusEl = div.querySelector('.typing-status');
  div._typingTimer = setInterval(() => {
    i = (i + 1) % phrases.length;
    if (statusEl && div.parentNode) statusEl.textContent = phrases[i];
  }, 2600);
  return div;
}
function finishTyping(bubble, text, source) {
  if (bubble._typingTimer) { clearInterval(bubble._typingTimer); bubble._typingTimer = null; }
  bubble.classList.remove('typing');
  bubble.innerHTML = '';
  const txt = document.createElement('span');
  txt.innerHTML = linkify(text);
  bubble.appendChild(txt);
  // Show the engine on every real reply — not just ollama/openrouter. The old
  // filter hid the badge whenever the source resolved to "openai" (which is
  // what Gemini/OpenAI-compatible endpoints report), so a working chat looked
  // dead and unconfigured.
  if (source && source !== 'fallback' && source !== 'completion' && source !== 'guardrail') {
    const tag = document.createElement('span');
    tag.className = 'chat-src';
    tag.textContent = (source === 'ollama' ? 'local' : source);
    bubble.appendChild(tag);
  }
  bubble.parentNode.scrollTop = bubble.parentNode.scrollHeight;
}

/* Live text while streaming: swap the typing animation for actual words as they
   arrive, so the bubble reads as "writing right now" instead of a frozen spinner. */
function setTypingText(bubble, text) {
  if (!bubble || !bubble.parentNode) return;
  if (bubble._typingTimer) { clearInterval(bubble._typingTimer); bubble._typingTimer = null; }
  bubble.classList.remove('typing');
  bubble.innerHTML = '';
  const txt = document.createElement('span');
  txt.innerHTML = linkify(text);
  bubble.appendChild(txt);
  bubble.parentNode.scrollTop = bubble.parentNode.scrollHeight;
}

async function quickChat(cmd) {
  const chatEl = document.getElementById('chat-container');
  if (!chatEl) return;
  openChatPanel();
  if (chatEl._busy) { showToast('I\'m still answering the last one', 'error'); return; }
  chatEl._busy = true;
  const bubble = addTyping(chatEl);
  try {
    const r = await api('/chat/quick', { method: 'POST', body: JSON.stringify({ command: cmd.toLowerCase() }) });
    finishTyping(bubble, r.reply, r.source);
  } catch (e) {
    finishTyping(bubble, e.message, null);
    bubble.classList.add('chat-error');
  } finally {
    chatEl._busy = false;
  }
}

async function streamChatReply(goalId, message, onDelta) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 95000);
  let res;
  try {
    res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify({ goal_id: goalId, message }),
      signal: ctrl.signal,
    });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') {
      throw new Error('The model went quiet for 95 seconds — the connection dropped. Try again; if it keeps stalling, open Settings and hit Test connection.');
    }
    throw new Error('Network dropped mid-reply. Send it again.');
  }
  clearTimeout(timer);
  if (res.status === 401) { logout(); throw new Error('unauthorized'); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const idle = setTimeout(() => ctrl.abort(), 95000);
  let buf = '';
  let full = '';
  let source = null;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!raw.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(raw.slice(6)); } catch (e) { continue; }
        if (ev.error) { const msg = ev.detail || 'model connection dropped'; throw new Error(msg); }
        if (ev.delta) { full += ev.delta; onDelta(full); }
        if (ev.done) { source = ev.source || source; }
      }
    }
  } catch (e) {
    clearTimeout(idle);
    if (e.name === 'AbortError') {
      throw new Error('The reply stalled mid-stream. Send it again — if it keeps stalling, check Settings > LLM Provider → Test connection.');
    }
    throw e;
  }
  clearTimeout(idle);
  // An empty reply with a live source means the cleaner ate everything (or the
  // model echoed my prompt back). The old check only errored for "offline", so
  // a live-but-empty reply drew a blank bubble and read as "chat is broken".
  if (!full) {
    throw new Error("The model returned nothing usable — it may have echoed my prompt back or come out blank. Try rephrasing; if every reply is empty, open Settings and hit Test connection.");
  }
  return { text: full, source: source || 'llm' };
}

async function sendGlobalChat() {
  const inp = document.getElementById('global-chat-input');
  const chatEl = document.getElementById('chat-container');
  if (!inp || !chatEl) return;
  openChatPanel();
  if (chatEl._busy) { showToast('I\'m still answering the last one', 'error'); return; }
  const msg = (inp.value || '').trim();
  if (!msg) return;
  inp.value = '';
  chatEl._busy = true;
  appendChatMsg(chatEl, 'user', msg);
  chatEl._lastUserMsg = true;
  const bubble = addTyping(chatEl);
  try {
    const r = await streamChatReply(null, msg, (partial) => {
      setTypingText(bubble, partial);
      chatEl.scrollTop = chatEl.scrollHeight;
    });
    finishTyping(bubble, r.text, r.source);
    refreshPanelModel();
  } catch (e) {
    finishTyping(bubble, e.message, null);
    bubble.classList.add('chat-error');
  } finally {
    chatEl._busy = false;
  }
}

/* ============================================================
   Goal chat — inline section (no popup)
   ============================================================ */
async function mountGoalChat(goalId) {
  const cc = document.getElementById('goalchat-container');
  if (!cc) return;
  try {
    const h = await api(`/chat/goals/${goalId}`);
    h.messages.forEach(m => appendChatMsg(cc, m.role, m.content));
  } catch (e) {}
  const inp = document.getElementById('goalchat-input');
  if (inp) inp.onkeypress = (e) => { if (e.key === 'Enter') sendGoalChat(goalId); };
}
async function sendGoalChat(goalId) {
  const inp = document.getElementById('goalchat-input');
  const cc = document.getElementById('goalchat-container');
  if (!inp || !cc) return;
  if (cc._busy) { showToast('I\'m still answering the last one', 'error'); return; }
  const msg = (inp.value || '').trim();
  if (!msg) return;
  inp.value = '';
  cc._busy = true;
  appendChatMsg(cc, 'user', msg);
  const bubble = addTyping(cc);
  try {
    const r = await streamChatReply(goalId, msg, (partial) => {
      setTypingText(bubble, partial);
      cc.scrollTop = cc.scrollHeight;
    });
    finishTyping(bubble, r.text, r.source);
    if (r.source === 'completion') setTimeout(() => navigate('goals'), 1000);
  } catch (e) {
    finishTyping(bubble, e.message, null);
    bubble.classList.add('chat-error');
  } finally {
    cc._busy = false;
  }
}

/* ============================================================
   Settings
   ============================================================ */
async function renderSettings(el) {
  let html = `<div class="page-header"><div class="kicker">Settings</div><h1>Tuning the rig.</h1><div class="header-rule"></div></div>`;
  try {
    const [llm, status] = await Promise.all([api('/settings/llm'), api('/status')]);
    window._lastLlm = llm;
    window._lastStatus = status;
    const presets = (llm && llm.presets) || [];
    const presetOpts = presets.map(p =>
      `<option value="${escHtml(p.id)}">${escHtml(p.label)}</option>`).join('');
    const keyChip = llm.api_key_set
      ? `<span class="chip ${llm.key_source === 'env' ? 'warn' : 'ok'}">${llm.key_source === 'env' ? 'key from .env' : 'key saved'}</span>`
      : '<span class="chip off">no key yet</span>';
    const lastTest = window._llmTest;
    const testChip = lastTest
      ? `<span class="chip ${lastTest.ok ? 'ok' : 'bad'}">${lastTest.ok ? 'last test: replies' : 'last test: failed'}</span>`
      : '';
    html += `<div class="settings-section"><h3>LLM Provider</h3>
      <div class="card">
        <div class="llm-status">
          ${status.cloud_configured ? '<span class="chip hot"><span class="ember-dot"></span> Cloud engine armed</span>' : '<span class="chip off">No model talking — paste a key</span>'}
          ${keyChip}
          ${testChip}
          ${status.local_model_available ? '<span class="chip hot">Local engine live</span>' : ''}
          <span class="chip">${escHtml(status.cloud_model || 'no model')}</span>
        </div>
        <div class="field"><label>Provider</label>
          <select id="set-provider" onchange="applyProviderPreset()">
            ${presetOpts}
            <option value="">Custom / other (OpenAI-compatible)</option>
          </select>
        </div>
        <div class="field"><label>API Key</label>
          <input type="password" id="set-api-key" placeholder="paste it — the provider snaps to match" autocomplete="off">
          <p class="field-hint">Pasting is detected: sk-or- is OpenRouter, AIza is Gemini, gsk_ is Groq, csk- is Cerebras. Blank keeps the current key.</p>
        </div>
        <div class="field"><label>API Base URL</label><input type="text" id="set-base-url" placeholder="https://openrouter.ai/api/v1"></div>
        <div class="field"><label>Model</label><input type="text" id="set-model" placeholder="e.g. meta-llama/llama-3.1-8b-instruct:free"></div>
        <div class="field"><label><input type="checkbox" id="set-enable-local" style="width:auto;margin-right:8px;">Also use a local Ollama engine (optional)</label></div>
        <div class="llm-actions">
          <button class="btn" style="width:auto;" onclick="saveLLMSettings()">Save &amp; Test</button>
          <button class="btn btn-ghost" style="width:auto;" onclick="testLLMSettings()">Test connection</button>
          ${llm.api_key_set && llm.key_source === 'settings' ? '<button class="btn btn-ghost" style="width:auto;" onclick="clearLLMKey()">Forget saved key</button>' : ''}
        </div>
        <div id="llm-test-result" class="llm-test"></div>
      </div>
    </div>`;
    html += `<div class="settings-section"><h3>Daily Check-in</h3>
      <div class="card">
        <p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">Pick the hour Eloise checks in and emails your day at you.</p>
        <div class="field"><label>Check-in time</label><input type="time" id="set-checkin-time"></div>
        <button class="btn btn-ghost" style="width:auto;" onclick="saveCheckinTime()">Save Time</button>
      </div>
    </div>`;
    html += `<div class="settings-section"><h3>Always-Busy Windows</h3>
      <div class="card">
        <p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">Windows Eloise never schedules anything in — set once, respected by every goal. E.g. <strong>gym 5pm-7pm</strong>, <strong>class 9-11am</strong>.</p>
        <div id="busy-list" style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;"></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <input type="text" id="busy-new" placeholder="gym 5-7pm" style="flex:1;min-width:180px;">
          <button class="btn btn-ghost" style="width:auto;" onclick="addBusyWindow()">Add</button>
          <button class="btn" style="width:auto;" onclick="saveBusyWindows()">Save</button>
        </div>
      </div>
    </div>`;
    html += `<div class="settings-section"><h3>Account</h3>
      <div class="card">
        <p style="font-size:13px;margin-bottom:12px;color:var(--bone-dim);">On the board as <strong style="color:var(--bone);">${escHtml(userName || '')}</strong></p>
        <button class="btn btn-danger" style="width:auto;color:#fff;" onclick="logout()">Sign Out</button>
      </div>
    </div>`;
    let persona = '';
    try { const ps = await api('/settings/persona'); persona = ps.persona || ''; } catch (e) {}
    html += `<div class="settings-section"><h3>Persona</h3>
      <div class="card">
        <p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">How Eloise talks to you. Set it here or in any chat ("be sarcastic", "never a motivational speaker") — it sticks and overrides the default tone.</p>
        <div class="field"><textarea id="set-persona" rows="4" style="width:100%;min-height:88px;">${escHtml(persona)}</textarea></div>
        <button class="btn" style="width:auto;" onclick="savePersona()">Save Persona</button>
      </div>
    </div>`;
    let locVal = '';
    try { const lc = await api('/settings/location'); locVal = lc.location || ''; } catch (e) {}
    html += `<div class="settings-section"><h3>Location</h3>
      <div class="card">
        <p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">Where you actually live. Chat and schedules only suggest services, shops and apps that exist here — no DoorDash in Lahore.</p>
        <div class="field"><label>Country / city</label>
          <input type="text" id="set-location" value="${escHtml(locVal)}" placeholder="e.g. Pakistan"></div>
        <button class="btn" style="width:auto;" onclick="saveLocation()">Save Location</button>
      </div>
    </div>`;
    try {
      const me = await api('/me');
      const inp = document.getElementById('set-checkin-time');
      if (me.checkin_time && inp) inp.value = me.checkin_time;
    } catch (e) {}
    // Always-busy windows list
    window._busyWindows = [];
    try {
      const bw = await api('/settings/blocked-windows');
      window._busyWindows = bw.blocked_windows || [];
    } catch (e) {}
    // Email channel section + on-demand digest
    let em = null;
    try { em = await api('/email/status'); } catch (e) {}
    const emLive = em && em.configured && em.user_has_email;
    const emCls = emLive ? 'ok' : (em && em.configured ? 'hot' : 'off');
    const emLabel = emLive
      ? 'Mail channel live'
      : (em && em.configured ? `Armed, needs your email${em.host ? ' via ' + escHtml(em.host) : ''}` : 'SMTP not usable yet');
    const emProblems = (em && em.problems && em.problems.length)
      ? `<ul class="problem-list">${em.problems.map(p => `<li>${escHtml(p)}</li>`).join('')}</ul>`
      : '';
    html += `<div class="settings-section"><h3>Email / Digests</h3>
      <div class="card">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
          <span class="chip ${emCls}">${emLabel}</span>
          ${em && em.host ? `<span class="chip">${escHtml(em.host)}:${em.port || 587}</span>` : ''}
          ${em && em.from ? `<span class="chip">from ${escHtml(em.from)}</span>` : ''}
        </div>
        ${emProblems}
        ${em && em.hint && !emLive ? `<p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">${escHtml(em.hint)}</p>` : ''}
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
          <button class="btn" style="width:auto;" onclick="sendDigest()">Email me the week</button>
          <span class="chip ${em && em.user_has_email ? 'ok' : 'off'}">${em && em.user_has_email ? escHtml(userName || 'you') : 'add an email to get mail'}</span>
        </div>
      </div>
    </div>`;
  } catch (e) {
    html += `<p style="color:#ff8b7a;">Failed to load settings.</p>`;
  }
  el.innerHTML = html;
  // populate LLM provider fields
  const llmState = window._lastLlm;
  if (llmState) {
    const sel = document.getElementById('set-provider');
    const bl = document.getElementById('set-base-url');
    const ml = document.getElementById('set-model');
    const prov = (llmState.provider || '').trim().toLowerCase();
    if (sel) {
      const known = (llmState.presets || []).some(p => p.id === prov);
      sel.value = known ? prov : '';
    }
    if (bl) bl.value = llmState.base_url || '';
    if (ml) ml.value = llmState.model || '';
    const elc = document.getElementById('set-enable-local');
    if (elc) elc.checked = !!llmState.enable_local;
  }
  // Pasting a key snaps the provider to match it, so a Gemini key never
  // silently lands on an OpenRouter endpoint (the old way nothing changed).
  const keyInput = document.getElementById('set-api-key');
  if (keyInput) keyInput.addEventListener('input', _detectProviderFromKey);
  renderBusyWindows();
}
function _llmPresets() {
  return (window._lastLlm && window._lastLlm.presets) || [];
}
function _presetById(id) {
  return _llmPresets().find(p => p.id === id) || null;
}
function _detectProviderFromKey() {
  const key = ((document.getElementById('set-api-key') || {}).value || '').trim();
  const sel = document.getElementById('set-provider');
  if (!key || !sel) return;
  // Longest prefixes first: sk-or- must win over the generic sk- of OpenAI.
  const probes = ['sk-or-', 'AIza', 'gsk_', 'csk-', 'sk-'];
  const ids = ['openrouter', 'gemini', 'groq', 'cerebras', 'openai'];
  const idx = probes.findIndex(p => key.startsWith(p));
  if (idx < 0) return;
  const want = ids[idx];
  if (sel.value === want) return;
  if (!_presetById(want)) return;
  sel.value = want;
  applyProviderPreset();
  showToast(`Looks like a ${_presetById(want).label} key — preset applied`, 'success');
}
function applyProviderPreset() {
  const sel = document.getElementById('set-provider');
  const preset = _presetById((sel && sel.value) || '');
  if (!preset) return; // custom endpoint: the operator owns both fields
  const bl = document.getElementById('set-base-url');
  const ml = document.getElementById('set-model');
  // Always overwrite: a provider switch must never inherit the previous
  // provider's endpoint or model.
  if (bl) bl.value = preset.url;
  if (ml) ml.value = preset.model;
}
function _llmFormBody() {
  return {
    provider: (document.getElementById('set-provider') || {}).value || '',
    base_url: (document.getElementById('set-base-url') || {}).value || '',
    model: (document.getElementById('set-model') || {}).value || '',
    api_key: (document.getElementById('set-api-key') || {}).value || '',
    enable_local: !!(document.getElementById('set-enable-local') || {}).checked,
  };
}
function _setTestResult(kind, text) {
  const box = document.getElementById('llm-test-result');
  if (!box) return;
  box.className = 'llm-test ' + (kind || '');
  box.textContent = text || '';
}
async function saveLLMSettings() {
  try {
    await api('/settings/llm', { method: 'PUT', body: JSON.stringify(_llmFormBody()) });
    showToast('Settings saved', 'success');
  } catch (e) { showToast(e.message, 'error'); return; }
  await testLLMSettings();
}
async function testLLMSettings() {
  _setTestResult('pending', 'Contacting the provider…');
  try {
    const r = await api('/settings/llm/test', { method: 'POST', body: JSON.stringify(_llmFormBody()) });
    window._llmTest = { ok: r.ok, at: Date.now() };
    if (r.ok) {
      _setTestResult('ok', `${r.provider} answers — ${r.model} replied in ${r.latency_ms}ms. Eloise is live.`);
      showToast('Provider replies — you are live', 'success');
    } else {
      _setTestResult('bad', r.error || 'Provider did not reply.');
      showToast('Provider failed — details on the card', 'error');
    }
  } catch (e) {
    _setTestResult('bad', e.message);
    showToast('Test failed', 'error');
  }
}
async function clearLLMKey() {
  try {
    await api('/settings/llm', { method: 'PUT', body: JSON.stringify({ clear_key: true }) });
    showToast('Saved key forgotten — falling back to .env', 'success');
    navigate('settings');
  } catch (e) { showToast(e.message, 'error'); }
}
async function saveCheckinTime() {
  const t = document.getElementById('set-checkin-time').value;
  if (!t) { showToast('Pick a time', 'error'); return; }
  try { await api('/settings/checkin-time', { method: 'PUT', body: JSON.stringify({ checkin_time: t }) }); showToast('Check-in time saved', 'success'); } catch (e) { showToast(e.message, 'error'); }
}
async function savePersona() {
  const p = (document.getElementById('set-persona') || {}).value || '';
  try { await api('/settings/persona', { method: 'PUT', body: JSON.stringify({ persona: p }) }); showToast('Persona saved — Eloise obeys it from now on', 'success'); } catch (e) { showToast(e.message, 'error'); }
}
async function saveLocation() {
  const v = ((document.getElementById('set-location') || {}).value || '').trim();
  try {
    const r = await api('/settings/location', { method: 'PUT', body: JSON.stringify({ location: v }) });
    showToast(`Location saved — suggestions now match ${r.location}`, 'success');
  } catch (e) { showToast(e.message, 'error'); }
}
function renderBusyWindows() {
  const wrap = document.getElementById('busy-list');
  if (!wrap) return;
  wrap.innerHTML = (window._busyWindows || []).map((w, i) =>
    `<div class="cons-chip"><span>${escHtml(w)}</span><button class="x-btn" onclick="removeBusyWindow(${i})" title="Remove">&times;</button></div>`
  ).join('') || '<p style="font-size:13px;color:var(--bone-dim);margin:0;">None set — Eloise will plan around nothing.</p>';
}
function addBusyWindow() {
  const inp = document.getElementById('busy-new');
  const val = (inp && inp.value || '').trim();
  if (!val) { showToast('Type a window like gym 5-7pm', 'error'); return; }
  window._busyWindows = window._busyWindows || [];
  if (!window._busyWindows.includes(val.toLowerCase())) window._busyWindows.push(val.toLowerCase());
  if (inp) inp.value = '';
  renderBusyWindows();
}
function removeBusyWindow(i) {
  (window._busyWindows || []).splice(i, 1);
  renderBusyWindows();
}
async function saveBusyWindows() {
  try {
    const r = await api('/settings/blocked-windows', { method: 'PUT', body: JSON.stringify({ blocked_windows: window._busyWindows || [] }) });
    window._busyWindows = r.blocked_windows || [];
    renderBusyWindows();
    showToast('Saved — every schedule now avoids those windows', 'success');
  } catch (e) { showToast(e.message, 'error'); }
}
async function sendDigest() {
  try {
    const r = await api('/email/digest', { method: 'POST' });
    if (r.hint) showToast(r.hint, r.ok ? 'success' : 'error');
    else showToast(r.reason || 'done', r.ok ? 'success' : 'error');
  } catch (e) { showToast(e.message, 'error'); }
}

/* ============================================================
   Boot
   ============================================================ */
window.addEventListener('DOMContentLoaded', () => {
  if (token) {
    const ru = document.getElementById('rail-user');
    if (ru) ru.textContent = (userName || '?').slice(0, 1).toUpperCase();
    enterApp();
    // Restore the page from the URL hash (refresh keeps your place).
    if (!routeFromHash()) navigate('today');
  } else {
    document.getElementById('auth-screen').classList.remove('hidden');
    renderAuth();
  }
  setInterval(() => { if (token) pollCheckIn(); }, 120000);
});