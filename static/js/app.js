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
function escHtml(s) {
  if (s === null || s === undefined) return '';
  const div = document.createElement('div');
  div.textContent = String(s);
  return div.innerHTML;
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
  const t = document.createElement('div');
  t.className = 'toast' + (type === 'error' ? ' error' : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
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
function closeModal() { const m = document.getElementById('app-modal'); if (m) m.remove(); }
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
  document.getElementById('user-name').textContent = userName || '';
  document.getElementById('user-label').textContent = '';
  document.getElementById('user-name').classList.add('ember-dot');
  navigate('today');
  refreshFootMomentum();
  setTimeout(maybeOnboarding, 350);
}

async function refreshFootMomentum() {
  const el = document.getElementById('foot-momentum');
  if (!el) return;
  try {
    const m = await api('/momentum');
    const cls = m.momentum >= 70 ? 'var(--ok)' : m.momentum >= 40 ? 'var(--amber)' : 'var(--blood)';
    el.innerHTML = `momentum <div class="mini-meter"><span style="width:${m.momentum}%;background:${cls}"></span></div> ${m.momentum}`;
  } catch (e) { el.innerHTML = '';
  }
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
   Navigation & router
   ============================================================ */
function navigate(screen, param) {
  currentScreen = screen;
  document.querySelectorAll('.nav-item').forEach(n =>
    n.classList.toggle('active', n.dataset.screen === screen)
  );
  const main = document.getElementById('main-content');
  switch (screen) {
    case 'today': renderToday(main); break;
    case 'board': renderBoard(main); break;
    case 'goals': renderGoals(main); break;
    case 'new-goal': renderNewGoal(main); break;
    case 'goal': renderGoalDetail(main, param); break;
    case 'schedule': renderSchedule(main); break;
    case 'global-chat': renderGlobalChat(main); break;
    case 'settings': renderSettings(main); break;
    default: renderToday(main);
  }
  if (token) setTimeout(pollCheckIn, 600);
}

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
      <p class="sub">${escHtml(today.items.length)} blocks on the board${today.items.length ? ' · none of them moved themselves' : '.'}</p>
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
    html += `<div style="margin-top:auto;padding-top:40px;display:flex;align-items:center;gap:10px;color:var(--bone-mute);font-size:12px;">
      <span class="ember-dot"></span>
      ${status.local_model_available
        ? '<span class="chip hot">Local engine live</span>'
        : '<span class="chip off">&#x25CB; Local engine off — checks fall back to scripted Eloise</span>'}
      <span>${status.ollama_error ? escHtml(status.ollama_error) : ''}</span>
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
          <p class="sub" style="margin:0;">${m.done_days_total} days with real completed blocks count toward this.</p>
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
      html += `<div class="empty-state"><h3>No schedule drawn yet</h3></div>`;
    } else {
      dates.forEach(d => {
        html += `<div class="schedule-day"><div class="schedule-day-head"><span>${escHtml(d)}</span><span class="chip hot">${byDate[d].length}</span></div>`;
        byDate[d].forEach(a => {
          const timeStr = a.start_time && a.end_time ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}` : '';
          const cls = a.status === 'done' ? 'done' : a.status === 'missed' ? 'missed' : '';
          html += `<div class="schedule-row ${cls}">
            <span class="time">${timeStr || '—'}</span>
            <span class="title">${escHtml(a.title)}</span>
            <span class="task-name">${escHtml(a.status)}</span>
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
      cons.forEach(c => html += `<div class="note-card"><span class="who">blocked</span><div>${escHtml(c)}</div></div>`);
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
async function regenPlan(id) {
  showToast('Eloise is drawing the schedule...', 'success');
  try {
    await api(`/goals/${id}/plan`, { method: 'POST' });
    showToast('Schedule redrawn.', 'success');
  } catch (e) { showToast(e.message, 'error'); }
  navigate('goal', id);
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
    // if questionnaire questions returned, show them before finishing
    if (r.questions && r.questions.length > 0) {
      showQuestionnaire(r.id, title, r.questions);
    } else {
      showToast('Filed. Eloise draws the lines.', 'success');
      navigate('goals');
    }
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
    const schedule = await api('/schedule');
    const today = new Date().toISOString().slice(0, 10);
    let html = `<div class="page-header"><div class="kicker">15 days out</div><h1>The run of show.</h1><p class="sub">Tap a day to open it. Tick blocks as they close.</p><div class="header-rule"></div></div>`;
    const dates = {};
    for (const goalId of Object.keys(schedule)) {
      const data = schedule[goalId];
      for (const dateStr of Object.keys(data.by_date)) {
        (dates[dateStr] = dates[dateStr] || []).push(...data.by_date[dateStr].map(a => ({ ...a, goal: data.title })));
      }
    }
    const sorted = Object.keys(dates).sort();
    if (sorted.length === 0) {
      html += `<div class="empty-state"><h3>Nothing booked.</h3><p>File a goal to start the run.</p></div>`;
    } else {
      html += `<div class="schedule-grid">`;
      sorted.forEach((d, di) => {
        const list = dates[d].sort((x, y) => (x.start_time || '').localeCompare(y.start_time || ''));
        const doneCount = list.filter(a => a.status === 'done').length;
        const total = list.length;
        const isToday = d === today;
        const pct = total ? Math.round(doneCount / total * 100) : 0;
        const open = isToday || di < 2 ? 'open' : '';
        html += `<div class="schedule-day ${open}">
          <div class="schedule-day-head sdh-click" onclick="toggleDay(this)">
            <span class="sdh-left">
              <span class="caret">&#9656;</span>
              <span>${escHtml(d)}</span>
              ${isToday ? '<span class="chip bad">NOW</span>' : ''}
            </span>
            <span class="sdh-right"><span class="chip ${doneCount===total?doneCount?'ok':'':doneCount?'hot':'off'}">${doneCount}/${total} done</span><span class="prog-bar"><span style="width:${pct}%"></span></span></span>
          </div>
          <div class="sdh-body">`;
        list.forEach(a => {
          const timeStr = a.start_time && a.end_time ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}` : '';
          const cls = a.status === 'done' ? 'done' : a.status === 'missed' ? 'missed' : '';
          const nowHere = isToday && a.start_time && a.end_time && inNowRange(a.start_time, a.end_time);
          html += `<div class="schedule-row ${cls} ${nowHere ? 'now' : ''}">
            <span class="time">${nowHere ? '<span class="pulse-now">&#9679;</span> ' : ''}${timeStr || '—'}</span>
            <span class="title">${escHtml(a.title)}<span class="task-name-inline">${escHtml(a.goal)}</span></span>
            <span class="task-name">
              <button class="btn btn-sm ${a.status === 'done' ? 'btn-ghost' : ''}" onclick="toggleTask(${a.id}, this)">${a.status === 'done' ? 'REOPEN' : 'DONE'}</button>
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
    btn.textContent = done ? 'REOPEN' : 'DONE';
    btn.classList.toggle('btn-ghost', done);
    const head = row.closest('.schedule-day').querySelector('.sdh-right');
    const bar = head.querySelector('.prog-bar>span');
    // refetch to recompute counters
    const items = Array.from(row.parentElement.querySelectorAll('.schedule-row'));
    const t = items.length, dnc = items.filter(r2 => r2.classList.contains('done')).length;
    const chip = head.querySelector('.chip');
    chip.textContent = `${dnc}/${t} done`;
    chip.className = 'chip ' + (dnc === t ? 'ok' : (dnc ? 'hot' : 'off'));
    bar.style.width = Math.round(dnc / t * 100) + '%';
  } catch (e) { showToast(e.message, 'error'); }
}

/* ============================================================
   Chat (global)
   ============================================================ */
async function renderGlobalChat(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Direct line</div><h1>Eloise.</h1><p class="sub">No motivation. No excuses. Task gets done.</p><div class="header-rule"></div></div>
    <div id="eloise-console" class="console mood-idle">
      <div class="console-head">
        <div class="console-sigil">E</div>
        <div class="console-id">
          <div class="console-name">ELOISE <span class="console-ver">//exe v2.1.4</span></div>
          <div class="console-boot" id="eloise-boot">SEATING…</div>
        </div>
        <div class="console-glow" id="eloise-glow"></div>
      </div>
      <div class="console-state" id="eloise-state">STATE: BOOTING — the machine is watching you.</div>
      <div class="console-telemetry" id="eloise-telemetry"></div>
    </div>
    <div id="chat-container" class="chat-container"></div>
    <div class="chat-quick">
      <span class="kicker" style="display:block;margin-bottom:8px;">Tap a wire</span>
      <button class="chip quick-chip" onclick="quickChat('Today')">Today</button>
      <button class="chip quick-chip" onclick="quickChat('Score')">Score</button>
      <button class="chip quick-chip" onclick="quickChat('Fire')">Urgent</button>
      <button class="chip quick-chip" onclick="quickChat('Momentum')">Momentum</button>
      <button class="chip quick-chip" onclick="quickChat('Stakes')">Stakes</button>
      <button class="chip quick-chip" onclick="quickChat('Fuck it')">Fuck it</button>
    </div>
    <div class="chat-input">
      <input type="text" id="global-chat-input" placeholder="Talk to Eloise...">
      <button class="btn btn-sm" onclick="sendGlobalChat()">Send</button>
    </div>`;
  // live console telemetry + mood
  renderEloiseConsole();
  const chatEl = document.getElementById('chat-container');
  try {
    const h = await api('/chat');
    h.messages.forEach(m => appendChatMsg(chatEl, m.role, m.content));
  } catch (e) {}
  const inp = document.getElementById('global-chat-input');
  inp.focus();
  inp.onkeypress = (e) => { if (e.key === 'Enter') sendGlobalChat(); };
}

/* Eloise console: live telemetry + mood-reactive glow. Pulls /api/eloise. */
async function renderEloiseConsole() {
  const con = document.getElementById('eloise-console');
  if (!con) return;
  try {
    const s = await api('/eloise');
    con.className = `console mood-${escHtml(s.mood)}`;
    const boot = document.getElementById('eloise-boot');
    if (boot) boot.textContent = `SEATED ${s.uptime} · online`;
    const state = document.getElementById('eloise-state');
    if (state) state.textContent = s.status;
    const tel = document.getElementById('eloise-telemetry');
    if (tel) {
      let cells = [
        { k: 'MOMENTUM', v: `${s.momentum}/100` },
        { k: 'STREAK', v: `${s.streak}d` },
        { k: 'BURN', v: `${s.burn_hours}h/day` },
      ];
      if (s.top_pressure) {
        const pc = s.top_pressure.score;
        const cls = pc >= 8 ? 'c-bad' : pc >= 6 ? 'c-hot' : pc >= 4 ? 'c-warn' : 'c-ok';
        cells.unshift({ k: `TOP PRESSURE · ${escHtml(s.top_pressure.goal)}`, v: `${pc}/10`, cls });
      }
      tel.innerHTML = cells.map(c =>
        `<div class="cell ${c.cls || ''}"><span class="cell-k">${c.k}</span><span class="cell-v">${c.v}</span></div>`
      ).join('');
    }
  } catch (e) {}
}
/* Flip the console over to booting while Eloise "thinks", then re-derive her mood. */
function flashTyping() {
  const con = document.getElementById('eloise-console');
  if (con) con.className = 'console mood-idle';
  renderEloiseConsole();
}
function appendChatMsg(container, role, text, source) {
  const div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  div.textContent = text;
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

/* Live typing indicator: animated dots bubble shown while Eloise is "thinking". */
function addTyping(container) {
  const div = document.createElement('div');
  div.className = 'chat-msg eloise typing';
  div.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}
function finishTyping(bubble, text, source) {
  bubble.classList.remove('typing');
  bubble.innerHTML = '';
  const txt = document.createElement('span');
  txt.textContent = text;
  bubble.appendChild(txt);
  if (source && (source === 'ollama' || source === 'openrouter')) {
    const tag = document.createElement('span');
    tag.className = 'chat-src';
    tag.textContent = (source === 'ollama' ? 'local' : source);
    bubble.appendChild(tag);
  }
  bubble.parentNode.scrollTop = bubble.parentNode.scrollHeight;
}

async function quickChat(cmd) {
  const chatEl = document.getElementById('chat-container');
  if (!chatEl) { navigate('global-chat'); setTimeout(() => quickChat(cmd), 400); return; }
  const bubble = addTyping(chatEl);
  try {
    const r = await api('/chat/quick', { method: 'POST', body: JSON.stringify({ command: cmd.toLowerCase() }) });
    finishTyping(bubble, r.reply, r.source);
  } catch (e) {
    finishTyping(bubble, e.message, null);
  }
}

async function sendGlobalChat() {
  const inp = document.getElementById('global-chat-input');
  const chatEl = document.getElementById('chat-container');
  const msg = (inp.value || '').trim();
  if (!msg) return;
  inp.value = '';
  appendChatMsg(chatEl, 'user', msg);
  const bubble = addTyping(chatEl);
  flashTyping();
  try {
    const r = await api('/chat', { method: 'POST', body: JSON.stringify({ goal_id: null, message: msg }) });
    finishTyping(bubble, r.reply, r.source);
  } catch (e) {
    finishTyping(bubble, e.message, null);
  }
  renderEloiseConsole();
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
  const msg = (inp.value || '').trim();
  if (!msg) return;
  inp.value = '';
  appendChatMsg(cc, 'user', msg);
  const bubble = addTyping(cc);
  try {
    const r = await api('/chat', { method: 'POST', body: JSON.stringify({ goal_id: goalId, message: msg }) });
    finishTyping(bubble, r.reply, r.source);
    if (r.goal_status === 'succeeded') setTimeout(() => navigate('goals'), 1000);
  } catch (e) { finishTyping(bubble, e.message, null); }
}

/* ============================================================
   Settings
   ============================================================ */
async function renderSettings(el) {
  let html = `<div class="page-header"><div class="kicker">Settings</div><h1>Tuning the rig.</h1><div class="header-rule"></div></div>`;
  try {
    const [llm, status] = await Promise.all([api('/settings/llm'), api('/status')]);
    html += `<div class="settings-section"><h3>LLM Providers</h3>
      <div class="card">
        <div style="margin-bottom:18px;">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap;">
            ${status.local_model_available ? '<span class="chip hot"><span class="ember-dot"></span> Local engine live</span>' : '<span class="chip off">&#x25CB; Local engine off</span>'}
            ${status.cloud_configured ? '<span class="chip hot">OpenRouter armed</span>' : '<span class="chip off">&#x25CB; OpenRouter off</span>'}
          </div>
          <p style="font-size:13px;color:var(--bone-dim);">Primary: ${escHtml(status.ollama_model || 'not set')} · Fallback: ${escHtml(llm.openrouter_model || 'not set')}</p>
        </div>
        <div class="field"><label>Ollama URL</label><input type="text" id="set-ollama-url" value="${escHtml(status.ollama_url || llm.ollama_url || '')}"></div>
        <div class="field"><label>Ollama Model</label><input type="text" id="set-ollama-model" value="${escHtml(status.ollama_model || llm.ollama_model || '')}"></div>
        <div class="field"><label>OpenRouter Model</label><input type="text" id="set-or-model" value="${escHtml(llm.openrouter_model || '')}"></div>
        <div class="field"><label>OpenRouter API Key</label><input type="password" id="set-or-key" placeholder="Leave blank to keep current"></div>
        <button class="btn" style="width:auto;" onclick="saveLLMSettings()">Save Settings</button>
      </div>
    </div>`;
    html += `<div class="settings-section"><h3>Daily Check-in</h3>
      <div class="card">
        <p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">Pick the hour Eloise checks in and emails your day at you.</p>
        <div class="field"><label>Check-in time</label><input type="time" id="set-checkin-time"></div>
        <button class="btn btn-ghost" style="width:auto;" onclick="saveCheckinTime()">Save Time</button>
      </div>
    </div>`;
    html += `<div class="settings-section"><h3>Account</h3>
      <div class="card">
        <p style="font-size:13px;margin-bottom:12px;color:var(--bone-dim);">On the board as <strong style="color:var(--bone);">${escHtml(userName || '')}</strong></p>
        <button class="btn btn-danger" style="width:auto;color:#fff;" onclick="logout()">Sign Out</button>
      </div>
    </div>`;
    try {
      const me = await api('/me');
      const inp = document.getElementById('set-checkin-time');
      if (me.checkin_time && inp) inp.value = me.checkin_time;
    } catch (e) {}
    // Email channel section + on-demand digest
    let em = null;
    try { em = await api('/email/status'); } catch (e) {}
    const emCls = em && em.configured && em.user_has_email ? 'ok' : (em && em.configured ? 'hot' : 'off');
    const emLabel = em && em.configured && em.user_has_email
      ? 'Mail channel live'
      : (em && em.configured ? `Armed, needs your email${em.host ? ' via ' + escHtml(em.host) : ''}` : 'SMTP off');
    html += `<div class="settings-section"><h3>Email / Digests</h3>
      <div class="card">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
          <span class="chip ${emCls}">${emLabel}</span>
          ${em && !em.configured ? '<span class="chip off">set SMTP_HOST + MAIL_FROM in .env</span>' : ''}
        </div>
        ${em && em.hint ? `<p style="font-size:13px;color:var(--bone-dim);margin-bottom:14px;">${escHtml(em.hint)}</p>` : ''}
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
          <button class="btn" style="width:auto;" onclick="sendDigest()">Email me the week</button>
          <span class="chip ${em && em.configured && em.user_has_email ? 'ok' : 'off'}">${em && em.user_has_email ? escHtml(userName || 'you') : 'add an email to get mail'}</span>
        </div>
      </div>
    </div>`;
  } catch (e) {
    html += `<p style="color:#ff8b7a;">Failed to load settings.</p>`;
  }
  el.innerHTML = html;
}
async function saveLLMSettings() {
  const body = {
    ollama_url: document.getElementById('set-ollama-url').value,
    ollama_model: document.getElementById('set-ollama-model').value,
    openrouter_model: document.getElementById('set-or-model').value,
    openrouter_key: document.getElementById('set-or-key').value,
  };
  try { await api('/settings/llm', { method: 'PUT', body: JSON.stringify(body) }); showToast('Settings saved', 'success'); } catch (e) { showToast(e.message, 'error'); }
}
async function saveCheckinTime() {
  const t = document.getElementById('set-checkin-time').value;
  if (!t) { showToast('Pick a time', 'error'); return; }
  try { await api('/settings/checkin-time', { method: 'PUT', body: JSON.stringify({ checkin_time: t }) }); showToast('Check-in time saved', 'success'); } catch (e) { showToast(e.message, 'error'); }
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
    document.getElementById('user-name').textContent = userName || '';
    document.getElementById('user-name').classList.add('ember-dot');
    enterApp();
  } else {
    document.getElementById('auth-screen').classList.remove('hidden');
    renderAuth();
  }
  setInterval(() => { if (token) pollCheckIn(); }, 120000);
});