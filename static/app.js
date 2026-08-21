const API = "";
let TOKEN = localStorage.getItem("mara_token");
let USER_NAME = localStorage.getItem("mara_name");
let constraintCount = 0;
let expandedTaskId = null;
const openPanelsByTask = {};
const chatDraftByTask = {};

function el(id) { return document.getElementById(id); }

function showAuthTab(tab) {
  const startPanel = el("startPanel");
  const loginPanel = el("loginPanel");
  const showingStart = tab === "start";
  const outgoing = showingStart ? loginPanel : startPanel;
  const incoming = showingStart ? startPanel : loginPanel;
  el("tabStart").classList.toggle("active", showingStart);
  el("tabLogin").classList.toggle("active", !showingStart);
  outgoing.classList.add("hidden");
  incoming.classList.remove("hidden");
}

el("tabStart").onclick = () => showAuthTab("start");
el("tabLogin").onclick = () => showAuthTab("login");

el("togglePasswordField").onclick = () => {
  const wrap = el("passwordFieldWrap");
  const nowHidden = wrap.classList.toggle("hidden");
  el("togglePasswordField").textContent = nowHidden
    ? "+ add a password (to log in from another device later)"
    : "\u2212 skip the password (start without one)";
};

function showModal({ title, message, showReason, confirmLabel, cancelLabel }) {
  return new Promise((resolve) => {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML = `
      <div class="modal-box">
        <div class="modal-title">${escapeHtml(title)}</div>
        <div class="modal-message">${escapeHtml(message)}</div>
        ${showReason ? `<textarea class="modal-reason" placeholder="why, if you feel like saying (optional)"></textarea>` : ""}
        <div class="modal-actions">
          <button class="btn-ghost modal-cancel">${escapeHtml(cancelLabel || "Never mind")}</button>
          <button class="btn-primary modal-confirm">${escapeHtml(confirmLabel || "Confirm")}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    const cleanup = () => backdrop.remove();
    backdrop.querySelector(".modal-cancel").onclick = () => { cleanup(); resolve(null); };
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) { cleanup(); resolve(null); } });
    backdrop.querySelector(".modal-confirm").onclick = () => {
      const reason = showReason ? backdrop.querySelector(".modal-reason").value.trim() : undefined;
      cleanup();
      resolve(showReason ? { reason } : true);
    };
    if (showReason) backdrop.querySelector(".modal-reason").focus();
  });
}

function showRoastToast(text) {
  const toast = document.createElement("div");
  toast.className = "roast-toast";
  toast.textContent = text;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 7000);
}

async function api(path, method, body) {
  const headers = { "Content-Type": "application/json" };
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  const res = await fetch(API + path, {
    method: method || "GET",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    localStorage.removeItem("mara_token");
    localStorage.removeItem("mara_name");
    TOKEN = null;
    el("dashView").classList.add("hidden");
    el("settingsView").classList.add("hidden");
    el("authView").classList.remove("hidden");
    throw new Error("session invalid");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "request failed" }));
    throw new Error(err.detail || "request failed");
  }
  return res.json();
}

el("startSubmit").onclick = async () => {
  el("startError").textContent = "";
  const name = el("stName").value.trim();
  const email = el("stEmail").value.trim();
  const password = el("stPassword").value.trim();
  if (!name || !email) { el("startError").textContent = "name and email required"; return; }
  try {
    const data = password
      ? await api("/api/signup", "POST", { name, email, password })
      : await api("/api/guest", "POST", { name, email });
    setSession(data);
  } catch (e) { el("startError").textContent = e.message; }
};

el("loginSubmit").onclick = async () => {
  el("loginError").textContent = "";
  const email = el("liEmail").value.trim();
  const password = el("liPassword").value;
  try {
    const data = await api("/api/login", "POST", { email, password });
    setSession(data);
  } catch (e) { el("loginError").textContent = e.message; }
};

function setSession(data) {
  TOKEN = data.token;
  USER_NAME = data.name;
  localStorage.setItem("mara_token", TOKEN);
  localStorage.setItem("mara_name", USER_NAME);
  enterDashboard();
}

function doSignOut() {
  localStorage.removeItem("mara_token");
  localStorage.removeItem("mara_name");
  TOKEN = null;
  el("dashView").classList.add("hidden");
  el("settingsView").classList.add("hidden");
  el("authView").classList.remove("hidden");
}

el("signoutBtn").onclick = doSignOut;
el("settingsSignoutBtn").onclick = doSignOut;

el("settingsBtn").onclick = async () => {
  el("dashView").classList.add("hidden");
  el("settingsView").classList.remove("hidden");
  await loadSettings();
};

el("closeSettingsBtn").onclick = () => {
  el("settingsView").classList.add("hidden");
  el("dashView").classList.remove("hidden");
};

el("mobileMenuBtn").onclick = () => {
  el("sidebar").classList.toggle("open");
  el("sidebarOverlay").classList.toggle("active");
};

el("sidebarOverlay").onclick = () => {
  el("sidebar").classList.remove("open");
  el("sidebarOverlay").classList.remove("active");
};

async function loadSettings() {
  try {
    const me = await api("/api/me");
    el("settingsName").textContent = me.name;
    el("settingsEmail").textContent = me.email;
    el("settingsVerifiedStatus").textContent = me.verified ? "Verified \u2713" : "Not verified yet";
    el("settingsVerifiedStatus").style.color = me.verified ? "var(--success)" : "var(--accent)";
    el("settingsResendBtn").classList.toggle("hidden", me.verified);
  } catch (e) {}
  el("settingsResendBtn").onclick = async () => {
    el("settingsResendBtn").disabled = true;
    el("settingsResendBtn").textContent = "Sent";
    try { await api("/api/verify/resend", "POST"); } catch (e) {}
  };
  try {
    const tasks = await api("/api/tasks");
    const list = el("settingsTaskList");
    list.innerHTML = "";
    const openTasks = tasks.filter(t => t.status === "active");
    if (openTasks.length === 0) {
      list.innerHTML = `<div class="empty-state">No open tasks.</div>`;
      return;
    }
    openTasks.forEach(task => {
      const row = document.createElement("div");
      row.className = "settings-task-row";
      row.innerHTML = `
        <div>
          <div class="settings-task-goal">${escapeHtml(task.goal)}</div>
          <div class="settings-task-meta">due ${task.deadline} \u00b7 ${task.days_left >= 0 ? task.days_left + " day(s) left" : Math.abs(task.days_left) + " overdue"}</div>
        </div>
        <div style="display:flex;gap:8px;">
          <button class="btn-ghost" data-complete>Complete</button>
          <button class="btn-ghost" data-cancel style="color:var(--accent);">Delete</button>
        </div>`;
      row.querySelector("[data-complete]").onclick = async (e) => {
        const confirmed = await showModal({
          title: "Really? Already?",
          message: `This sends a confirmation email for "${task.goal}" — click the link there to actually close it out.`,
          confirmLabel: "Yes, send it",
          cancelLabel: "Not yet",
        });
        if (!confirmed) return;
        e.target.disabled = true;
        e.target.textContent = "Sending...";
        try { const res = await api(`/api/tasks/${task.id}/request-complete`, "POST"); e.target.textContent = res.sent ? "Check email" : "Failed"; } catch (err) { e.target.textContent = "Failed"; }
      };
      row.querySelector("[data-cancel]").onclick = async (e) => {
        const result = await showModal({
          title: "Think again.",
          message: `Deleting "${task.goal}" is permanent once you confirm the email. Are you really, really sure?`,
          showReason: true,
          confirmLabel: "Send delete confirmation",
          cancelLabel: "Never mind",
        });
        if (!result) return;
        e.target.disabled = true;
        e.target.textContent = "Sending...";
        try {
          const res = await api(`/api/tasks/${task.id}/request-delete`, "POST", { reason: result.reason || "" });
          e.target.textContent = res.sent ? "Check email" : "Failed";
          if (res.roast) showRoastToast(res.roast);
        } catch (err) { e.target.textContent = "Failed"; }
      };
      list.appendChild(row);
    });
  } catch (e) {}
}

el("newTaskBtn").onclick = () => {
  el("newTaskPanel").classList.toggle("hidden");
  if (!el("newTaskPanel").classList.contains("hidden")) {
    el("taskGoal").focus();
  }
};

function addConstraintRow(value) {
  constraintCount += 1;
  const id = "c" + constraintCount;
  const row = document.createElement("div");
  row.className = "constraint-row";
  row.innerHTML = `<input type="text" id="${id}" placeholder="e.g. no gym on weekdays" value="${value || ""}" />
    <button type="button" class="remove-btn" data-remove="${id}">x</button>`;
  el("constraintList").appendChild(row);
  row.querySelector("button").onclick = () => row.remove();
}

el("addConstraint").onclick = () => addConstraintRow("");

el("createTaskBtn").onclick = async () => {
  el("taskError").textContent = "";
  const goal = el("taskGoal").value.trim();
  const deadline = el("taskDeadline").value;
  const reminder_time = el("taskTime").value;
  const constraints = Array.from(document.querySelectorAll("#constraintList input"))
    .map(i => i.value.trim()).filter(Boolean);
  if (!goal || !deadline || !reminder_time) {
    el("taskError").textContent = "goal, deadline, and reminder time are required";
    return;
  }
  const btn = el("createTaskBtn");
  btn.disabled = true;
  btn.textContent = "Filing...";
  try {
    await api("/api/tasks", "POST", { goal, deadline, reminder_time, constraints });
    el("taskGoal").value = "";
    el("taskDeadline").value = "";
    el("constraintList").innerHTML = "";
    el("newTaskPanel").classList.add("hidden");
    loadTasks();
    loadSchedule();
  } catch (e) {
    el("taskError").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "File it";
  }
};

function statusInfo(task) {
  if (task.status === "succeeded") return { label: "done", cls: "succeeded" };
  if (task.status === "failed") return { label: "failed", cls: "failed" };
  if (task.days_left < 0) return { label: "overdue", cls: "overdue" };
  return { label: "active", cls: "active" };
}

function timelinePercent(task) {
  const created = new Date(task.created_at);
  const deadline = new Date(task.deadline + "T23:59:59");
  const now = new Date();
  const total = deadline - created;
  if (total <= 0) return 100;
  return Math.max(2, Math.min(100, ((now - created) / total) * 100));
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function renderMara(text) {
  const withBold = (s) => escapeHtml(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const normalized = text.replace(/ - (?=[A-Z0-9])/g, "\n- ");
  const lines = normalized.split("\n");
  let html = "";
  let inList = false;
  for (const rawLine of lines) {
    const line = rawLine.trim();
    const bulletMatch = line.match(/^-\s+(.*)$/);
    if (bulletMatch) {
      if (!inList) { html += '<ul class="mara-list">'; inList = true; }
      html += `<li>${withBold(bulletMatch[1])}</li>`;
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (line) html += `<div class="mara-para">${withBold(line)}</div>`;
    }
  }
  if (inList) html += "</ul>";
  return html || withBold(text);
}

function renderTask(task) {
  const info = statusInfo(task);
  const isOpen = task.status !== "succeeded" && task.status !== "failed";
  const card = document.createElement("div");
  card.id = `task-${task.id}`;
  card.className = "task-card" + (task.days_left < 0 && isOpen ? " overdue-glow" : "") + (expandedTaskId === task.id ? " expanded" : "");

  const daysText = !isOpen
    ? (task.status === "succeeded" ? "done" : "failed")
    : (task.days_left < 0 ? Math.abs(task.days_left) + "d overdue" : task.days_left + "d left");

  const constraintsHtml = task.constraints.length
    ? `<div class="task-constraints">${task.constraints.map(c => `<span class="constraint-tag">${escapeHtml(c)}</span>`).join("")}</div>`
    : "";

  const timelineHtml = isOpen ? `<div class="timeline-bar"><div class="timeline-fill${task.days_left < 0 ? " overdue-fill" : ""}" style="width:${timelinePercent(task)}%"></div></div>` : "";

  const maraLine = task.latest_message
    ? `<div class="mara-line"><span class="prefix">Mara</span><span class="mara-text" id="mara-${task.id}"></span></div>`
    : "";

  const checkinHtml = (isOpen && task.today_checkin === null && task.can_checkin) ? `
    <div class="checkin-row">
      <div class="checkin-q">Did you get it done today?</div>
      <button class="checkin-btn yes" data-yes>Yes</button>
      <button class="checkin-btn no" data-no>No</button>
    </div>` : "";

  card.innerHTML = `
    <div class="task-card-header">
      <svg class="task-card-chevron" viewBox="0 0 16 16" fill="currentColor"><path d="M6 3l5 5-5 5"/></svg>
      <div class="task-card-goal">${escapeHtml(task.goal)}</div>
      <div class="task-card-meta">
        <span class="task-card-days">${daysText}</span>
        <span class="task-card-stamp ${info.cls}">${info.label}</span>
      </div>
    </div>
    <div class="task-card-body">
      <div class="task-card-details">
        <div class="task-detail-row">
          <span>deadline ${task.deadline}</span>
          <span>daily @ ${task.reminder_time}</span>
        </div>
        ${constraintsHtml}
        ${timelineHtml}
        ${maraLine}
        ${checkinHtml}
        <button class="panel-toggle" data-plan-toggle>Schedule <span class="chevron">\u25B6</span></button>
        <div class="expandable-panel" data-plan-panel>
          <div class="panel-content" style="padding:12px;">
            <div data-plan-content></div>
            <button class="btn-secondary regen-btn" data-plan-regen>Regenerate schedule</button>
          </div>
        </div>
        <button class="panel-toggle" data-chat-toggle>Chat with Mara <span class="chevron">\u25B6</span></button>
        <div class="expandable-panel" data-chat-panel>
          <div class="panel-content">
            <div class="chat-messages" data-chat-messages></div>
            <div class="chat-input-row">
              <input type="text" placeholder="say something" data-chat-input />
              <button data-chat-send>Send</button>
            </div>
          </div>
        </div>
        <button class="panel-toggle" data-attach-toggle>Notes, links & files <span class="chevron">\u25B6</span></button>
        <div class="expandable-panel" data-attach-panel>
          <div class="panel-content" style="padding:12px;">
            <div data-attach-list></div>
            <div class="attach-tabs">
              <button class="active" data-attach-tab="note">Note</button>
              <button data-attach-tab="link">Link</button>
              <button data-attach-tab="file">File</button>
            </div>
            <div data-attach-form-note>
              <div class="attach-form-row"><input type="text" placeholder="title" data-note-title /></div>
              <div class="attach-form-row"><input type="text" placeholder="what to remember" data-note-content /></div>
              <button class="btn-secondary" data-note-submit>Add note</button>
            </div>
            <div data-attach-form-link class="hidden">
              <div class="attach-form-row"><input type="text" placeholder="title" data-link-title /></div>
              <div class="attach-form-row"><input type="text" placeholder="https://..." data-link-url /></div>
              <button class="btn-secondary" data-link-submit>Add link</button>
            </div>
            <div data-attach-form-file class="hidden">
              <div class="attach-form-row"><input type="text" placeholder="title" data-file-title /></div>
              <div class="attach-form-row"><input type="file" data-file-input /></div>
              <button class="btn-secondary" data-file-submit>Upload</button>
            </div>
            <div class="error-msg" data-attach-error></div>
          </div>
        </div>
      </div>
    </div>`;

  if (task.latest_message) {
    const target = card.querySelector(`#mara-${task.id}`);
    target.textContent = task.latest_message;
  }

  card.querySelector(".task-card-header").onclick = () => {
    const wasExpanded = card.classList.contains("expanded");
    document.querySelectorAll(".task-card.expanded").forEach(c => c.classList.remove("expanded"));
    if (!wasExpanded) {
      card.classList.add("expanded");
      expandedTaskId = task.id;
    } else {
      expandedTaskId = null;
    }
  };

  if (isOpen) {
    card.querySelector("[data-yes]").onclick = async () => {
      await api(`/api/tasks/${task.id}/checkin`, "POST", { completed: true });
      loadTasks();
      loadMood();
    };
    card.querySelector("[data-no]").onclick = async () => {
      await api(`/api/tasks/${task.id}/checkin`, "POST", { completed: false });
      loadTasks();
      loadMood();
    };
  }

  const chatToggle = card.querySelector("[data-chat-toggle]");
  const chatPanel = card.querySelector("[data-chat-panel]");
  const chatMessages = card.querySelector("[data-chat-messages]");
  const chatInput = card.querySelector("[data-chat-input]");
  const chatSend = card.querySelector("[data-chat-send]");
  const panelState = openPanelsByTask[task.id] || (openPanelsByTask[task.id] = { plan: false, chat: false, attach: false });
  let chatLoaded = false;

  if (chatDraftByTask[task.id]) chatInput.value = chatDraftByTask[task.id];
  chatInput.addEventListener("input", () => { chatDraftByTask[task.id] = chatInput.value; });

  function appendBubble(sender, message) {
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble " + (sender === "user" ? "user" : "mara");
    if (sender === "mara") {
      bubble.innerHTML = `<span class="who">Mara</span>${renderMara(message)}`;
    } else {
      bubble.textContent = message;
    }
    chatMessages.appendChild(bubble);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  async function loadChat() {
    chatMessages.innerHTML = "";
    const history = await api(`/api/tasks/${task.id}/chat`);
    if (history.length === 0) {
      const hint = document.createElement("div");
      hint.className = "chat-typing";
      hint.textContent = "Say anything about this task. Mara will answer.";
      chatMessages.appendChild(hint);
    } else {
      history.forEach(m => appendBubble(m.sender, m.message));
    }
  }

  async function openChatPanel() {
    chatPanel.classList.add("open");
    chatToggle.classList.add("open");
    panelState.chat = true;
    if (!chatLoaded) {
      chatLoaded = true;
      await loadChat();
    }
  }

  chatToggle.onclick = async () => {
    if (chatPanel.classList.contains("open")) {
      chatPanel.classList.remove("open");
      chatToggle.classList.remove("open");
      panelState.chat = false;
      return;
    }
    await openChatPanel();
  };

  async function sendChat() {
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    delete chatDraftByTask[task.id];
    chatInput.disabled = true;
    chatSend.disabled = true;
    appendBubble("user", text);
    const typing = document.createElement("div");
    typing.className = "chat-typing";
    typing.textContent = "Mara is typing...";
    chatMessages.appendChild(typing);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    try {
      const res = await api(`/api/tasks/${task.id}/chat`, "POST", { message: text });
      typing.remove();
      appendBubble("mara", res.reply);
      if (res.source === "fallback") {
        const note = document.createElement("div");
        note.className = "chat-typing";
        note.textContent = "\u26A0 fallback response — no LLM reachable";
        chatMessages.appendChild(note);
      }
      if (res.schedule_building) {
        const note = document.createElement("div");
        note.className = "chat-typing";
        note.textContent = "\u2192 Schedule updating — check the Schedule panel.";
        chatMessages.appendChild(note);
        planLoaded = true;
        task.plan_status = "generating";
        panelState.plan = true;
        planPanel.classList.add("open");
        planToggle.classList.add("open");
        renderPlanText(null, "generating");
        pollUntilReady();
      }
    } catch (e) {
      typing.remove();
      appendBubble("mara", "Couldn't reach the model. Try again.");
    } finally {
      chatInput.disabled = false;
      chatSend.disabled = false;
      chatInput.focus();
    }
  }

  chatSend.onclick = sendChat;
  chatInput.onkeydown = (e) => { if (e.key === "Enter") sendChat(); };

  const planToggle = card.querySelector("[data-plan-toggle]");
  const planPanel = card.querySelector("[data-plan-panel]");
  const planContent = card.querySelector("[data-plan-content]");
  const planRegen = card.querySelector("[data-plan-regen]");

  function renderPlanText(text, status) {
    planContent.innerHTML = "";
    if (status === "generating") {
      planContent.innerHTML = `<div class="chat-typing">Building schedule in background...</div>`;
      return;
    }
    if (!text) {
      planContent.innerHTML = `<div class="chat-typing">No schedule yet. Generate one.</div>`;
      return;
    }
    text.split("\n").filter(l => l.trim()).forEach(line => {
      const dayMatch = line.match(/^Day\s+(\d+)\s*\(([^)]+)\):\s*(.*)$/i);
      const weekMatch = line.match(/^Week\s+(\d+)\s*\(([^)]+)\):\s*(.*)$/i);
      if (dayMatch) {
        const wrap = document.createElement("div");
        wrap.className = "plan-day-group";
        const header = document.createElement("div");
        header.className = "plan-day-header";
        header.textContent = `DAY ${dayMatch[1]} \u00b7 ${dayMatch[2]}`;
        wrap.appendChild(header);
        dayMatch[3].split("|").map(s => s.trim()).filter(Boolean).forEach(block => {
          const row = document.createElement("div");
          row.className = "plan-block";
          row.textContent = block;
          wrap.appendChild(row);
        });
        planContent.appendChild(wrap);
      } else if (weekMatch) {
        const div = document.createElement("div");
        div.className = "plan-week";
        div.innerHTML = `<span class="plan-day-label">WEEK ${weekMatch[1]} \u00b7 ${escapeHtml(weekMatch[2])}</span>${escapeHtml(weekMatch[3])}`;
        planContent.appendChild(div);
      } else if (line.toLowerCase().startsWith("flag:")) {
        const div = document.createElement("div");
        div.className = "plan-flag";
        div.textContent = line;
        planContent.appendChild(div);
      } else {
        const div = document.createElement("div");
        div.className = "plan-block";
        div.textContent = line;
        planContent.appendChild(div);
      }
    });
  }

  let planLoaded = false;
  let planPollTimer = null;

  function pollUntilReady() {
    if (planPollTimer) clearInterval(planPollTimer);
    planPollTimer = setInterval(async () => {
      try {
        const tasks = await api("/api/tasks");
        const fresh = tasks.find(t => t.id === task.id);
        if (fresh && fresh.plan_status !== "generating") {
          clearInterval(planPollTimer);
          planPollTimer = null;
          task.plan_text = fresh.plan_text;
          task.plan_status = fresh.plan_status;
          if (planPanel.classList.contains("open")) {
            renderPlanText(fresh.plan_text, fresh.plan_status);
          }
        }
      } catch (e) {}
    }, 4000);
  }

  async function openPlanPanel() {
    planPanel.classList.add("open");
    planToggle.classList.add("open");
    panelState.plan = true;
    if (!planLoaded) {
      planLoaded = true;
      renderPlanText(task.plan_text, task.plan_status);
      if (task.plan_status === "generating") pollUntilReady();
    }
  }

  planToggle.onclick = () => {
    if (planPanel.classList.contains("open")) {
      planPanel.classList.remove("open");
      planToggle.classList.remove("open");
      panelState.plan = false;
      return;
    }
    openPlanPanel();
  };

  planRegen.onclick = async () => {
    renderPlanText(null, "generating");
    task.plan_status = "generating";
    try {
      await api(`/api/tasks/${task.id}/plan`, "POST");
      pollUntilReady();
    } catch (e) {
      planContent.innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
    }
  };

  const attachToggle = card.querySelector("[data-attach-toggle]");
  const attachPanel = card.querySelector("[data-attach-panel]");
  const attachList = card.querySelector("[data-attach-list]");
  const attachError = card.querySelector("[data-attach-error]");
  const tabs = card.querySelectorAll("[data-attach-tab]");
  const forms = {
    note: card.querySelector("[data-attach-form-note]"),
    link: card.querySelector("[data-attach-form-link]"),
    file: card.querySelector("[data-attach-form-file]"),
  };
  let attachLoaded = false;

  tabs.forEach(tab => {
    tab.onclick = () => {
      tabs.forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      const which = tab.getAttribute("data-attach-tab");
      Object.keys(forms).forEach(k => forms[k].classList.toggle("hidden", k !== which));
    };
  });

  async function loadAttachments() {
    attachList.innerHTML = "";
    const items = await api(`/api/tasks/${task.id}/attachments`);
    if (items.length === 0) { attachList.innerHTML = `<div class="chat-typing">Nothing added yet.</div>`; return; }
    items.forEach(item => {
      const row = document.createElement("div");
      row.className = "attach-item";
      let bodyHtml;
      if (item.kind === "file") bodyHtml = `<a href="/api/attachments/${item.id}/download" target="_blank">${escapeHtml(item.title)}</a>`;
      else if (item.kind === "link") bodyHtml = `<a href="${escapeHtml(item.content)}" target="_blank">${escapeHtml(item.title)}</a>`;
      else bodyHtml = `<span class="attach-title">${escapeHtml(item.title)}: ${escapeHtml(item.content || "")}</span>`;
      row.innerHTML = `<div><span class="attach-kind">${item.kind.toUpperCase()}</span>${bodyHtml}</div>`;
      const delBtn = document.createElement("button");
      delBtn.textContent = "remove";
      delBtn.onclick = async () => { await api(`/api/attachments/${item.id}`, "DELETE"); loadAttachments(); };
      row.appendChild(delBtn);
      attachList.appendChild(row);
    });
  }

  async function openAttachPanel() {
    attachPanel.classList.add("open");
    attachToggle.classList.add("open");
    panelState.attach = true;
    if (!attachLoaded) {
      attachLoaded = true;
      await loadAttachments();
    }
  }

  attachToggle.onclick = async () => {
    if (attachPanel.classList.contains("open")) {
      attachPanel.classList.remove("open");
      attachToggle.classList.remove("open");
      panelState.attach = false;
      return;
    }
    await openAttachPanel();
  };

  card.querySelector("[data-note-submit]").onclick = async () => {
    attachError.textContent = "";
    const title = card.querySelector("[data-note-title]").value.trim();
    const content = card.querySelector("[data-note-content]").value.trim();
    if (!title) { attachError.textContent = "title required"; return; }
    try {
      await api(`/api/tasks/${task.id}/notes`, "POST", { kind: "note", title, content });
      card.querySelector("[data-note-title]").value = "";
      card.querySelector("[data-note-content]").value = "";
      loadAttachments();
    } catch (e) { attachError.textContent = e.message; }
  };

  card.querySelector("[data-link-submit]").onclick = async () => {
    attachError.textContent = "";
    const title = card.querySelector("[data-link-title]").value.trim();
    const url = card.querySelector("[data-link-url]").value.trim();
    if (!title || !url) { attachError.textContent = "title and url required"; return; }
    try {
      await api(`/api/tasks/${task.id}/notes`, "POST", { kind: "link", title, content: url });
      card.querySelector("[data-link-title]").value = "";
      card.querySelector("[data-link-url]").value = "";
      loadAttachments();
    } catch (e) { attachError.textContent = e.message; }
  };

  card.querySelector("[data-file-submit]").onclick = async () => {
    attachError.textContent = "";
    const title = card.querySelector("[data-file-title]").value.trim();
    const fileInput = card.querySelector("[data-file-input]");
    const file = fileInput.files[0];
    if (!file) { attachError.textContent = "choose a file"; return; }
    const formData = new FormData();
    formData.append("title", title || file.name);
    formData.append("file", file);
    try {
      const res = await fetch(`/api/tasks/${task.id}/upload`, {
        method: "POST",
        headers: { "Authorization": "Bearer " + TOKEN },
        body: formData,
      });
      if (!res.ok) { const err = await res.json().catch(() => ({ detail: "upload failed" })); throw new Error(err.detail); }
      card.querySelector("[data-file-title]").value = "";
      fileInput.value = "";
      loadAttachments();
    } catch (e) { attachError.textContent = e.message; }
  };

  if (panelState.chat) openChatPanel();
  if (panelState.plan) openPlanPanel();
  if (panelState.attach) openAttachPanel();

  return card;
}

async function loadTasks() {
  const tasks = await api("/api/tasks");
  const active = tasks.filter(t => t.status !== "succeeded" && t.status !== "failed");
  const closed = tasks.filter(t => t.status === "succeeded" || t.status === "failed");
  const overdue = active.filter(t => t.days_left < 0).length;

  const sidebarList = el("sidebarTaskList");
  if (sidebarList) {
    sidebarList.innerHTML = "";
    if (tasks.length === 0) {
      sidebarList.innerHTML = `<div class="empty-state" style="padding:16px 8px;font-size:11px;">nothing filed</div>`;
    } else {
      tasks.sort((a, b) => a.days_left - b.days_left).forEach(t => {
        const info = statusInfo(t);
        const row = document.createElement("div");
        row.className = "sidebar-task" + (expandedTaskId === t.id ? " active" : "");
        row.innerHTML = `
          <span class="sidebar-task-goal">${escapeHtml(t.goal.length > 28 ? t.goal.slice(0, 26) + "..." : t.goal)}</span>
          <span class="sidebar-task-badge ${info.cls}">${info.label}</span>`;
        row.onclick = () => {
          expandedTaskId = t.id;
          const target = el(`task-${t.id}`);
          if (target) {
            document.querySelectorAll(".task-card.expanded").forEach(c => c.classList.remove("expanded"));
            target.classList.add("expanded");
            target.scrollIntoView({ behavior: "smooth", block: "start" });
          }
          el("sidebar").classList.remove("open");
          el("sidebarOverlay").classList.remove("active");
          loadTasks();
        };
        sidebarList.appendChild(row);
      });
    }
  }

  const activeList = el("taskListActive");
  activeList.innerHTML = "";
  if (active.length === 0) {
    activeList.innerHTML = `<div class="empty-state">nothing filed. file something and get moving.</div>`;
  } else {
    active.sort((a, b) => a.days_left - b.days_left).forEach(t => activeList.appendChild(renderTask(t)));
  }

  const closedList = el("taskListClosed");
  closedList.innerHTML = "";
  if (closed.length > 0) {
    const label = document.createElement("div");
    label.className = "sidebar-section-label";
    label.style.padding = "16px 0 8px";
    label.textContent = "CLOSED";
    closedList.appendChild(label);
    closed.forEach(t => closedList.appendChild(renderTask(t)));
  }

  el("verdictLine").innerHTML = `<span>${active.length} OPEN</span> <span class="count">\u00b7 ${overdue} OVERDUE</span> <span>\u00b7 NO EXCUSES</span>`;
  el("companionSub").textContent = active.length === 0 ? "nothing filed" : `watching ${active.length} open`;
  el("companionSubSide").textContent = active.length === 0 ? "nothing filed" : `watching ${active.length} open`;
}

async function loadMood() {
  try {
    const mood = await api("/api/mood");
    const badge = el("moodBadge");
    badge.textContent = mood.label;
    badge.className = "mood-badge " + mood.mood;
    badge.title = mood.detail;
  } catch (e) {}
}

async function loadSchedule() {
  const days = await api("/api/schedule");
  const strip = el("scheduleStrip");
  strip.innerHTML = "";
  const activeDays = days.filter(d => d.tasks.length > 0 || d.is_today);
  if (activeDays.length === 0) {
    strip.innerHTML = `<div class="schedule-empty">Nothing scheduled in the next 15 days.</div>`;
    return;
  }
  activeDays.forEach(day => {
    const row = document.createElement("div");
    row.className = "schedule-day" + (day.is_today ? " today" : "");
    const itemsHtml = day.tasks.length
      ? day.tasks.map(t => {
          const label = t.is_deadline ? "DUE: " + t.task_name : t.action;
          return `<div class="schedule-day-item${t.is_deadline ? " deadline" : ""}"><span class="task-tag">${escapeHtml(t.task_name)}</span>${escapeHtml(label || "")}</div>`;
        }).join("")
      : `<div class="schedule-empty">Nothing due</div>`;
    row.innerHTML = `<div class="schedule-day-date">${day.is_today ? "TODAY \u00b7 " + day.label : day.label}</div>${itemsHtml}`;
    strip.appendChild(row);
  });
}

let lastGlobalActivity = Date.now();

async function loadGlobalChat() {
  const container = el("globalChatMessages");
  const history = await api("/api/chat");
  container.innerHTML = "";
  if (history.length === 0) {
    const hint = document.createElement("div");
    hint.className = "chat-typing";
    hint.textContent = "She hasn't said anything yet. Say something.";
    container.appendChild(hint);
  } else {
    history.forEach(m => {
      const bubble = document.createElement("div");
      bubble.className = "chat-bubble " + (m.sender === "user" ? "user" : "mara");
      if (m.sender === "mara") {
        bubble.innerHTML = `<span class="who">Mara</span>${renderMara(m.message)}`;
      } else {
        bubble.textContent = m.message;
      }
      container.appendChild(bubble);
    });
  }
  container.scrollTop = container.scrollHeight;
  lastGlobalActivity = Date.now();
}

async function maybeNudge() {
  if (Date.now() - lastGlobalActivity < 5 * 60 * 1000) return;
  try {
    const res = await api("/api/chat/nudge", "POST");
    const container = el("globalChatMessages");
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble mara";
    bubble.innerHTML = `<span class="who">Mara</span>${renderMara(res.message)}`;
    container.appendChild(bubble);
    container.scrollTop = container.scrollHeight;
    lastGlobalActivity = Date.now();
  } catch (e) {}
}

async function sendGlobalChat() {
  lastGlobalActivity = Date.now();
  const input = el("globalChatInput");
  const text = input.value.trim();
  if (!text) return;
  const container = el("globalChatMessages");
  const btn = el("globalChatSend");
  input.value = "";
  input.disabled = true;
  btn.disabled = true;
  const userBubble = document.createElement("div");
  userBubble.className = "chat-bubble user";
  userBubble.textContent = text;
  container.appendChild(userBubble);
  const typing = document.createElement("div");
  typing.className = "chat-typing";
  typing.textContent = "Mara is typing...";
  container.appendChild(typing);
  container.scrollTop = container.scrollHeight;
  try {
    const res = await api("/api/chat", "POST", { message: text });
    typing.remove();
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble mara";
    bubble.innerHTML = `<span class="who">Mara</span>${renderMara(res.reply)}`;
    container.appendChild(bubble);
    if (res.source === "fallback") {
      const note = document.createElement("div");
      note.className = "chat-typing";
      note.textContent = "\u26A0 fallback response \u2014 no LLM reachable";
      container.appendChild(note);
    }
  } catch (e) {
    typing.remove();
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble mara";
    bubble.innerHTML = `<span class="who">Mara</span>Couldn't reach the model. Try again.`;
    container.appendChild(bubble);
  } finally {
    input.disabled = false;
    btn.disabled = false;
    input.focus();
    container.scrollTop = container.scrollHeight;
  }
}

el("globalChatSend").onclick = sendGlobalChat;
el("globalChatInput").onkeydown = (e) => { if (e.key === "Enter") sendGlobalChat(); };

async function checkStatus() {
  try {
    const s = await api("/api/status");
    const problems = [];
    const dot = el("sidebarStatusDot");
    const text = el("sidebarStatusText");
    if (!s.smtp_configured) {
      problems.push("EMAIL NOT CONFIGURED — set SMTP_USER and SMTP_PASS in .env");
    }
    if (!s.ollama_reachable) {
      problems.push("NO MODEL REACHABLE — Ollama off and no OpenRouter key set");
    }
    try {
      const me = await api("/api/me");
      if (!me.verified) {
        problems.push(`EMAIL NOT VERIFIED — check ${escapeHtml(me.email)} for confirmation link`);
      }
    } catch (e) {}
    const banner = el("statusBanner");
    if (problems.length) {
      banner.innerHTML = problems.map(p => "\u26A0 " + p).join("<br>");
      banner.classList.remove("hidden");
      dot.className = "status-dot warn";
      text.textContent = "issues detected";
    } else {
      banner.classList.add("hidden");
      dot.className = "status-dot ok";
      text.textContent = "all systems go";
    }
  } catch (e) {}
}

function enterDashboard() {
  el("authView").classList.add("hidden");
  el("dashView").classList.remove("hidden");
  checkStatus();
  loadTasks();
  loadGlobalChat();
  loadSchedule();
  loadMood();
  setInterval(loadTasks, 60000);
  setInterval(checkStatus, 60000);
  setInterval(loadSchedule, 60000);
  setInterval(maybeNudge, 60000);
  setInterval(loadMood, 60000);
  window.addEventListener("focus", checkStatus);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkStatus(); });
}

if (TOKEN) {
  enterDashboard();
}
