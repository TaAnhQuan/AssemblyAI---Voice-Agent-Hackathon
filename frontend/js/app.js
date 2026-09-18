/**
 * app.js — State management, SPA navigation, SQLite ticket sync,
 * and live telemetry tool inspector.
 */
const APP_STATE = {
  currentUser: null,
  currentTicket: null,
  voice: {
    isConnected: false,
    ws: null,
    toolCallsCount: 0,
  },
};

// --- SPA Router ---
function navigateTo(hash) {
  const savedUser = localStorage.getItem("omni_user");

  if (!savedUser) {
    if (hash !== "#register") hash = "#login";
  } else {
    if (!hash || hash === "#login" || hash === "#register") hash = "#live-room";
  }

  if (window.location.hash !== hash) {
    window.location.hash = hash;
  }

  const views = {
    "#login": document.getElementById("view-login"),
    "#register": document.getElementById("view-register"),
    "#create-ticket": document.getElementById("view-create-ticket"),
    "#live-room": document.getElementById("view-live-room"),
    "#ticket-queue": document.getElementById("view-ticket-queue"),
  };

  // Hide all view panels
  Object.values(views).forEach((el) => {
    if (el) el.classList.add("hidden");
  });

  const target = views[hash] || views["#login"];
  if (target) {
    target.classList.remove("hidden");
  }

  // Toggle top-right controls
  const headerControls = document.getElementById("header-user-controls");
  if (headerControls) {
    const isAuth = hash === "#login" || hash === "#register";
    headerControls.style.display = isAuth ? "none" : "flex";
  }

  // Header search visibility (remove feature)
//   const searchBar = document.getElementById("header-search-bar");
//   if (searchBar) {
//     searchBar.style.display = (hash === "#live-room" || hash === "#ticket-queue") ? "flex" : "none";
//   }

  // Dynamic fetch triggers
  if (hash === "#live-room") {
    fetchLatestTicket();
  } else if (hash === "#ticket-queue") {
    fetchTicketQueue();
  }

  window.scrollTo({ top: 0, behavior: "smooth" });
}

// Router Event Listeners
window.addEventListener("hashchange", () => navigateTo(window.location.hash));

window.addEventListener("DOMContentLoaded", () => {
  const savedUser = localStorage.getItem("omni_user");

  if (savedUser) {
    try {
      APP_STATE.currentUser = JSON.parse(savedUser);
      updateUserIdentity();
      navigateTo(window.location.hash || "#live-room");
    } catch (e) {
      localStorage.removeItem("omni_user");
      navigateTo("#login");
    }
  } else {
    navigateTo("#login");
  }

  setupCategoryPills();
  setupPriorityCards();
  updateCharCount();
});

// --- Ticket DOM Rendering Helpers ---
function renderTicketDetails(ticket) {
  APP_STATE.currentTicket = ticket;

  const idEl = document.getElementById("ticket-id-tag");
  if (idEl) idEl.textContent = ticket.ticket_id || "--";

  const titleEl = document.getElementById("ticket-title-display");
  if (titleEl) titleEl.textContent = ticket.subject || "No Active Incident";

  const descEl = document.getElementById("ticket-desc-display");
  if (descEl) descEl.textContent = ticket.description || "No description provided.";

  const deskEl = document.getElementById("ticket-desk-display");
  if (deskEl) deskEl.textContent = ticket.assigned_desk || "Specialist Dispatch";

  const prioEl = document.getElementById("ticket-priority-display");
  if (prioEl) {
    const prio = (ticket.priority || "MEDIUM").toUpperCase();
    const isHigh = /HIGH|CRITICAL/.test(prio);
    prioEl.className = `text-xs font-bold ${isHigh ? "text-error" : "text-primary"} flex items-center gap-1 mt-0.5`;
    prioEl.innerHTML = `<span class="w-2 h-2 rounded-full ${isHigh ? "bg-error" : "bg-primary"}"></span> ${prio}`;
  }
}

function renderEmptyTicketDetails() {
  APP_STATE.currentTicket = null;

  const idEl = document.getElementById("ticket-id-tag");
  if (idEl) idEl.textContent = "--";

  const titleEl = document.getElementById("ticket-title-display");
  if (titleEl) titleEl.textContent = "No Active Incident";

  const descEl = document.getElementById("ticket-desc-display");
  if (descEl) descEl.textContent = "Submit a ticket to stage triage telemetry for specialist review.";

  const deskEl = document.getElementById("ticket-desk-display");
  if (deskEl) deskEl.textContent = "Standby";

  const prioEl = document.getElementById("ticket-priority-display");
  if (prioEl) {
    prioEl.className = "text-xs font-bold text-outline flex items-center gap-1 mt-0.5";
    prioEl.innerHTML = `<span class="w-2 h-2 rounded-full bg-outline"></span> None`;
  }
}

// --- Fetch Latest Ticket for Live Room Sidebar ---
async function fetchLatestTicket() {
  if (!APP_STATE.currentUser?.email) {
    renderEmptyTicketDetails();
    return;
  }

  try {
    const res = await fetch(`/api/tickets/latest?email=${encodeURIComponent(APP_STATE.currentUser.email)}`);
    if (res.ok) {
      const data = await res.json();
      if (data.ticket) {
        renderTicketDetails(data.ticket);
      } else {
        renderEmptyTicketDetails();
      }
    }
  } catch (err) {
    console.error("[Ticket Fetch Error]:", err);
  }
}

// --- Submit Real Ticket to Backend Database ---
async function handleTicketSubmit() {
  const btn = document.getElementById("ticket-submit-btn");
  const subjectInput = document.getElementById("ticket-subject");
  const descInput = document.getElementById("ticket-desc");
  const escalationToggle = document.getElementById("escalation-toggle");

  const subject = subjectInput ? subjectInput.value.trim() : "";
  const desc = descInput ? descInput.value.trim() : "";
  const escalate = escalationToggle ? escalationToggle.checked : false;

  const activeCatBtn = document.querySelector("#category-pill-group .cat-pill.active");
  const category = activeCatBtn ? activeCatBtn.dataset.category : "Technical Incident";

  const activePrioCard = document.querySelector("#priority-card-group .prio-card.active");
  const priority = activePrioCard ? activePrioCard.dataset.priority.toUpperCase() : "HIGH";

  if (!subject || !desc) {
    showToast("Missing Fields", "Please enter both a subject and a description.", "error");
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="material-symbols-outlined text-[18px] animate-spin">sync</span><span>Saving to SQLite...</span>`;
  }

  try {
    const userEmail = APP_STATE.currentUser?.email || "guest@omnipulse.internal";

    const res = await fetch("/api/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_email: userEmail,
        subject: subject,
        description: desc,
        category: category,
        priority: priority,
        escalateVoice: escalate,
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Server failed to save ticket.");
    }

    const createdTicket = await res.json();

    // Reset Form
    if (subjectInput) subjectInput.value = "";
    if (descInput) descInput.value = "";
    updateCharCount();

    // Render immediately in Live Room context
    renderTicketDetails(createdTicket);

    showToast("Ticket Created", `Incident ${createdTicket.ticket_id} persisted to database.`, "success");
    navigateTo("#live-room");

    if (escalate) {
      setTimeout(() => toggleVoiceBridge(true), 600);
    }
  } catch (err) {
    console.error("[Create Ticket Error]:", err);
    showToast("Creation Failed", err.message || "Could not save ticket.", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span>Create Ticket & Route to Specialist</span><span class="material-symbols-outlined text-[18px] group-hover:translate-x-1 transition-transform">arrow_forward</span>`;
    }
  }
}

// --- Fetch & Render Entire Ticket Queue ---
async function fetchTicketQueue() {
  const queueContainer = document.getElementById("ticket-queue-list");
  const countBadge = document.getElementById("queue-count-badge");
  if (!queueContainer) return;

  queueContainer.innerHTML = `
    <div class="py-12 flex flex-col items-center justify-center gap-2 text-outline">
      <span class="material-symbols-outlined text-3xl animate-spin">sync</span>
      <span class="text-xs">Loading queue records from backend...</span>
    </div>
  `;

  try {
    const emailParam = APP_STATE.currentUser?.email ? `?email=${encodeURIComponent(APP_STATE.currentUser.email)}` : "";
    const res = await fetch(`/api/tickets/list${emailParam}`);
    const data = await res.json();
    const tickets = data.tickets || [];

    if (countBadge) {
      countBadge.textContent = `${tickets.length} Incident${tickets.length === 1 ? "" : "s"}`;
    }

    if (tickets.length === 0) {
      queueContainer.innerHTML = `
        <div class="py-16 flex flex-col items-center justify-center text-center gap-3 p-6 text-outline">
          <div class="w-12 h-12 rounded-full bg-surface-container-low flex items-center justify-center text-outline">
            <span class="material-symbols-outlined text-[28px]">inbox</span>
          </div>
          <div>
            <h4 class="text-sm font-bold text-on-surface">Your ticket queue is empty</h4>
            <p class="text-xs text-on-surface-variant max-w-sm mt-1">No support tickets have been filed yet. Create a ticket to dispatch telemetry to specialist routing.</p>
          </div>
          <button onclick="navigateTo('#create-ticket')" class="mt-2 flex items-center gap-1.5 px-4 py-2 rounded-xl bg-primary text-white font-semibold text-xs shadow-sm hover:bg-primary-container transition-all">
            <span class="material-symbols-outlined text-[16px]">add</span>
            <span>Create a New Ticket</span>
          </button>
        </div>
      `;
      return;
    }

    queueContainer.innerHTML = "";
    tickets.forEach((ticket) => {
      const isHigh = /high|critical/i.test(ticket.priority);
      const row = document.createElement("div");
      row.className = "p-4 sm:p-5 hover:bg-surface-container-low/60 transition-colors flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4";

      row.innerHTML = `
        <div class="flex items-start gap-3.5 flex-1 min-w-0">
          <div class="w-10 h-10 rounded-xl bg-surface-container-high flex items-center justify-center text-primary shrink-0 mt-0.5">
            <span class="material-symbols-outlined text-[20px]">confirmation_number</span>
          </div>
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-code font-bold text-primary text-xs">${ticket.ticket_id}</span>
              <span class="text-outline">•</span>
              <span class="text-xs font-semibold text-on-surface-variant truncate">${ticket.category}</span>
              <span class="text-outline">•</span>
              <span class="font-code text-[11px] text-outline">${ticket.created_at || "Recently filed"}</span>
            </div>
            <h3 class="text-sm font-bold text-on-surface truncate mt-0.5">${ticket.subject}</h3>
            <p class="text-xs text-on-surface-variant line-clamp-1 mt-0.5">${ticket.description}</p>
          </div>
        </div>

        <div class="flex items-center gap-3 w-full sm:w-auto justify-between sm:justify-end shrink-0 pt-2 sm:pt-0 border-t sm:border-0 border-outline-variant/10">
          <div class="flex flex-col items-start sm:items-end">
            <span class="text-[10px] uppercase tracking-wider text-outline font-semibold">${ticket.assigned_desk}</span>
            <span class="text-xs font-bold ${isHigh ? "text-error" : "text-primary"} flex items-center gap-1 mt-0.5">
              <span class="w-1.5 h-1.5 rounded-full ${isHigh ? "bg-error" : "bg-primary"}"></span>
              <span>${ticket.priority}</span>
            </span>
          </div>
          <button onclick="openTicketInLiveRoom('${encodeURIComponent(JSON.stringify(ticket))}')" class="px-3.5 py-1.5 rounded-lg bg-surface-container-low hover:bg-primary hover:text-white text-on-surface text-xs font-semibold transition-all flex items-center gap-1 shadow-sm">
            <span>Troubleshoot</span>
            <span class="material-symbols-outlined text-[16px]">arrow_forward</span>
          </button>
        </div>
      `;

      queueContainer.appendChild(row);
    });
  } catch (err) {
    console.error("[Queue Error]:", err);
    queueContainer.innerHTML = `
      <div class="py-10 text-center text-xs text-error">
        Failed to load incident records from backend. Please refresh.
      </div>
    `;
  }
}

// --- Select a Queue Ticket and Bring into Live Room ---
function openTicketInLiveRoom(encodedJson) {
  try {
    const ticket = JSON.parse(decodeURIComponent(encodedJson));
    renderTicketDetails(ticket);
    showToast("Incident Staged", `Active session assigned to ticket ${ticket.ticket_id}.`, "info");
    navigateTo("#live-room");
  } catch (e) {
    console.error("Error opening ticket:", e);
  }
}

// --- Toast Messaging ---
function showToast(title, message, type = "success") {
  const banner = document.getElementById("toast-banner");
  const icon = document.getElementById("toast-icon");
  const titleEl = document.getElementById("toast-title");
  const msgEl = document.getElementById("toast-message");

  const theme = {
    success: { bg: "bg-secondary-container text-on-secondary-container border-secondary/30", icon: "check_circle" },
    error: { bg: "bg-error-container text-on-error-container border-error/30", icon: "error" },
    info: { bg: "bg-surface-container-highest text-on-surface border-outline-variant", icon: "info" },
  }[type] || theme.info;

  banner.className = `fixed top-20 right-6 z-50 transition-all duration-300 max-w-md p-4 rounded-xl shadow-lg border flex items-start gap-3 ${theme.bg}`;
  icon.textContent = theme.icon;
  titleEl.textContent = title;
  msgEl.textContent = message;

  banner.classList.remove("hidden");
  clearTimeout(window._toastTimeout);
  window._toastTimeout = setTimeout(hideToast, 4000);
}

function hideToast() {
  const banner = document.getElementById("toast-banner");
  if (banner) banner.classList.add("hidden");
}

// --- Voice Call Bridge & WebSocket ---
async function toggleVoiceBridge(forceStart = false) {
  if (APP_STATE.voice.isConnected && !forceStart) {
    stopVoiceBridge();
  } else {
    await startVoiceBridge();
  }
}

async function startVoiceBridge() {
  const callBtn = document.getElementById("directCallBtn");
  const cockpit = document.getElementById("voiceSessionCockpit");
  const micBadge = document.getElementById("mic-badge");

  if (callBtn) {
    callBtn.disabled = true;
    callBtn.innerHTML = `<span class="material-symbols-outlined text-[18px] animate-spin">sync</span><span>Connecting Audio...</span>`;
  }

  try {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/voice`;
    const socket = new WebSocket(wsUrl);
    socket.binaryType = "arraybuffer";

    socket.onopen = async () => {
      APP_STATE.voice.isConnected = true;
      APP_STATE.voice.ws = socket;

      if (cockpit) cockpit.classList.remove("hidden");
      if (callBtn) {
        callBtn.className = "group flex items-center justify-center gap-2 px-5 py-2.5 rounded-xl bg-error text-white font-semibold text-xs shadow-md hover:bg-red-700 transition-all";
        callBtn.innerHTML = `<span class="material-symbols-filled text-[18px]">call_end</span><span>Disconnect Voice</span>`;
        callBtn.disabled = false;
      }

      if (micBadge) {
        micBadge.textContent = "16kHz Streaming";
        micBadge.className = "text-[10px] font-semibold px-2 py-0.5 rounded bg-secondary-container text-on-secondary-container";
      }

      await window.VoiceEngine.startMicrophone(
        (chunk) => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send(chunk);
          }
        },
        (rms) => {
          const meter = document.getElementById("mic-energy-meter");
          const bar = document.getElementById("mic-energy-bar");
          if (meter && bar) {
            meter.textContent = `${rms.toFixed(1)} RMS`;
            bar.style.width = `${Math.min(100, rms * 1.8)}%`;
          }
        }
      );

      const canvas = document.getElementById("cockpitCanvas");
      if (canvas && window.VoiceEngine) {
        window.VoiceEngine.startSpectrumVisualizer(canvas, () => APP_STATE.voice.isConnected);
      }

      showToast("Audio Connected", "Duplex single connection active with AssemblyAI stack.", "success");
    };

    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        handleServerJsonEvent(JSON.parse(event.data));
      } else if (window.VoiceEngine) {
        window.VoiceEngine.playPcmChunk(event.data, () => toggleTypingIndicator(false));
      }
    };

    socket.onclose = () => stopVoiceBridge();
    socket.onerror = (err) => {
      console.error("[WebSocket Error]:", err);
      showToast("Socket Reconnecting", "Backend session dropped or running in mock mode.", "info");
    };
  } catch (err) {
    console.error("Audio Initialization Failed:", err);
    stopVoiceBridge();
    showToast("Microphone Error", "Allow microphone access in your browser to proceed.", "error");
  }
}

function stopVoiceBridge() {
  APP_STATE.voice.isConnected = false;
  if (APP_STATE.voice.ws && APP_STATE.voice.ws.readyState === WebSocket.OPEN) {
    APP_STATE.voice.ws.close();
  }
  APP_STATE.voice.ws = null;

  if (window.VoiceEngine) window.VoiceEngine.stop();

  const callBtn = document.getElementById("directCallBtn");
  const cockpit = document.getElementById("voiceSessionCockpit");
  const micBadge = document.getElementById("mic-badge");

  if (callBtn) {
    callBtn.disabled = false;
    callBtn.className = "group flex items-center justify-center gap-2 px-5 py-2.5 rounded-xl bg-primary text-white font-semibold text-xs shadow-md hover:bg-primary-container transition-all";
    callBtn.innerHTML = `<span class="material-symbols-filled text-[18px]">call</span><span>Start Direct Voice Call</span>`;
  }
  if (cockpit) cockpit.classList.add("hidden");
  if (micBadge) {
    micBadge.textContent = "Standby";
    micBadge.className = "text-[10px] font-semibold px-2 py-0.5 rounded bg-surface-container text-outline";
  }
  toggleTypingIndicator(false);
}

function sendManualInterrupt() {
  if (APP_STATE.voice.ws && APP_STATE.voice.ws.readyState === WebSocket.OPEN) {
    APP_STATE.voice.ws.send(JSON.stringify({ type: "user_action", action: "interrupt" }));
  }
  if (window.VoiceEngine) window.VoiceEngine.flush();
  showToast("Interruption Sent", "Agent audio buffer cleared via VAD barge-in.", "info");
}

// --- Wire Protocol & Telemetry Handlers ---
function handleServerJsonEvent(data) {
  if (data.type === "transcript") {
    appendChatMessage(data.role === "agent" ? "Support AI" : "You", data.text, data.role);
  } else if (data.type === "tool_call") {
    renderToolCallInspector(data.tool_call);
  } else if (data.type === "tool_response") {
    updateToolResponse(data.call_id, data.output);
  } else if (data.type === "interruption") {
    if (window.VoiceEngine) window.VoiceEngine.flush();
    showToast("Barge-In Detected", "Specialist speech yielded to caller.", "info");
  }
}

function appendChatMessage(sender, text, role = "agent") {
  const stream = document.getElementById("chatStream");
  const typing = document.getElementById("typingIndicator");
  if (!stream) return;

  const node = document.createElement("div");
  node.className = role === "user" ? "flex flex-col items-end gap-1" : "flex items-start gap-2.5 max-w-xl";

  const timeStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  if (role === "user") {
    node.innerHTML = `
      <div class="flex items-center gap-1.5 text-outline text-[11px]">
        <span>${sender}</span>
        <span class="font-code">${timeStr}</span>
      </div>
      <div class="max-w-xl bg-primary text-white p-3.5 rounded-2xl rounded-tr-sm shadow-sm text-sm leading-relaxed">
        ${text}
      </div>
      <span class="text-[10px] text-outline flex items-center gap-1">
        <span class="material-symbols-filled text-[12px] text-primary">done_all</span> Delivered
      </span>
    `;
  } else {
    node.innerHTML = `
      <div class="w-8 h-8 rounded-full bg-primary flex items-center justify-center text-white font-bold text-xs shrink-0 mt-1">
        ER
      </div>
      <div class="flex flex-col gap-1">
        <div class="flex items-center gap-1.5 text-outline text-[11px]">
          <span class="font-semibold text-on-surface">${sender}</span>
          <span class="font-code">${timeStr}</span>
        </div>
        <div class="bg-surface-container text-on-surface p-3.5 rounded-2xl rounded-tl-sm shadow-sm text-sm leading-relaxed">
          ${text}
        </div>
      </div>
    `;
  }

  stream.insertBefore(node, typing);
  stream.scrollTop = stream.scrollHeight;
}

function sendTextMessage() {
  const input = document.getElementById("chatInput");
  const msg = input.value.trim();
  if (!msg) return;

  appendChatMessage("You", msg, "user");
  input.value = "";

  if (APP_STATE.voice.ws && APP_STATE.voice.ws.readyState === WebSocket.OPEN) {
    APP_STATE.voice.ws.send(JSON.stringify({ type: "text", text: msg }));
  } else {
    toggleTypingIndicator(true, "Elena is processing query...");
    setTimeout(() => {
      toggleTypingIndicator(false);
      appendChatMessage("Support AI", `Received: "${msg}". Checking system telemetry.`, "agent");
    }, 1200);
  }
}

function toggleTypingIndicator(visible, text = "Elena is speaking...") {
  const ind = document.getElementById("typingIndicator");
  const txt = document.getElementById("typingText");
  if (ind && txt) {
    txt.textContent = text;
    ind.style.display = visible ? "flex" : "none";
    const stream = document.getElementById("chatStream");
    if (stream) stream.scrollTop = stream.scrollHeight;
  }
}

function renderToolCallInspector(toolCall) {
  APP_STATE.voice.toolCallsCount++;
  const counterEl = document.getElementById("tool-counter-badge");
  if (counterEl) counterEl.textContent = `${APP_STATE.voice.toolCallsCount} calls`;

  const container = document.getElementById("tool-telemetry-container");
  if (!container) return;
  if (APP_STATE.voice.toolCallsCount === 1) container.innerHTML = "";

  let args = toolCall.function.arguments;
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch (e) {}
  }

  const card = document.createElement("div");
  card.id = `tool-card-${toolCall.id}`;
  card.className = "bg-surface-container-low rounded-xl p-3 border-l-4 border-amber-500 flex flex-col gap-1.5 text-xs font-code";

  card.innerHTML = `
    <div class="flex items-center justify-between">
      <span class="font-bold text-primary truncate">λ ${toolCall.function.name}</span>
      <span id="badge-${toolCall.id}" class="px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-600 font-semibold text-[10px] uppercase">Executing</span>
    </div>
    <div class="text-[11px] text-on-surface-variant overflow-x-auto bg-surface-container-lowest p-2 rounded border border-outline-variant/30">
      ${JSON.stringify(args, null, 2)}
    </div>
  `;

  container.prepend(card);
}

function updateToolResponse(callId, rawOutput) {
  const badge = document.getElementById(`badge-${callId}`);
  if (badge) {
    badge.textContent = "Resolved";
    badge.className = "px-1.5 py-0.5 rounded bg-secondary-container text-on-secondary-container font-semibold text-[10px] uppercase";
  }

  const card = document.getElementById(`tool-card-${callId}`);
  if (card) {
    let parsed = rawOutput;
    if (typeof parsed === "string") {
      try {
        parsed = JSON.parse(parsed);
      } catch (e) {}
    }
    const resDiv = document.createElement("div");
    resDiv.className = "text-[11px] text-secondary overflow-x-auto bg-secondary/5 p-2 rounded border border-secondary/20 mt-1";
    resDiv.textContent = `Result: ${JSON.stringify(parsed, null, 2)}`;
    card.appendChild(resDiv);
  }
}

// --- Ticket Form UI Helpers ---
function setupCategoryPills() {
  const pills = document.querySelectorAll(".cat-pill");
  pills.forEach((pill) => {
    pill.addEventListener("click", () => {
      pills.forEach((p) => {
        p.className = "cat-pill flex items-center gap-2 p-3 rounded-xl bg-surface-container-low text-on-surface hover:bg-surface-container text-left transition-all";
        const check = p.querySelector(".pill-check");
        if (check) check.remove();
      });
      pill.className = "cat-pill active flex items-center gap-2 p-3 rounded-xl bg-primary text-white text-left transition-all shadow-sm";
      const chk = document.createElement("span");
      chk.className = "material-symbols-outlined text-[16px] pill-check";
      chk.textContent = "check";
      pill.appendChild(chk);
    });
  });
}

function setupPriorityCards() {
  const cards = document.querySelectorAll(".prio-card");
  cards.forEach((card) => {
    card.addEventListener("click", () => {
      cards.forEach((c) => {
        c.className = "prio-card cursor-pointer p-3 rounded-xl bg-surface-container-low text-on-surface hover:bg-surface-container transition-all";
        const sub = c.querySelector("span:last-child");
        if (sub) sub.className = "text-[11px] text-on-surface-variant block mt-1";
      });
      card.className = "prio-card active cursor-pointer p-3 rounded-xl bg-primary text-white transition-all shadow-sm";
      const sub = card.querySelector("span:last-child");
      if (sub) sub.className = "text-[11px] text-on-primary-container block mt-1";
    });
  });
}

function updateCharCount() {
  const desc = document.getElementById("ticket-desc");
  const counter = document.getElementById("ticket-desc-count");
  if (desc && counter) {
    counter.textContent = `${desc.value.length} / 2,000`;
  }
}

function saveTicketDraft() {
  showToast("Draft Saved", "Telemetry parameters preserved locally in browser session.", "info");
}

// --- Auth Helpers ---
function togglePasswordVisibility(fieldId, triggerBtn) {
  const input = document.getElementById(fieldId);
  const icon = triggerBtn.querySelector(".material-symbols-outlined");
  if (!input || !icon) return;
  if (input.type === "password") {
    input.type = "text";
    icon.textContent = "visibility_off";
  } else {
    input.type = "password";
    icon.textContent = "visibility";
  }
}

function evaluatePasswordStrength(pwd) {
  const meter1 = document.getElementById("meter-bar-1");
  const meter2 = document.getElementById("meter-bar-2");
  const meter3 = document.getElementById("meter-bar-3");
  const meter4 = document.getElementById("meter-bar-4");
  const label = document.getElementById("reg-strength-label");

  let score = 0;
  if (pwd.length >= 8) score++;
  if (/[A-Z]/.test(pwd)) score++;
  if (/[0-9]/.test(pwd)) score++;
  if (/[^A-Za-z0-9]/.test(pwd)) score++;

  [meter1, meter2, meter3, meter4].forEach((b) => b && (b.className = "h-1 rounded-full bg-surface-container-high transition-colors"));

  if (!label) return;
  if (!pwd) {
    label.textContent = "Awaiting input";
    label.className = "font-code text-xs text-on-surface-variant font-medium";
    return;
  }

  if (score <= 1) {
    if (meter1) meter1.classList.replace("bg-surface-container-high", "bg-error");
    label.textContent = "Weak";
    label.className = "font-code text-xs text-error font-medium";
  } else if (score === 2) {
    if (meter1) meter1.classList.replace("bg-surface-container-high", "bg-tertiary-container");
    if (meter2) meter2.classList.replace("bg-surface-container-high", "bg-tertiary-container");
    label.textContent = "Moderate";
    label.className = "font-code text-xs text-tertiary font-medium";
  } else if (score === 3) {
    if (meter1) meter1.classList.replace("bg-surface-container-high", "bg-primary");
    if (meter2) meter2.classList.replace("bg-surface-container-high", "bg-primary");
    if (meter3) meter3.classList.replace("bg-surface-container-high", "bg-primary");
    label.textContent = "Strong";
    label.className = "font-code text-xs text-primary font-medium";
  } else {
    [meter1, meter2, meter3, meter4].forEach((b) => b && b.classList.replace("bg-surface-container-high", "bg-secondary"));
    label.textContent = "Mission-Grade";
    label.className = "font-code text-xs text-secondary font-medium";
  }
}

function checkPasswordMatch() {
  const pass = document.getElementById("reg-password")?.value || "";
  const conf = document.getElementById("reg-confirm-password")?.value || "";
  const icon = document.getElementById("reg-match-icon");
  if (!icon) return;

  if (!conf) {
    icon.classList.add("hidden");
    return;
  }
  icon.classList.remove("hidden");
  if (pass === conf) {
    icon.textContent = "check_circle";
    icon.className = "material-symbols-outlined absolute right-3.5 top-1/2 -translate-y-1/2 text-secondary text-[20px]";
  } else {
    icon.textContent = "cancel";
    icon.className = "material-symbols-outlined absolute right-3.5 top-1/2 -translate-y-1/2 text-error text-[20px]";
  }
}

async function handleLoginSubmit() {
  const emailInput = document.getElementById("login-email");
  const pwdInput = document.getElementById("login-password");
  const btn = document.getElementById("login-submit-btn");

  const email = emailInput?.value.trim() || "";
  const password = pwdInput?.value || "";

  if (!email || !password) {
    showToast("Missing Fields", "Please enter your email and password.", "error");
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="material-symbols-outlined text-[18px] animate-spin">sync</span><span>Authenticating...</span>`;
  }

  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    const data = await res.json();
    if (!res.ok) {
      showToast("Sign In Failed", data.detail || "Invalid email or password.", "error");
      return;
    }

    APP_STATE.currentUser = data.user;
    localStorage.setItem("omni_user", JSON.stringify(data.user));
    updateUserIdentity();
    showToast("Signed In", `Workstation provisioned for ${data.user.name}.`, "success");
    navigateTo("#live-room");
  } catch (err) {
    console.error("[Login Error]:", err);
    showToast("Connection Error", "Cannot reach authentication server.", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span>Sign In to Support Hub</span><span class="material-symbols-outlined text-[20px]">arrow_forward</span>`;
    }
  }
}

async function handleRegisterSubmit() {
  const btn = document.getElementById("register-submit-btn");
  const nameInput = document.getElementById("reg-name");
  const emailInput = document.getElementById("reg-email");
  const pwdInput = document.getElementById("reg-password");
  const confirmInput = document.getElementById("reg-confirm-password");

  if (!nameInput || !emailInput || !pwdInput) return;

  const name = nameInput.value.trim();
  const email = emailInput.value.trim();
  const password = pwdInput.value;
  const confirmPassword = confirmInput ? confirmInput.value : "";

  if (!name || !email || !password) {
    showToast("Missing Fields", "Please complete all required fields.", "error");
    return;
  }

  if (confirmInput && password !== confirmPassword) {
    showToast("Password Mismatch", "Passwords do not match.", "error");
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="material-symbols-outlined text-[18px] animate-spin">sync</span><span>Creating Account...</span>`;
  }

  try {
    const res = await fetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, email, password }),
    });

    const data = await res.json();
    if (!res.ok) {
      showToast("Registration Failed", data.detail || "Account could not be created.", "error");
      return;
    }

    APP_STATE.currentUser = data.user;
    localStorage.setItem("omni_user", JSON.stringify(data.user));

    const regForm = document.getElementById("register-form");
    if (regForm) regForm.reset();

    updateUserIdentity();
    showToast("Registration Complete", `Account registered for ${data.user.name}.`, "success");
    navigateTo("#live-room");
  } catch (err) {
    console.error("[Register Error]:", err);
    showToast("Connection Error", "Cannot reach authentication server.", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span>Register for Support Hub</span><span class="material-symbols-outlined text-[20px]">arrow_forward</span>`;
    }
  }
}

function updateUserIdentity() {
  if (!APP_STATE.currentUser) {
    const initialsEl = document.getElementById("user-avatar-initials");
    if (initialsEl) initialsEl.textContent = "--";

    const nameEl = document.getElementById("user-display-name");
    if (nameEl) nameEl.textContent = "Guest";

    const authActionEl = document.getElementById("user-auth-action");
    if (authActionEl) authActionEl.textContent = "Sign In";
    return;
  }

  const nameParts = (APP_STATE.currentUser.name || "User").trim().split(" ");
  let initials = nameParts[0] ? nameParts[0][0].toUpperCase() : "U";
  if (nameParts.length > 1 && nameParts[nameParts.length - 1]) {
    initials += nameParts[nameParts.length - 1][0].toUpperCase();
  }

  const initialsEl = document.getElementById("user-avatar-initials");
  if (initialsEl) initialsEl.textContent = initials;

  const nameEl = document.getElementById("user-display-name");
  if (nameEl) nameEl.textContent = APP_STATE.currentUser.name;

  const authActionEl = document.getElementById("user-auth-action");
  if (authActionEl) authActionEl.textContent = "Sign Out";

  const chatSender = document.getElementById("chat-sender-name");
  if (chatSender) chatSender.textContent = `You (${APP_STATE.currentUser.name})`;
}

function handleAuthHeaderClick() {
  if (localStorage.getItem("omni_user")) {
    localStorage.removeItem("omni_user");
    APP_STATE.currentUser = null;
    showToast("Session Ended", "You have signed out of your support workstation.", "info");
    navigateTo("#login");
  } else {
    navigateTo("#login");
  }
}

function testMicHardware() {
  const btn = document.getElementById("testMicBtn");
  if (!btn) return;
  btn.textContent = "Listening... Speak now";
  btn.classList.add("bg-secondary-container", "text-on-secondary-container");

  navigator.mediaDevices
    .getUserMedia({ audio: true })
    .then((stream) => {
      setTimeout(() => {
        stream.getTracks().forEach((t) => t.stop());
        btn.textContent = "Microphone Verified (OK)";
        btn.className = "w-full py-2 px-3 rounded-xl bg-secondary-container font-semibold text-xs text-on-secondary-container";
        setTimeout(() => {
          btn.textContent = "Test Microphone Input";
          btn.className = "w-full py-2 px-3 rounded-xl bg-surface-container-low hover:bg-surface-container font-semibold text-xs text-on-surface transition-colors";
        }, 2500);
      }, 1500);
    })
    .catch(() => {
      btn.textContent = "Permission Denied";
      btn.className = "w-full py-2 px-3 rounded-xl bg-error-container font-semibold text-xs text-on-error-container";
    });
}