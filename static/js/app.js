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
    <h2>Claim a seat</h2>
    <p class="sub">Eloise keeps the ledger. You keep time.</p>
    <div class="field"><label>Name</label><input type="text" id="auth-name" placeholder="Your name"></div>
    <div class="field"><label>Email</label><input type="email" id="auth-email" placeholder="You@place.com"></div>
    <div class="field"><label>Password</label><input type="password" id="auth-pass" placeholder="At least 6 characters"></div>
    <button class="btn" onclick="doSignup()">Sign in</button>
    <div class="rule"><span>or</span></div>
    <button class="btn btn-ghost" onclick="doGuest()">Guest pass</button>
  `;
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
  const name = document.getElementById('auth-name').value || 'Guest';
  try {
    const r = await api('/guest', { method: 'POST', body: JSON.stringify({ name }) });
    token = r.token; userName = r.name;
    localStorage.setItem('eloise_token', token);
    localStorage.setItem('eloise_name', userName);
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
}

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

    let html = `<div class="page-header">
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
      html += `<div class="empty-state"><div class="big">&#x2737;</div><h3>Nothing booked today.</h3><p>File a goal and Eloise draws the lines.</p></div>`;
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
    html += `<div style="margin-top:40px;display:flex;align-items:center;gap:10px;color:var(--bone-mute);font-size:12px;">
      <span class="ember-dot"></span>
      ${status.local_model_available
        ? '<span class="chip hot">Local engine live</span>'
        : '<span class="chip off">&#x25CB; Local engine off — checks fall back to scripted Eloise</span>'}
      <span>${status.ollama_error ? escHtml(status.ollama_error) : ''}</span>
    </div>`;

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
   Goals
   ============================================================ */
async function renderGoals(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Goals</div><h1>…</h1></div>`;
  try {
    const goals = await api('/goals');
    let html = `<div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-end;">
      <div><div class="kicker">Archive</div><h1>Open files.</h1><p class="sub">${goals.length} total</p></div>
      <button class="btn" style="width:auto;" onclick="navigate('new-goal')">+ File</button>
    </div>`;
    if (goals.length === 0) {
      html += `<div class="empty-state"><div class="big">&#x2737;</div><h3>Nothing on file.</h3><p>Give Eloise something real to chase.</p></div>`;
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
      <button class="btn btn-sm" onclick="goGoalChat(${g.id})">Chat on this</button>
      <button class="btn btn-sm btn-ghost" onclick="regenPlan(${g.id})">Redraw schedule</button>
      <button class="btn btn-sm btn-ghost" onclick="completeGoal(${g.id})">Mark done</button>
      <button class="btn btn-sm btn-danger" style="color:#fff;" onclick="deleteGoal(${g.id})">Burn file</button>
    </div>`;

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

    el.innerHTML = html;
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
function goGoalChat(id) { showGoalChat(id); }
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
async function regenPlan(id) {
  try { await api(`/goals/${id}/plan`, { method: 'POST' }); showToast('Redrawing...', 'success'); setTimeout(() => navigate('goal', id), 1800); } catch (e) {}
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
    await api('/goals', { method: 'POST', body: JSON.stringify({ title, deadline, reminder_time: reminder, constraints: window._constraints || [] }) });
    showToast('Filed. Eloise draws the lines.', 'success');
    navigate('goals');
  } catch (e) { showToast(e.message, 'error'); }
}

/* ============================================================
   Schedule
   ============================================================ */
async function renderSchedule(el) {
  el.innerHTML = `<div class="page-header"><div class="kicker">Schedule</div><h1>…</h1></div>`;
  try {
    const schedule = await api('/schedule');
    let html = `<div class="page-header"><div class="kicker">15 days out</div><h1>The run of show.</h1><div class="header-rule"></div></div>`;
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
      sorted.forEach(d => {
        html += `<div class="schedule-day"><div class="schedule-day-head"><span>${escHtml(d)}</span><span class="chip hot">${dates[d].length} tasks</span></div>`;
        dates[d].forEach(a => {
          const timeStr = a.start_time && a.end_time ? `${fmtTime(a.start_time)} — ${fmtTime(a.end_time)}` : '';
          const cls = a.status === 'done' ? 'done' : a.status === 'missed' ? 'missed' : '';
          html += `<div class="schedule-row ${cls}">
            <span class="time">${timeStr || '—'}</span>
            <span class="title">${escHtml(a.title)}</span>
            <span class="task-name">${escHtml(a.goal)}</span>
          </div>`;
        });
        html += `</div>`;
      });
      html += `</div>`;
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div class="page-header"><div class="kicker">Schedule</div><h1>Nothing loads.</h1><p class="sub">${escHtml(e.message)}</p></div>`;
  }
}

/* ============================================================
   Chat (global)
   ============================================================ */
async function renderGlobalChat(el) {
  let html = `<div class="page-header"><div class="kicker">Direct line</div><h1>Eloise.</h1><p class="sub">No motivation. No excuses. Task gets done.</p><div class="header-rule"></div></div>
    <div id="chat-container" class="chat-container"></div>
    <div class="chat-input">
      <input type="text" id="global-chat-input" placeholder="Talk to Eloise...">
      <button class="btn btn-sm" onclick="sendGlobalChat()">Send</button>
    </div>`;
  el.innerHTML = html;
  const chatEl = document.getElementById('chat-container');
  try {
    const h = await api('/chat');
    h.messages.forEach(m => appendChatMsg(chatEl, m.role, m.content));
  } catch (e) {}
  const inp = document.getElementById('global-chat-input');
  inp.focus();
  inp.onkeypress = (e) => { if (e.key === 'Enter') sendGlobalChat(); };
}
function appendChatMsg(container, role, text) {
  const div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  div.textContent = text;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}
async function sendGlobalChat() {
  const inp = document.getElementById('global-chat-input');
  const chatEl = document.getElementById('chat-container');
  const msg = (inp.value || '').trim();
  if (!msg) return;
  inp.value = '';
  appendChatMsg(chatEl, 'user', msg);
  appendChatMsg(chatEl, 'eloise', '…');
  try {
    const r = await api('/chat', { method: 'POST', body: JSON.stringify({ goal_id: null, message: msg }) });
    chatEl.lastChild.textContent = r.reply;
  } catch (e) {
    chatEl.lastChild.textContent = e.message;
  }
}

/* ============================================================
   Goal chat modal
   ============================================================ */
async function showGoalChat(goalId) {
  const bodyHtml = `<div id="goalchat-container" class="chat-container" style="min-height:260px;"></div>
    <div class="chat-input"><input type="text" id="goalchat-input" placeholder="Talk on this file..."><button class="btn btn-sm" onclick="sendGoalChat(${goalId})">Send</button></div>`;
  openModal('On this file', bodyHtml, '');
  const cc = document.getElementById('goalchat-container');
  try {
    const h = await api(`/chat/goals/${goalId}`);
    h.messages.forEach(m => appendChatMsg(cc, m.role, m.content));
  } catch (e) {}
  const inp = document.getElementById('goalchat-input');
  inp.focus();
  inp.onkeypress = (e) => { if (e.key === 'Enter') sendGoalChat(goalId); };
}
async function sendGoalChat(goalId) {
  const inp = document.getElementById('goalchat-input');
  const cc = document.getElementById('goalchat-container');
  const msg = (inp.value || '').trim();
  if (!msg) return;
  inp.value = '';
  appendChatMsg(cc, 'user', msg);
  appendChatMsg(cc, 'eloise', '…');
  try {
    const r = await api('/chat', { method: 'POST', body: JSON.stringify({ goal_id: goalId, message: msg }) });
    cc.lastChild.textContent = r.reply;
    if (r.goal_status === 'succeeded') setTimeout(() => { closeModal(); navigate('goals'); }, 1000);
  } catch (e) { cc.lastChild.textContent = e.message; }
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