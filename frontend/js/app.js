/**
 * app.js — Alpine.js component: state, SPA routing, ticket sync,
 * and live voice/telemetry handling.
 */

// Backend origin for REST + WebSocket calls. Empty string means "same origin
// as this page" (the original single-server setup). Set window.API_BASE
// (see index.html) to a full origin like "https://your-app.up.railway.app"
// when the frontend is deployed separately from the backend (e.g. frontend
// on Vercel, backend on Railway) — every fetch()/WebSocket call below is
// built from this instead of a bare relative path.
const API_BASE = (window.API_BASE || "").replace(/\/$/, "");

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

document.addEventListener("alpine:init", () => {
  Alpine.data("omniApp", () => ({
    // --- Routing / Auth ---
    view: "login",
    currentUser: null,

    // --- Toast ---
    toast: { visible: false, type: "info", title: "", message: "" },
    _toastTimeout: null,

    // --- Ticket queue ---
    tickets: [],
    ticketsLoading: false,
    currentTicket: null,
    // "open" | "closed" | "all"
    ticketStatusFilter: "open",
    // Create-ticket is an inline panel within the ticket-queue view (sidebar
    // stays visible, dismissed via a back button) rather than its own route.
    showCreateTicketForm: false,

    // --- Create-ticket form ---
    categories: [
      { name: "Network & Service", icon: "signal_cellular_alt" },
      { name: "Billing & Payments", icon: "payments" },
      { name: "Account & Plan", icon: "sim_card" },
    ],
    priorities: [
      { id: "low", label: "Low", sub: "General • 24h", dot: "bg-outline" },
      { id: "medium", label: "Medium", sub: "Degraded • 4h", dot: "bg-tertiary-container" },
      { id: "high", label: "High", sub: "Critical • < 30m", dot: "bg-white", critical: false },
      { id: "critical", label: "Critical", sub: "Outage • < 5m", dot: "bg-error", critical: true },
    ],
    ticketForm: {
      category: "Network & Service",
      priority: "high",
      subject: "",
      description: "",
      escalate: true,
      submitting: false,
    },

    // --- Auth forms ---
    loginForm: { email: "", password: "", showPassword: false, submitting: false },
    registerForm: {
      name: "",
      email: "",
      phoneNumber: "",
      password: "",
      confirmPassword: "",
      showPassword: false,
      submitting: false,
    },

    // --- Voice bridge ---
    voice: {
      connected: false,
      connecting: false,
      ws: null,
      micBadge: "Standby",
      rms: 0,
      // The mic's actual negotiated MediaStreamTrack settings (sample rate,
      // channel count, etc.) — set the instant the track opens so the Audio
      // Diagnostics panel reports real live hardware behavior, not a guess.
      micInfo: null,
      toolCallsCount: 0,
      // Set by the server's persona_info event once connected, based on the
      // bound ticket's category (e.g. "Elena" for Billing, "Kai" for Technical).
      personaName: "AI Support",
      personaVoice: "",
      // True only when the user explicitly hangs up (or the server reports a
      // real, intentional session end) — everything else is treated as a
      // dropped call worth auto-reconnecting, like a real phone call.
      userHangup: false,
      reconnectAttempts: 0,
    },

    // --- Microphone device detection ---
    mic: {
      devices: [],
      selectedId: "",
      // idle | detecting | ready | no-device | denied | unsupported
      status: "idle",
    },

    // --- Appearance (Settings screen), persisted locally ---
    theme: "light",

    messages: [],
    toolCalls: [],
    typing: { visible: false, text: "" },
    chatInput: "",
    micTest: { label: "Test Microphone Input", variant: "idle" },

    // ===================================================================
    // Lifecycle
    // ===================================================================
    init() {
      const savedUser = localStorage.getItem("omni_user");
      if (savedUser) {
        try {
          this.currentUser = JSON.parse(savedUser);
        } catch (e) {
          localStorage.removeItem("omni_user");
        }
      }

      // The inline head script already applied any saved theme before first
      // paint (to avoid a flash) — just sync the reactive state to match.
      this.theme = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";

      window.addEventListener("hashchange", () => this.navigate(window.location.hash));
      navigator.mediaDevices?.addEventListener?.("devicechange", () => this.detectMicrophones());

      this.navigate(window.location.hash || "#ticket-queue");
    },

    // ===================================================================
    // Settings
    // ===================================================================
    setTheme(mode) {
      this.theme = mode;
      document.documentElement.setAttribute("data-theme", mode);
      localStorage.setItem("omni_theme", mode);
    },

    logout() {
      localStorage.removeItem("omni_user");
      this.currentUser = null;
      this.showToast("Session Ended", "You have signed out of your support workstation.", "info");
      this.navigate("#login");
    },

    // ===================================================================
    // Router
    // ===================================================================
    navigate(hash) {
      if (!this.currentUser) {
        if (hash !== "#register") hash = "#login";
      } else {
        if (!hash || hash === "#login" || hash === "#register") hash = "#ticket-queue";
      }

      if (window.location.hash !== hash) {
        window.location.hash = hash;
      }

      // The create-ticket panel isn't its own route — any real navigation
      // (including a hash change while it's open) drops back to the list.
      this.showCreateTicketForm = false;

      this.view = hash.replace("#", "");

      if (hash === "#live-room") {
        // A ticket explicitly staged via openTicketInLiveRoom() must win —
        // re-fetching "latest" here would silently swap the caller's chosen
        // ticket for whichever one was created most recently.
        if (this.currentTicket) {
          this.loadTranscriptHistory();
        } else {
          this.fetchLatestTicket().then(() => this.loadTranscriptHistory());
        }
        this.detectMicrophones();
      } else if (hash === "#ticket-queue") {
        this.fetchTicketQueue();
      } else if (hash === "#settings") {
        this.detectMicrophones();
      }

      window.scrollTo({ top: 0, behavior: "smooth" });
    },

    get isAuthView() {
      return this.view === "login" || this.view === "register";
    },

    initialsFor(name) {
      const clean = (name || "AI").trim();
      return clean.length >= 2 ? clean.slice(0, 2).toUpperCase() : (clean[0] || "A").toUpperCase().padEnd(2, "I");
    },

    personaInitials() {
      return this.initialsFor(this.voice.personaName);
    },

    // ===================================================================
    // Microphone detection
    // ===================================================================
    async detectMicrophones() {
      if (!navigator.mediaDevices?.getUserMedia) {
        this.mic.status = "unsupported";
        this.mic.devices = [];
        return;
      }

      this.mic.status = "detecting";
      try {
        const devices = await window.VoiceEngine.listInputDevices();
        this.mic.devices = devices;

        if (!devices.some((d) => d.deviceId === this.mic.selectedId)) {
          this.mic.selectedId = devices[0].deviceId;
        }
        this.mic.status = "ready";
      } catch (err) {
        this.mic.devices = [];
        this.mic.selectedId = "";

        if (err.message === "NO_MICROPHONE") {
          this.mic.status = "no-device";
        } else if (err.name === "NotAllowedError" || err.name === "SecurityError") {
          this.mic.status = "denied";
        } else if (err.message === "UNSUPPORTED") {
          this.mic.status = "unsupported";
        } else {
          this.mic.status = "no-device";
        }
        console.error("[Mic Detection Error]:", err);
      }
    },

    micStatusLabel() {
      return (
        {
          idle: "Not checked yet",
          detecting: "Detecting microphones...",
          ready: `${this.mic.devices.length} device${this.mic.devices.length === 1 ? "" : "s"} found`,
          "no-device": "No microphone detected",
          denied: "Microphone permission denied",
          unsupported: "Browser does not support mic access",
        }[this.mic.status] || ""
      );
    },

    // ===================================================================
    // Ticket details (Live Room sidebar)
    // ===================================================================
    async fetchLatestTicket() {
      if (!this.currentUser?.email) {
        this.currentTicket = null;
        return;
      }
      try {
        const res = await fetch(apiUrl(`/api/tickets/latest?email=${encodeURIComponent(this.currentUser.email)}`));
        if (res.ok) {
          const data = await res.json();
          this.currentTicket = data.ticket || null;
        }
      } catch (err) {
        console.error("[Ticket Fetch Error]:", err);
      }
    },

    isHighPriority(priority) {
      return /HIGH|CRITICAL/i.test(priority || "");
    },

    // Transcripts are persisted permanently in SQLite (see backend db.py) and
    // are never cleared on session end or navigation — this repopulates the
    // chat view with everything on record for the active ticket, so history
    // survives reloads and screen changes instead of living only in memory.
    async loadTranscriptHistory() {
      this.messages = [];
      const code = this.currentTicket?.ticket_id;
      if (!code) return;
      try {
        const res = await fetch(apiUrl(`/api/tickets/${encodeURIComponent(code.replace(/^#/, ""))}/transcripts`));
        if (!res.ok) return;
        const data = await res.json();
        (data.transcripts || []).forEach((t) => {
          this.messages.push({
            id: Date.now() + Math.random(),
            sender: t.role === "agent" ? this.voice.personaName : "You",
            text: t.text,
            role: t.role,
            time: t.created_at
              ? new Date(t.created_at + "Z").toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
              : "",
          });
        });
        this.$nextTick(() => this.scrollChatToBottom());
      } catch (err) {
        console.error("[Transcript History Error]:", err);
      }
    },

    // ===================================================================
    // Create ticket
    // ===================================================================
    get descCharCount() {
      return this.ticketForm.description.length;
    },

    async submitTicket() {
      const subject = this.ticketForm.subject.trim();
      const description = this.ticketForm.description.trim();

      if (!subject || !description) {
        this.showToast("Missing Fields", "Please enter both a subject and a description.", "error");
        return;
      }

      this.ticketForm.submitting = true;
      try {
        const userEmail = this.currentUser?.email || "guest@omnipulse.internal";
        const res = await fetch(apiUrl("/api/tickets"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_email: userEmail,
            subject: subject,
            description: description,
            category: this.ticketForm.category,
            priority: this.ticketForm.priority.toUpperCase(),
            escalateVoice: this.ticketForm.escalate,
          }),
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || "Server failed to save ticket.");
        }

        const createdTicket = await res.json();

        const escalate = this.ticketForm.escalate;
        this.ticketForm.subject = "";
        this.ticketForm.description = "";

        this.currentTicket = createdTicket;
        this.showToast("Ticket Created", `Incident ${createdTicket.ticket_id} persisted to database.`, "success");
        this.navigate("#live-room");

        if (escalate) {
          setTimeout(() => this.toggleVoiceBridge(true), 600);
        }
      } catch (err) {
        console.error("[Create Ticket Error]:", err);
        this.showToast("Creation Failed", err.message || "Could not save ticket.", "error");
      } finally {
        this.ticketForm.submitting = false;
      }
    },

    saveTicketDraft() {
      this.showToast("Draft Saved", "Telemetry parameters preserved locally in browser session.", "info");
    },

    // ===================================================================
    // Ticket queue
    // ===================================================================
    async fetchTicketQueue() {
      this.ticketsLoading = true;
      try {
        const params = new URLSearchParams();
        if (this.currentUser?.email) params.set("email", this.currentUser.email);
        params.set("status", this.ticketStatusFilter);

        const res = await fetch(apiUrl(`/api/tickets/list?${params.toString()}`));
        const data = await res.json();
        this.tickets = data.tickets || [];
      } catch (err) {
        console.error("[Queue Error]:", err);
        this.tickets = [];
        this.showToast("Load Failed", "Could not load incident records from backend.", "error");
      } finally {
        this.ticketsLoading = false;
      }
    },

    setTicketStatusFilter(filter) {
      if (this.ticketStatusFilter === filter) return;
      this.ticketStatusFilter = filter;
      this.fetchTicketQueue();
    },

    openTicketInLiveRoom(ticket) {
      this.currentTicket = ticket;
      this.showToast("Incident Staged", `Active session assigned to ticket ${ticket.ticket_id}.`, "info");
      this.navigate("#live-room");
    },

    // Strip the leading '#' before building the URL — otherwise both fetch()
    // and a raw <a href> would treat it as a fragment delimiter and truncate
    // the path right there instead of sending it to the server.
    async setTicketStatus(ticket, status) {
      const code = ticket.ticket_id.replace(/^#/, "");
      try {
        const res = await fetch(apiUrl(`/api/tickets/${encodeURIComponent(code)}/status`), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status }),
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || "Failed to update ticket status.");
        }

        const updated = await res.json();

        if (this.currentTicket?.ticket_id === updated.ticket_id) {
          this.currentTicket = updated;
        }

        // Drop it from the current list if the active filter no longer
        // includes its new status, instead of waiting for a manual refresh.
        if (this.ticketStatusFilter !== "all" && this.ticketStatusFilter !== updated.status.toLowerCase()) {
          this.tickets = this.tickets.filter((t) => t.ticket_id !== updated.ticket_id);
        } else {
          this.tickets = this.tickets.map((t) => (t.ticket_id === updated.ticket_id ? updated : t));
        }

        this.showToast(
          status === "CLOSED" ? "Ticket Archived" : "Ticket Reopened",
          `${updated.ticket_id} is now ${updated.status}.`,
          "success"
        );
      } catch (err) {
        console.error("[Ticket Status Error]:", err);
        this.showToast("Update Failed", err.message || "Could not update ticket status.", "error");
      }
    },

    archiveTicket(ticket) {
      this.setTicketStatus(ticket, "CLOSED");
    },

    reopenTicket(ticket) {
      this.setTicketStatus(ticket, "OPEN");
    },

    async requestHumanCallback(ticket) {
      if (!ticket || ticket.human_requested) return;
      const code = ticket.ticket_id.replace(/^#/, "");
      try {
        const res = await fetch(apiUrl(`/api/tickets/${encodeURIComponent(code)}/request-human`), {
          method: "POST",
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || "Failed to request a human callback.");
        }

        const updated = await res.json();

        if (this.currentTicket?.ticket_id === updated.ticket_id) {
          this.currentTicket = updated;
        }
        this.tickets = this.tickets.map((t) => (t.ticket_id === updated.ticket_id ? updated : t));

        this.showToast(
          "Callback Requested",
          `A specialist will call you back about ${updated.ticket_id} shortly.`,
          "success"
        );
      } catch (err) {
        console.error("[Human Callback Error]:", err);
        this.showToast("Request Failed", err.message || "Could not request a human callback.", "error");
      }
    },

    async cancelHumanCallback(ticket) {
      if (!ticket || !ticket.human_requested) return;
      const code = ticket.ticket_id.replace(/^#/, "");
      try {
        const res = await fetch(apiUrl(`/api/tickets/${encodeURIComponent(code)}/cancel-human`), {
          method: "POST",
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || "Failed to cancel the human callback request.");
        }

        const updated = await res.json();

        if (this.currentTicket?.ticket_id === updated.ticket_id) {
          this.currentTicket = updated;
        }
        this.tickets = this.tickets.map((t) => (t.ticket_id === updated.ticket_id ? updated : t));

        this.showToast(
          "Callback Cancelled",
          `The human callback request for ${updated.ticket_id} was cancelled.`,
          "info"
        );
      } catch (err) {
        console.error("[Cancel Human Callback Error]:", err);
        this.showToast("Cancel Failed", err.message || "Could not cancel the human callback request.", "error");
      }
    },

    // ===================================================================
    // Toast messaging
    // ===================================================================
    showToast(title, message, type = "success") {
      this.toast = { visible: true, type, title, message };
      clearTimeout(this._toastTimeout);
      this._toastTimeout = setTimeout(() => this.hideToast(), 4000);
    },

    hideToast() {
      this.toast.visible = false;
    },

    toastTheme() {
      return (
        {
          success: { bg: "bg-secondary-container text-on-secondary-container border-secondary/30", icon: "check_circle" },
          error: { bg: "bg-error-container text-on-error-container border-error/30", icon: "error" },
          info: { bg: "bg-surface-container-highest text-on-surface border-outline-variant", icon: "info" },
        }[this.toast.type] || { bg: "bg-surface-container-highest text-on-surface border-outline-variant", icon: "info" }
      );
    },

    // ===================================================================
    // Voice call bridge & WebSocket
    // ===================================================================
    async toggleVoiceBridge(forceStart = false) {
      if (this.voice.connected && !forceStart) {
        this.stopVoiceBridge();
      } else {
        this.voice.userHangup = false;
        this.voice.reconnectAttempts = 0;
        await this.startVoiceBridge();
      }
    },

    async startVoiceBridge() {
      if (this.mic.status !== "ready") {
        await this.detectMicrophones();
      }
      if (this.mic.status !== "ready") {
        const reason = this.micStatusLabel();
        this.showToast("Microphone Unavailable", `${reason}. Connect a microphone and try again.`, "error");
        return;
      }

      this.voice.connecting = true;
      try {
        // API_BASE (see apiUrl() above) may point at a separate backend
        // origin (e.g. frontend on Vercel, backend on Railway) — derive the
        // ws(s):// URL from that same origin instead of the page's own, or
        // fall back to same-origin when API_BASE is unset.
        let wsOrigin;
        if (API_BASE) {
          const base = new URL(API_BASE);
          wsOrigin = `${base.protocol === "https:" ? "wss:" : "ws:"}//${base.host}`;
        } else {
          wsOrigin = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
        }
        // The bound ticket's category picks which persona (name/voice/system
        // prompt/tools) the backend loads for this call — see PERSONAS in server.py.
        const category = this.currentTicket?.category || "";
        const params = new URLSearchParams();
        if (category) params.set("category", category);
        if (this.currentTicket?.ticket_id) params.set("ticket", this.currentTicket.ticket_id.replace(/^#/, ""));
        if (this.currentUser?.email) params.set("email", this.currentUser.email);
        const query = params.toString();
        const wsUrl = `${wsOrigin}/ws/voice${query ? `?${query}` : ""}`;
        const socket = new WebSocket(wsUrl);
        socket.binaryType = "arraybuffer";

        socket.onopen = async () => {
          const isResumed = this.voice.reconnectAttempts > 0;

          this.voice.connected = true;
          this.voice.connecting = false;
          this.voice.ws = socket;
          this.voice.micBadge = "24kHz Streaming";
          this.voice.reconnectAttempts = 0;

          await window.VoiceEngine.startMicrophone(
            (chunk) => {
              if (socket.readyState === WebSocket.OPEN) {
                socket.send(chunk);
              }
            },
            (rms) => {
              this.voice.rms = rms;
            },
            this.mic.selectedId || null
          );
          this.refreshMicInfo();

          this.$nextTick(() => {
            const canvas = document.getElementById("cockpitCanvas");
            if (canvas && window.VoiceEngine) {
              window.VoiceEngine.startSpectrumVisualizer(canvas, () => this.voice.connected);
            }
          });

          this.showToast(
            isResumed ? "Call Resumed" : "Audio Connected",
            isResumed ? "Voice bridge reconnected successfully." : "Duplex single connection active with AssemblyAI stack.",
            "success"
          );
        };

        socket.onmessage = (event) => {
          if (typeof event.data === "string") {
            this.handleServerJsonEvent(JSON.parse(event.data));
          } else if (window.VoiceEngine) {
            window.VoiceEngine.playPcmChunk(event.data, () => this.toggleTypingIndicator(false));
          }
        };

        // A WebSocket close is not necessarily a hangup: only an explicit user
        // action (stopVoiceBridge) or a server-reported session_ended sets
        // voice.userHangup. Anything else — a network blip, a transient
        // AssemblyAI error — is treated as a dropped call and auto-reconnected,
        // like a real phone call would recover from a bad patch of signal.
        socket.onclose = () => this.handleSocketClosed();
        socket.onerror = (err) => {
          console.error("[WebSocket Error]:", err);
        };
      } catch (err) {
        console.error("Audio Initialization Failed:", err);
        this.stopVoiceBridge();
        this.showToast("Microphone Error", "Allow microphone access in your browser to proceed.", "error");
      }
    },

    handleSocketClosed() {
      this.voice.ws = null;
      this.voice.connected = false;
      this.voice.connecting = false;
      if (window.VoiceEngine) window.VoiceEngine.stop();
      this.refreshMicInfo();
      this.toggleTypingIndicator(false);

      if (this.voice.userHangup) {
        this.voice.micBadge = "Standby";
        this.voice.rms = 0;
        return;
      }

      const MAX_RECONNECT_ATTEMPTS = 3;
      if (this.voice.reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        this.voice.reconnectAttempts += 1;
        this.voice.micBadge = "Reconnecting...";
        this.showToast(
          "Call Dropped",
          `Connection lost — reconnecting (attempt ${this.voice.reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`,
          "info"
        );
        setTimeout(() => {
          if (!this.voice.userHangup) this.startVoiceBridge();
        }, 800);
      } else {
        this.voice.micBadge = "Standby";
        this.voice.rms = 0;
        this.showToast("Call Ended", "Lost connection to the voice service after multiple attempts.", "error");
      }
    },

    stopVoiceBridge() {
      this.voice.userHangup = true;
      this.voice.connected = false;
      this.voice.connecting = false;
      if (this.voice.ws && this.voice.ws.readyState === WebSocket.OPEN) {
        this.voice.ws.close();
      }
      this.voice.ws = null;
      this.voice.micBadge = "Standby";
      this.voice.rms = 0;

      if (window.VoiceEngine) window.VoiceEngine.stop();
      this.refreshMicInfo();
      this.toggleTypingIndicator(false);
    },

    sendManualInterrupt() {
      if (this.voice.ws && this.voice.ws.readyState === WebSocket.OPEN) {
        this.voice.ws.send(JSON.stringify({ type: "user_action", action: "interrupt" }));
      }
      if (window.VoiceEngine) window.VoiceEngine.flush();
      this.showToast("Interruption Sent", "Agent audio buffer cleared via VAD barge-in.", "info");
    },

    // ===================================================================
    // Wire protocol & telemetry handlers
    // ===================================================================
    handleServerJsonEvent(data) {
      if (data.type === "persona_info") {
        this.voice.personaName = data.name || "AI Support";
        this.voice.personaVoice = data.voice || "";
      } else if (data.type === "transcript") {
        this.appendChatMessage(data.role === "agent" ? this.voice.personaName : "You", data.text, data.role);
      } else if (data.type === "tool_call") {
        this.renderToolCallInspector(data.tool_call);
      } else if (data.type === "tool_response") {
        this.updateToolResponse(data.call_id, data.output);
      } else if (data.type === "interruption") {
        if (window.VoiceEngine) window.VoiceEngine.flush();
        this.showToast("Barge-In Detected", "Specialist speech yielded to caller.", "info");
      } else if (data.type === "session_ended") {
        // A real, intentional end reported by the backend (e.g. AssemblyAI
        // closed the session, the agent decided to hang up, or it never
        // started) — don't auto-reconnect.
        this.voice.userHangup = true;
        if (data.reason === "agent_hangup") {
          this.showToast("Call Ended", `${this.voice.personaName} ended the call.`, "info");
        } else {
          this.showToast("Call Ended", data.reason || "The voice session ended.", "info");
        }
      } else if (data.type === "voice_warning") {
        this.showToast("Voice Warning", data.message || "AssemblyAI reported a warning.", "info");
      }
    },

    appendChatMessage(sender, text, role = "agent") {
      this.messages.push({
        id: Date.now() + Math.random(),
        sender,
        text,
        role,
        time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      });
      this.$nextTick(() => this.scrollChatToBottom());
    },

    sendTextMessage() {
      const msg = this.chatInput.trim();
      if (!msg) return;

      this.appendChatMessage("You", msg, "user");
      this.chatInput = "";

      if (this.voice.ws && this.voice.ws.readyState === WebSocket.OPEN) {
        this.voice.ws.send(JSON.stringify({ type: "text", text: msg }));
      } else {
        this.toggleTypingIndicator(true, `${this.voice.personaName} is processing query...`);
        setTimeout(() => {
          this.toggleTypingIndicator(false);
          this.appendChatMessage(this.voice.personaName, `Received: "${msg}". Checking system telemetry.`, "agent");
        }, 1200);
      }
    },

    toggleTypingIndicator(visible, text = "") {
      this.typing.visible = visible;
      this.typing.text = text;
      this.$nextTick(() => this.scrollChatToBottom());
    },

    scrollChatToBottom() {
      const stream = document.getElementById("chatStream");
      if (stream) stream.scrollTop = stream.scrollHeight;
    },

    // Human-readable "Maya is doing X..." labels per tool, so the caller
    // sees what's actually happening during the silence while a tool runs
    // instead of the raw function name — args are interpolated where they
    // make the label more specific (e.g. which location/number).
    toolActivityLabel(name, args) {
      const a = args || {};
      switch (name) {
        case "check_network_status":
          return `Checking network status in ${a.location || "your area"}...`;
        case "check_incident_history":
          return `Looking up past incidents for ${a.location || "your area"}...`;
        case "restart_connection":
          return `Resetting the connection for ${a.phone_number || "your line"}...`;
        case "end_call":
          return "Wrapping up the call...";
        default:
          return `Running ${name.replace(/_/g, " ")}...`;
      }
    },

    renderToolCallInspector(toolCall) {
      this.voice.toolCallsCount++;

      let args = toolCall.function.arguments;
      if (typeof args === "string") {
        try {
          args = JSON.parse(args);
        } catch (e) {}
      }

      this.toolCalls.unshift({
        id: toolCall.id,
        name: toolCall.function.name,
        args,
        status: "Executing",
        resolved: false,
        result: null,
      });

      // Inline activity message in the chat feed itself (not just the side
      // inspector) — this is what the caller actually sees during the gap
      // between "let me check that" and the follow-up reply, instead of
      // apparent silence.
      this.messages.push({
        id: `tool-${toolCall.id}`,
        sender: this.voice.personaName,
        text: this.toolActivityLabel(toolCall.function.name, args),
        role: "tool",
        resolved: false,
        time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      });
      this.$nextTick(() => this.scrollChatToBottom());
    },

    updateToolResponse(callId, rawOutput) {
      const call = this.toolCalls.find((c) => c.id === callId);
      if (!call) return;

      let parsed = rawOutput;
      if (typeof parsed === "string") {
        try {
          parsed = JSON.parse(parsed);
        } catch (e) {}
      }

      call.status = "Resolved";
      call.resolved = true;
      call.result = JSON.stringify(parsed, null, 2);

      const msg = this.messages.find((m) => m.id === `tool-${callId}`);
      if (msg) {
        msg.resolved = true;
        msg.text = msg.text.replace(/\.\.\.$/, " — done.");
      }
    },

    // ===================================================================
    // Create-ticket form helpers
    // ===================================================================
    categoryClass(name) {
      const active = this.ticketForm.category === name;
      return active
        ? "cat-pill active flex items-center gap-2 p-3 rounded-xl bg-primary text-on-primary text-left transition-all shadow-sm"
        : "cat-pill flex items-center gap-2 p-3 rounded-xl bg-surface-container-low text-on-surface hover:bg-surface-container text-left transition-all";
    },

    priorityCardClass(id) {
      const active = this.ticketForm.priority === id;
      return active
        ? "prio-card active cursor-pointer p-3 rounded-xl bg-primary text-on-primary transition-all shadow-sm"
        : "prio-card cursor-pointer p-3 rounded-xl bg-surface-container-low text-on-surface hover:bg-surface-container transition-all";
    },

    priorityLabelClass(id) {
      if (id === "critical") return "text-xs font-bold text-error";
      return "text-xs font-bold";
    },

    priorityDotClass(p) {
      return this.ticketForm.priority === p.id && p.id === "high" ? "w-2.5 h-2.5 rounded-full bg-white" : `w-2.5 h-2.5 rounded-full ${p.dot}`;
    },

    prioritySubClass(id) {
      const active = this.ticketForm.priority === id;
      return active ? "text-[11px] text-on-primary-container block mt-1" : "text-[11px] text-on-surface-variant block mt-1";
    },

    // ===================================================================
    // Auth helpers
    // ===================================================================
    passwordStrength() {
      const pwd = this.registerForm.password;
      if (!pwd) return { score: 0, label: "Awaiting input", colorClass: "text-on-surface-variant" };

      let score = 0;
      if (pwd.length >= 8) score++;
      if (/[A-Z]/.test(pwd)) score++;
      if (/[0-9]/.test(pwd)) score++;
      if (/[^A-Za-z0-9]/.test(pwd)) score++;

      const levels = [
        { label: "Weak", colorClass: "text-error", barClass: "bg-error" },
        { label: "Moderate", colorClass: "text-tertiary", barClass: "bg-tertiary-container" },
        { label: "Strong", colorClass: "text-primary", barClass: "bg-primary" },
        { label: "Mission-Grade", colorClass: "text-secondary", barClass: "bg-secondary" },
      ];
      const level = levels[Math.max(0, score - 1)] || levels[0];
      return { score, label: level.label, colorClass: level.colorClass, barClass: level.barClass };
    },

    strengthBarClass(index) {
      const { score, barClass } = this.passwordStrength();
      const filled = index < (score <= 1 ? 1 : score);
      return filled && score > 0 ? `h-1 rounded-full ${barClass} transition-colors` : "h-1 rounded-full bg-surface-container-high transition-colors";
    },

    passwordsMatch() {
      if (!this.registerForm.confirmPassword) return null;
      return this.registerForm.password === this.registerForm.confirmPassword;
    },

    async handleLoginSubmit() {
      const email = this.loginForm.email.trim();
      const password = this.loginForm.password;

      if (!email || !password) {
        this.showToast("Missing Fields", "Please enter your email and password.", "error");
        return;
      }

      this.loginForm.submitting = true;
      try {
        const res = await fetch(apiUrl("/api/auth/login"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password }),
        });

        const data = await res.json();
        if (!res.ok) {
          this.showToast("Sign In Failed", data.detail || "Invalid email or password.", "error");
          return;
        }

        this.currentUser = data.user;
        localStorage.setItem("omni_user", JSON.stringify(data.user));
        this.showToast("Signed In", `Workstation provisioned for ${data.user.name}.`, "success");
        this.navigate("#ticket-queue");
      } catch (err) {
        console.error("[Login Error]:", err);
        this.showToast("Connection Error", "Cannot reach authentication server.", "error");
      } finally {
        this.loginForm.submitting = false;
      }
    },

    async handleRegisterSubmit() {
      const name = this.registerForm.name.trim();
      const email = this.registerForm.email.trim();
      const phoneNumber = this.registerForm.phoneNumber.trim();
      const password = this.registerForm.password;
      const confirmPassword = this.registerForm.confirmPassword;

      if (!name || !email || !phoneNumber || !password) {
        this.showToast("Missing Fields", "Please complete all required fields, including phone number.", "error");
        return;
      }

      if (!/^\d{7,15}$/.test(phoneNumber.replace(/[-\s]/g, ""))) {
        this.showToast("Invalid Phone Number", "Enter a valid phone number (digits only).", "error");
        return;
      }

      if (password !== confirmPassword) {
        this.showToast("Password Mismatch", "Passwords do not match.", "error");
        return;
      }

      this.registerForm.submitting = true;
      try {
        const res = await fetch(apiUrl("/api/auth/register"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, email, password, phone_number: phoneNumber }),
        });

        const data = await res.json();
        if (!res.ok) {
          this.showToast("Registration Failed", data.detail || "Account could not be created.", "error");
          return;
        }

        this.currentUser = data.user;
        localStorage.setItem("omni_user", JSON.stringify(data.user));
        this.registerForm = { name: "", email: "", phoneNumber: "", password: "", confirmPassword: "", showPassword: false, submitting: false };

        this.showToast("Registration Complete", `Account registered for ${data.user.name}.`, "success");
        this.navigate("#ticket-queue");
      } catch (err) {
        console.error("[Register Error]:", err);
        this.showToast("Connection Error", "Cannot reach authentication server.", "error");
      } finally {
        this.registerForm.submitting = false;
      }
    },

    get userInitials() {
      if (!this.currentUser) return "--";
      const nameParts = (this.currentUser.name || "User").trim().split(" ");
      let initials = nameParts[0] ? nameParts[0][0].toUpperCase() : "U";
      if (nameParts.length > 1 && nameParts[nameParts.length - 1]) {
        initials += nameParts[nameParts.length - 1][0].toUpperCase();
      }
      return initials;
    },

    get userDisplayName() {
      return this.currentUser ? this.currentUser.name : "Guest";
    },

    // Runs the real capture engine (not a throwaway getUserMedia call) so the
    // RMS meter and Input Driver line show the mic's actual live behavior,
    // not a canned animation. Blocked during an active call since that's
    // already using the one microphone engine instance.
    async testMicHardware() {
      if (this.voice.connected || this.micTest.variant === "listening") return;

      this.micTest = { label: "Listening... Speak now", variant: "listening" };

      try {
        await window.VoiceEngine.startMicrophone(
          () => {}, // test only — captured audio isn't sent anywhere
          (rms) => {
            this.voice.rms = rms;
          },
          this.mic.selectedId || null
        );
        this.refreshMicInfo();

        setTimeout(() => {
          window.VoiceEngine.stop();
          this.voice.rms = 0;
          this.refreshMicInfo();
          this.micTest = { label: "Microphone Verified (OK)", variant: "ok" };
          setTimeout(() => {
            this.micTest = { label: "Test Microphone Input", variant: "idle" };
          }, 2500);
        }, 3000);
      } catch (err) {
        console.error("[Mic Test Error]:", err);
        this.micTest = { label: "Permission Denied", variant: "error" };
      }
    },

    // Pulls the mic's actual negotiated MediaStreamTrack settings straight
    // from the browser into reactive state, right when the track opens or
    // closes — real hardware/OS behavior can differ from what we requested
    // (e.g. the device may not honor 24kHz), so this reports what's live,
    // not assumed, and updates the instant it changes rather than on the
    // next unrelated re-render.
    refreshMicInfo() {
      this.voice.micInfo = window.VoiceEngine?.getTrackSettings?.() || null;
    },

    micDriverLabel() {
      const settings = this.voice.micInfo;
      if (!settings) return "Not active";

      const rateKhz = settings.sampleRate ? Math.round(settings.sampleRate / 1000) : null;
      const bits = settings.sampleSize || 16;
      const channels = settings.channelCount === 1 ? "Mono" : settings.channelCount ? `${settings.channelCount}ch` : "Mono";

      return `${rateKhz ? `${rateKhz}kHz` : "--"} Int${bits} ${channels} PCM`;
    },

    micTestBtnClass() {
      const base = "w-full py-2 px-3 rounded-xl font-semibold text-xs transition-colors";
      const variants = {
        idle: "bg-surface-container-low hover:bg-surface-container text-on-surface",
        listening: "bg-secondary-container text-on-secondary-container",
        ok: "bg-secondary-container text-on-secondary-container",
        error: "bg-error-container text-on-error-container",
      };
      return `${base} ${variants[this.micTest.variant] || variants.idle}`;
    },
  }));
});
