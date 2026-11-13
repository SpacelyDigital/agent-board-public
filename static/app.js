"use strict";

(() => {
  if (document.documentElement.dataset.providerMarks === "off") {
    document.querySelectorAll(".usage-logo").forEach(mark => mark.remove());
  }
  // The native app injects this in its own loopback origin. Keep it out of URLs,
  // persistent web storage, and the global object after initialization.
  const accessToken = typeof window.__agentBoardAccessToken === "string" ? window.__agentBoardAccessToken : null;
  delete window.__agentBoardAccessToken;
  const nativeBoard = window.webkit?.messageHandlers?.agentBoard;
  const labels = { cooking: "Cooking", needs_you: "Needs you", ready: "Ready", disconnected: "Disconnected", idle: "Open" };
  const providerNames = { codex: "Codex", claude: "Claude", chatgpt: "ChatGPT" };
  const priority = { cooking: 0, needs_you: 1, ready: 1, disconnected: 2 };
  const elements = Object.fromEntries([
    "connection", "connection-text", "summary", "monitor-message", "action-message", "conversation-updates", "loading", "empty", "empty-samples", "sample-controls", "usage",
    "attention-section", "attention-list", "attention-count", "cooking-section", "cooking-list", "cooking-count",
    "history", "history-count", "history-list", "sources", "last-sync", "settings-toggle", "sound-toggle", "sound-select", "sound-preview", "fullscreen-toggle",
    "board-tab", "background-tab", "board-panel", "background-panel", "background-list", "background-empty", "background-count",
    "appearance-menu", "layout-select", "theme-select", "mode-select", "motion-toggle", "motion-option", "active-workspace", "layout-primary", "primary-heading", "primary-card", "layout-sections", "split-queue", "split-list",
    "rename-dialog", "rename-form", "rename-input", "rename-error", "rename-cancel", "rename-save"
  ].map(id => [id, document.getElementById(id)]));
  const cards = new Map();
  const queueItems = new Map();
  const layouts = ["ledger", "split", "columns", "focus", "instrument"];
  const themes = window.AgentBoardThemes;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const pending = new Set();
  const usageWindows = [
    { provider: "claude", window: "weekly" },
    { provider: "codex", window: "weekly" }
  ].map(item => ({ ...item, element: document.getElementById(`usage-${item.provider}-${item.window}`) }));
  const creditRows = ["claude", "codex"].map(id => ({id, element: document.getElementById(`usage-${id}-credits`)}));
  const percentFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const creditFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const dollarFormat = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 0, maximumFractionDigits: 0 });
  let state = null;
  let activeTab = "board";
  let connected = false;
  let attemptedConnection = false;
  let lastSuccess = null;
  let serverOffset = 0;
  let previousStatuses = new Map();
  let previousAnnouncedCounts = null;
  let polling = null;
  let cardSequence = 0;
  let renameKey = null;
  let renameRevision = 0;
  let layout = "ledger";
  let appearance = themes.readPreferences();
  let selectedKey = null;
  let primaryKey = null;
  let audioContext = null;
  const alertSounds = [
    ["soft-ding", "Soft Ding"], ["chime", "Chime"], ["bell", "Bell"], ["glass", "Glass"],
    ["marimba", "Marimba"], ["arcade", "Arcade"], ["radar", "Radar"],
    ["space-signal", "Space Signal"], ["pop", "Pop"], ["fart", "Fart"]
  ];
  let soundID = "soft-ding";
  const soundBuffers = new Map();
  let activeSound = null;
  let soundGeneration = 0;
  let soundEnabled = false;
  let samplePending = false;
  try { soundEnabled = localStorage.getItem("agent-board:sound") === "on"; } catch (_) { /* Storage can be disabled. */ }
  try {
    const savedSound = localStorage.getItem("agent-board:sound-id");
    if (alertSounds.some(([id]) => id === savedSound)) soundID = savedSound;
  } catch (_) { /* Optional preference. */ }
  try {
    const storedLayout = localStorage.getItem("agent-board:layout");
    selectedKey = localStorage.getItem("agent-board:selected");
    if (layouts.includes(storedLayout)) layout = storedLayout;
  } catch (_) { /* Optional preferences. */ }

  function now() { return Date.now() / 1000 + serverOffset; }

  function elapsed(timestamp) {
    const seconds = Math.max(0, Math.floor(now() - Number(timestamp || now())));
    if (seconds < 60) return "<1m";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
    return `${Math.floor(seconds / 86400)}d`;
  }

  function projectName(cwd) {
    if (!cwd) return "Unknown project";
    return String(cwd).replace(/\/+$/, "").split("/").filter(Boolean).pop() || "/";
  }

  function setText(element, value) {
    const text = String(value ?? "");
    if (element.textContent !== text) element.textContent = text;
  }

  function make(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function allSessions() { return state ? [...state.sessions, ...state.history, ...(state.background || [])] : []; }
  function findSession(key) { return allSessions().find(session => session.key === key); }
  function isSnoozed(session) { return Number(session.snoozed_until || 0) > now(); }
  function visibleStatus(session) {
    if (session.source_unavailable === true) return "disconnected";
    if (session.catalog_only === true) return "disconnected";
    return session.open_only === true ? "idle" : labels[session.status] ? session.status : "disconnected";
  }

  function makeCard(key) {
    const card = make("article", "session");
    card.dataset.key = key;
    const main = make("div", "session-main");
    const title = make("h3", "session-title");
    const meta = make("div", "session-meta");
    const provider = make("span", "provider");
    const project = make("span", "project");
    const separator = make("span", "meta-separator", "/");
    meta.append(provider, separator, project);
    const snoozeNote = make("span", "snooze-note");
    snoozeNote.hidden = true;
    const previewToggle = make("button", "preview-toggle");
    previewToggle.type = "button";
    previewToggle.dataset.preview = key;
    previewToggle.setAttribute("aria-expanded", "false");
    previewToggle.hidden = true;
    const previewContent = make("div", "preview-content");
    previewContent.id = `session-preview-${++cardSequence}`;
    title.id = `session-title-${cardSequence}`;
    card.setAttribute("aria-labelledby", title.id);
    previewContent.hidden = true;
    previewToggle.setAttribute("aria-controls", previewContent.id);
    const previewText = make("p", "preview-text");
    previewContent.append(previewText);
    meta.append(snoozeNote, previewToggle);
    const detail = make("p", "session-detail");
    main.append(title, meta, detail, previewContent);
    const side = make("div", "session-side");
    const status = make("div", "session-state");
    const badge = make("span", "status-badge");
    const age = make("span", "session-age");
    status.append(badge, age);
    const actions = make("div", "session-actions");
    const open = make("button", "action-button", "Open");
    open.type = "button";
    open.dataset.action = "open";
    const review = make("button", "action-button review-button", "Reviewed");
    review.type = "button";
    review.dataset.action = "review";
    const options = make("div", "session-options");
    const menuToggle = make("button", "action-button menu-toggle", "⋯");
    menuToggle.type = "button";
    menuToggle.dataset.menu = key;
    menuToggle.setAttribute("aria-label", "Conversation options");
    menuToggle.setAttribute("aria-haspopup", "menu");
    menuToggle.setAttribute("aria-expanded", "false");
    const menu = make("div", "session-menu");
    menu.id = `session-menu-${cardSequence}`;
    menu.hidden = true;
    menu.setAttribute("role", "menu");
    menuToggle.setAttribute("aria-controls", menu.id);
    const menuItems = {};
    for (const [action, label] of [["rename", "Rename"], ["automatic-title", "Use automatic title"], ["move", "Move to Background"], ["automatic-placement", "Use automatic placement"], ["snooze", "Snooze for 10 minutes"]]) {
      const button = make("button", "menu-item", label);
      button.type = "button";
      button.dataset.menuAction = action;
      button.setAttribute("role", "menuitem");
      menuItems[action] = button;
      menu.append(button);
    }
    options.append(menuToggle, menu);
    actions.append(open, review, options);
    side.append(status, actions);
    card.append(main, side);
    return { card, title, provider, project, separator, detail, badge, age, open, review, snoozeNote, previewToggle, previewContent, previewText, options, menuToggle, menu, menuItems };
  }

  function updateCard(session, archived, background = false) {
    let item = cards.get(session.key);
    if (!item) { item = makeCard(session.key); cards.set(session.key, item); }
    const { card, title, provider, project, detail, badge, age, open, review, snoozeNote, previewToggle, previewContent, previewText, options, menuToggle, menuItems } = item;
    const openOnly = session.open_only === true;
    const status = openOnly && !session.catalog_only ? "idle" : labels[session.status] ? session.status : "disconnected";
    const sourceUnavailable = session.source_unavailable === true;
    card.classList.toggle("history-session", archived);
    card.classList.toggle("background-session", background);
    card.classList.toggle("stale", !connected || sourceUnavailable);
    card.dataset.status = visibleStatus(session);
    setText(title, session.title || "Untitled conversation");
    title.title = session.title || "Untitled conversation";
    setText(provider, providerNames[session.provider] || session.provider);
    setText(project, session.project_name || projectName(session.cwd));
    project.hidden = item.separator.hidden = !session.project_name && !session.cwd;
    project.title = session.cwd || session.project_name || "";
    const detailText = !connected ? `Last known state: ${labels[status]}.` : sourceUnavailable ? session.source_unavailable_detail || "This source is unavailable. Reconnect it to confirm new activity." : session.detail || "";
    setText(detail, detailText);
    const uncertainActivity = status === "cooking" && /\binferred\b|unconfirmed|could not be confirmed/i.test(detailText);
    detail.hidden = !detailText || (connected && !sourceUnavailable && (status === "ready" || (status === "cooking" && !uncertainActivity)));
    detail.title = detailText;
    setText(badge, session.catalog_only ? "Status unavailable" : archived ? "Reviewed" : !connected ? "Unconfirmed" : sourceUnavailable ? `Last known: ${labels[status]}` : labels[status]);
    badge.dataset.status = archived || !connected || sourceUnavailable ? "disconnected" : status;
    badge.title = detailText;
    const displayedStatus = visibleStatus(session);
    const timestamp = displayedStatus === "cooking" && session.started_at ? session.started_at : ["ready", "needs_you"].includes(displayedStatus) ? session.attention_since || session.updated_at : session.updated_at;
    age.dataset.timestamp = timestamp || "";
    age.dataset.status = displayedStatus;
    age.dataset.hasStart = session.started_at ? "true" : "false";
    age.dataset.archived = archived ? "true" : "false";
    age.dataset.background = background ? "true" : "false";
    age.hidden = openOnly;
    updateAge(age);
    const isPending = pending.has(session.key);
    const snoozed = isSnoozed(session);
    snoozeNote.hidden = !snoozed;
    setText(snoozeNote, snoozed ? `Snoozed ${resetCountdown(Number(session.snoozed_until)).replace(/^Resets in /, "")}` : "");
    snoozeNote.title = snoozed ? `Snoozed until ${localTimestamp(Number(session.snoozed_until))}` : "";
    const preview = typeof session.preview === "string" ? session.preview.slice(0, 2400) : "";
    const hasPreview = !!preview.trim();
    previewToggle.hidden = !hasPreview;
    setText(previewToggle, session.preview_kind === "permission" ? "Request details" : "Reply");
    setText(previewText, preview);
    if (session.key === primaryKey && hasPreview) {
      previewContent.hidden = false;
      previewToggle.setAttribute("aria-expanded", "true");
      previewToggle.hidden = true;
    }
    if (!hasPreview) { previewContent.hidden = true; previewToggle.setAttribute("aria-expanded", "false"); }
    previewToggle.setAttribute("aria-label", `${previewToggle.getAttribute("aria-expanded") === "true" ? "Hide" : "Show"} ${session.preview_kind === "permission" ? "request details" : "reply preview"}: ${title.textContent}`);
    setText(open, session.open_label || "Open");
    const canOpen = session.can_open && session.open_kind !== "app";
    open.disabled = !connected || isPending || !canOpen;
    open.title = canOpen ? "Open this conversation" : session.open_error || "A direct link to this conversation is unavailable.";
    review.dataset.action = archived ? "restore" : "review";
    review.hidden = background;
    setText(review, isPending ? "Working…" : archived ? "Restore" : "Reviewed");
    review.disabled = !connected || isPending || (!archived && status === "cooking");
    review.title = !archived && status === "cooking" ? "You can mark this reviewed when the agent stops working." : archived ? "Return this conversation to the board" : "Remove this conversation from the board";
    options.hidden = openOnly;
    menuToggle.disabled = !connected || isPending || options.hidden;
    menuItems.rename.disabled = !connected || isPending;
    menuItems["automatic-title"].hidden = !session.title_override;
    menuItems.move.dataset.placement = background ? "board" : "background";
    setText(menuItems.move, background ? "Move to Board" : "Move to Background");
    menuItems["automatic-placement"].hidden = !["board", "background"].includes(session.placement_override);
    menuItems.snooze.hidden = background || archived || !["ready", "needs_you"].includes(status);
    for (const button of Object.values(menuItems)) button.disabled = !connected || isPending;
    for (const button of [open, review, ...Object.values(menuItems)]) {
      const label = `${button.textContent}: ${title.textContent}`;
      if (button.getAttribute("aria-label") !== label) button.setAttribute("aria-label", label);
    }
    const menuLabel = `Conversation options: ${title.textContent}`;
    if (menuToggle.getAttribute("aria-label") !== menuLabel) menuToggle.setAttribute("aria-label", menuLabel);
    return card;
  }

  function updateAge(element) {
    const timestamp = Number(element.dataset.timestamp);
    const age = elapsed(timestamp);
    const unreviewed = element.dataset.archived !== "true" && element.dataset.background !== "true";
    const working = element.dataset.status === "cooking" && unreviewed && connected;
    const waiting = ["ready", "needs_you"].includes(element.dataset.status) && unreviewed && connected;
    const description = waiting ? `Waiting for you for ${age}` : working ? element.dataset.hasStart === "true" ? `Working for ${age}` : `Last active ${age} ago` : `${age} ago`;
    const compact = ["ledger", "instrument"].includes(layout) || !!element.closest(".split-item");
    setText(element, compact ? `${age}${waiting ? " waiting" : working && element.dataset.hasStart === "true" ? " running" : " ago"}` : description);
    element.setAttribute("aria-label", description);
    element.title = timestamp ? `${working ? "Working since" : "Updated"} ${new Date(timestamp * 1000).toLocaleString()}` : "";
  }

  function reconcileList(container, sessions, archived, background = false) {
    // Reuse and move existing nodes so polling never replaces a focused control.
    for (let index = 0; index < sessions.length; index += 1) {
      const node = updateCard(sessions[index], archived, background);
      if (container.children[index] !== node) container.insertBefore(node, container.children[index] || null);
    }
    const allowed = new Set(sessions.map(session => session.key));
    for (const child of [...container.children]) {
      if (!allowed.has(child.dataset.key)) child.remove();
    }
  }

  function reconcileQueue(sessions) {
    const container = elements["split-list"];
    for (let index = 0; index < sessions.length; index += 1) {
      const session = sessions[index];
      let item = queueItems.get(session.key);
      if (!item) {
        const button = make("button", "split-item");
        button.type = "button";
        button.dataset.select = session.key;
        const top = make("span", "split-row-top");
        const badge = make("span", "status-badge");
        const age = make("span", "session-age");
        const title = make("strong", "split-title");
        const meta = make("span", "split-meta");
        top.append(badge, age);
        button.append(top, title, meta);
        item = { button, title, meta, badge, age };
        queueItems.set(session.key, item);
      }
      const selected = session.key === selectedKey;
      item.button.classList.toggle("is-selected", selected);
      item.button.setAttribute("aria-pressed", String(selected));
      item.button.tabIndex = selected ? 0 : -1;
      const displayedStatus = visibleStatus(session);
      item.button.dataset.status = displayedStatus;
      setText(item.title, session.title || "Untitled conversation");
      const provider = providerNames[session.provider] || session.provider;
      setText(item.meta, `${provider} / ${session.project_name || projectName(session.cwd)}`);
      const sourceUnavailable = session.source_unavailable === true;
      const statusLabel = labels[session.status] || "Disconnected";
      setText(item.badge, !connected ? "Unconfirmed" : sourceUnavailable ? `Last known: ${statusLabel}` : statusLabel);
      item.badge.dataset.status = connected && !sourceUnavailable ? session.status : "disconnected";
      item.button.title = sourceUnavailable ? session.source_unavailable_detail || "This source is unavailable. Reconnect it to confirm new activity." : "";
      item.age.dataset.timestamp = displayedStatus === "cooking" ? session.started_at || session.updated_at || "" : ["ready", "needs_you"].includes(displayedStatus) ? session.attention_since || session.updated_at || "" : session.updated_at || "";
      item.age.dataset.status = displayedStatus;
      item.age.dataset.hasStart = session.started_at ? "true" : "false";
      updateAge(item.age);
      if (container.children[index] !== item.button) container.insertBefore(item.button, container.children[index] || null);
    }
    const allowed = new Set(sessions.map(session => session.key));
    for (const [key, item] of queueItems) {
      if (!allowed.has(key)) { item.button.remove(); queueItems.delete(key); }
    }
  }

  function renderWorkspace(sessions, attention, cooking) {
    const split = layout === "split";
    const focus = layout === "focus";
    if (split && !sessions.some(session => session.key === selectedKey)) selectedKey = sessions[0]?.key || null;
    primaryKey = split ? selectedKey : focus ? sessions[0]?.key || null : null;
    const selected = sessions.find(session => session.key === primaryKey);
    elements["split-queue"].hidden = !split || !sessions.length;
    elements["layout-primary"].hidden = !selected;
    elements["layout-sections"].hidden = split;
    const otherAttention = attention.filter(session => session.key !== primaryKey);
    const otherCooking = cooking.filter(session => session.key !== primaryKey);
    elements["attention-section"].hidden = !otherAttention.length || split;
    elements["cooking-section"].hidden = !otherCooking.length || split;
    setText(document.getElementById("attention-heading"), focus ? "Other waiting conversations" : "Needs your attention");
    setText(elements["attention-count"], otherAttention.length);
    setText(elements["cooking-count"], otherCooking.length);
    reconcileList(elements["attention-list"], split ? [] : otherAttention, false);
    reconcileList(elements["cooking-list"], split ? [] : otherCooking, false);
    const primaryContainer = elements["primary-card"];
    if (selected) {
      const status = visibleStatus(selected);
      const heading = focus && status !== "disconnected" ? status === "cooking" ? "Cooking" : "Latest waiting conversation" : "Selected conversation";
      setText(elements["primary-heading"], heading);
      const node = updateCard(selected, false);
      if (primaryContainer.firstElementChild !== node) primaryContainer.replaceChildren(node);
    } else if (primaryContainer.children.length) primaryContainer.replaceChildren();
    if (split) reconcileQueue(sessions);
    else if (elements["split-list"].children.length) elements["split-list"].replaceChildren();
    // Items stay cached while another layout is shown, then reattach on return.
  }

  function renderBackground() {
    const background = Array.isArray(state?.background) ? [...state.background] : [];
    setText(elements["background-count"], background.length);
    elements["background-count"].hidden = !background.length;
    if (activeTab !== "background") {
      if (elements["background-list"].children.length) elements["background-list"].replaceChildren();
      return;
    }
    background.sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0) || String(a.key).localeCompare(String(b.key)));
    elements["background-empty"].hidden = !!background.length;
    setText(elements["background-empty"].querySelector("p"), !state ? "Loading background sessions." : "There are no background sessions.");
    reconcileList(elements["background-list"], background, false, true);
  }

  function selectTab(name) {
    closeMenus();
    activeTab = name === "background" ? "background" : "board";
    for (const tab of ["board", "background"]) {
      const selected = tab === activeTab;
      elements[`${tab}-tab`].setAttribute("aria-selected", String(selected));
      elements[`${tab}-tab`].tabIndex = selected ? 0 : -1;
      elements[`${tab}-panel`].hidden = !selected;
    }
    renderBackground();
  }

  function renderSources() {
    const sources = state?.sources || [];
    const fragment = document.createDocumentFragment();
    for (const source of sources) {
      const available = connected && source.available;
      const limited = available && source.coverage_limited === true;
      const item = make("span", `source ${available ? "available" : "unavailable"}${limited ? " limited" : ""}`);
      item.append(make("span", "source-dot"), make("span", "source-name", source.name || source.id));
      const sourceStatus = limited ? "limited" : source.waiting_for_chat === true ? "waiting for a chat" : "connected";
      const health = !connected ? "unconfirmed" : source.sample === true ? "sample" : source.available ? sourceStatus : "unavailable";
      item.append(make("span", "source-health", health));
      item.title = source.detail || health;
      item.setAttribute("role", "group");
      item.setAttribute("aria-label", `${source.name || source.id}: ${health}${source.detail ? `. ${source.detail}` : ""}`);
      fragment.append(item);
    }
    elements.sources.replaceChildren(fragment);
  }

  function resetCountdown(timestamp) {
    const seconds = timestamp - now();
    if (seconds <= 0) return "Reset due";
    if (seconds < 60) return "Resets in <1m";
    const minutes = Math.ceil(seconds / 60);
    if (minutes < 60) return `Resets in ${minutes}m`;
    if (minutes < 1440) {
      const remainder = minutes % 60;
      return `Resets in ${Math.floor(minutes / 60)}h${remainder ? ` ${remainder}m` : ""}`;
    }
    const hours = Math.floor(minutes % 1440 / 60);
    return `Resets in ${Math.floor(minutes / 1440)}d${hours ? ` ${hours}h` : ""}`;
  }

  function localTimestamp(timestamp) {
    return new Date(timestamp * 1000).toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short"
    });
  }

  function usageIsStale(provider, credit = null) {
    if (state?.sample_mode === true) return false;
    if (!connected || provider?.stale || credit?.stale) return true;
    const observed = [provider?.updated_at, credit?.observed_at].filter(value => Number.isFinite(value) && value > 0);
    const updated = observed.length ? Math.min(...observed) : null;
    const deadline = updated && Number.isFinite(provider?.stale_at) && provider.stale_at >= updated
      ? provider.stale_at : updated ? updated + 120 : null;
    const resetPassed = provider?.windows?.some(window => Number.isFinite(window.reset_at) && window.reset_at > 0 && window.reset_at <= now());
    return !!resetPassed || (deadline !== null && now() > deadline);
  }

  function renderUsage() {
    for (const item of usageWindows) {
      const provider = state?.usage?.find(source => source.id === item.provider);
      const window = provider?.windows?.find(entry => entry.id === item.window);
      const reading = window?.remaining_percent;
      const hasReading = typeof reading === "number" && Number.isFinite(reading) && reading >= 0 && reading <= 100;
      const updated = hasReading && typeof provider?.updated_at === "number" && Number.isFinite(provider.updated_at) && provider.updated_at > 0 ? provider.updated_at : null;
      const reset = typeof window?.reset_at === "number" && Number.isFinite(window.reset_at) && window.reset_at > 0 ? window.reset_at : null;
      const loading = (provider?.loading && connected) || (!state && !attemptedConnection);
      const localReading = provider?.source === "local_record" || provider?.source === "local_export";
      const stale = hasReading && (usageIsStale(provider) || !provider?.available);
      const value = item.element.querySelector(".usage-value");
      const resetText = item.element.querySelector(".usage-reset");
      item.element.classList.toggle("stale", !!stale);
      item.element.classList.toggle("unavailable", !hasReading);
      if (loading && !hasReading) {
        setText(value, "Loading");
        setText(resetText, "Waiting for usage data.");
        resetText.title = "";
      } else {
        setText(value, hasReading ? `${percentFormat.format(reading)}% remaining` : "Unavailable");
        setText(resetText, reset ? resetCountdown(reset) : hasReading ? "Reset time unavailable." : "");
        resetText.title = reset ? `Scheduled reset: ${localTimestamp(reset)}` : provider?.error || "";
      }
      item.element.title = [localReading && hasReading ? "This is a cached provider reading from local session data." : "", provider?.error, updated ? `Last usage reading: ${localTimestamp(updated)}` : ""].filter(Boolean).join("\n");
    }
    for (const {id, element} of creditRows) {
      const provider = state?.usage?.find(source => source.id === id);
      const credit = provider?.credits;
      const sample = state?.sample_mode === true;
      const hasBalance = credit?.unit === (id === "claude" ? "USD" : "credits") && Number.isFinite(credit.amount);
      const creditsDisabled = id === "claude" && credit?.status === "disabled" && !hasBalance;
      const loading = !hasBalance && !creditsDisabled && ((provider?.loading && connected) || (!state && !attemptedConnection));
      const stale = (hasBalance || creditsDisabled) && !sample && usageIsStale(provider, credit);
      const balance = id === "claude" ? dollarFormat : creditFormat;
      element.classList.toggle("unavailable", !hasBalance && !creditsDisabled);
      element.classList.toggle("stale", stale);
      setText(element.querySelector(".usage-value"), hasBalance ? balance.format(credit.amount) : creditsDisabled ? "Turned off" : loading ? "Loading" : "Unavailable");
      const balanceLink = element.querySelector(".usage-balance-link");
      balanceLink.hidden = hasBalance || loading || sample || document.documentElement.dataset.providerMarks === "off";
      setText(balanceLink, creditsDisabled ? "Manage credits" : "View balance");
      balanceLink.setAttribute("aria-label", creditsDisabled ? "Manage Claude usage credits" : `View ${id === "claude" ? "Claude" : "Codex"} credit balance`);
    }
    let hasNotes = false;
    for (const id of ["claude", "codex"]) {
      const provider = state?.usage?.find(source => source.id === id);
      const note = document.getElementById(`usage-${id}-note`);
      const credit = provider?.credits;
      const hasUsage = provider?.windows?.some(window => Number.isFinite(window.remaining_percent) && window.remaining_percent >= 0 && window.remaining_percent <= 100);
      const hasCredits = credit?.unit === (id === "claude" ? "USD" : "credits") && (Number.isFinite(credit.amount) || (id === "claude" && credit.status === "disabled"));
      const updated = [hasUsage ? provider?.updated_at : null, hasCredits ? credit?.observed_at : null].filter(value => Number.isFinite(value) && value > 0);
      const observed = updated.length ? Math.min(...updated) : null;
      const stale = usageIsStale(provider, credit);
      const local = provider?.source === "local_record" || provider?.source === "local_export";
      const sample = state?.sample_mode === true;
      const rateLimited = provider?.error_code === "rate_limited";
      const accessNeeded = provider?.error_code === "claude_account_access_required";
      const retry = Number.isFinite(provider?.retry_at) && provider.retry_at > now()
        ? `Retrying in ${Math.max(1, Math.ceil((provider.retry_at - now()) / 60))}m.` : "";
      let text = "";
      if (sample) text = "Sample usage and balance.";
      else if (rateLimited) text = ["Usage checks are temporarily rate limited.", observed ? `Last reading ${elapsed(observed)} ago.` : "", retry].filter(Boolean).join(" ");
      else if (accessNeeded) text = ["Allow Claude account access in Settings.", observed ? `Last reading ${elapsed(observed)} ago.` : ""].filter(Boolean).join(" ");
      else if (stale && observed) text = `This reading is stale; last updated ${elapsed(observed)} ago.`;
      else if (!connected && attemptedConnection) text = "The monitor is disconnected.";
      else if (provider?.error) text = provider.error;
      else if (local && observed) text = `Observed locally ${elapsed(observed)} ago.`;
      else if (observed && Number.isFinite(provider?.next_refresh_at) && provider.next_refresh_at - observed > 120) {
        const nextCheck = provider.next_refresh_at > now()
          ? `Next check in ${Math.max(1, Math.ceil((provider.next_refresh_at - now()) / 60))}m.` : "";
        text = [`Last reading ${elapsed(observed)} ago.`, nextCheck].filter(Boolean).join(" ");
      }
      else if (provider && !provider.available && !provider.loading) text = "No usage reading is available.";
      note.hidden = !text;
      note.classList.toggle("stale", !!stale || rateLimited);
      setText(note, text);
      note.title = observed ? `Last usage reading: ${localTimestamp(observed)}` : "";
      hasNotes ||= !!text;
    }
    elements.usage.classList.toggle("has-notes", hasNotes);
  }

  function render() {
    const sampleMode = state?.sample_mode === true;
    elements["settings-toggle"].hidden = !nativeBoard || sampleMode;
    elements.connection.className = `connection ${connected ? "live" : "offline"}`;
    setText(elements["connection-text"], connected ? sampleMode ? "Sample board" : "Monitoring locally" : attemptedConnection ? "Monitor disconnected" : "Connecting");
    elements["sample-controls"].hidden = !sampleMode;
    elements["sample-controls"].querySelectorAll("button").forEach(button => { button.disabled = !connected || samplePending; });
    elements.usage.setAttribute("aria-label", sampleMode ? "Sample usage" : state?.store_profile === true ? "Local usage" : "Account usage");
    elements.summary.classList.toggle("stale", !connected);
    const monitorNotices = [];
    if (typeof state?.recovery_notice === "string" && state.recovery_notice.trim()) monitorNotices.push(state.recovery_notice.slice(0, 2000));
    if (!connected && state) monitorNotices.push("The monitor is disconnected. Conversation states are unconfirmed until it reconnects.");
    elements["monitor-message"].hidden = !monitorNotices.length;
    setText(elements["monitor-message"], monitorNotices.join(" "));
    renderUsage();
    if (!state) { renderBackground(); return; }
    elements.loading.hidden = true;
    const sessions = [...(state.sessions || [])].sort((a, b) => {
      const aStatus = visibleStatus(a), bStatus = visibleStatus(b);
      const statusOrder = (priority[aStatus] ?? 2) - (priority[bStatus] ?? 2);
      if (statusOrder) return statusOrder;
      const newestFirst = Number(b.attention_since || b.updated_at || 0) - Number(a.attention_since || a.updated_at || 0);
      return newestFirst || String(a.key).localeCompare(String(b.key));
    });
    const attention = sessions.filter(session => visibleStatus(session) !== "cooking");
    const cooking = sessions.filter(session => visibleStatus(session) === "cooking");
    const history = [...(state.history || [])].sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
    elements.empty.hidden = !!sessions.length;
    const noSources = !(state.sources || []).some(source => source.available === true);
    elements["empty-samples"].hidden = !(nativeBoard && connected && noSources && !sampleMode && state.store_profile === true);
    setText(elements.empty.querySelector("h2"), !connected ? "Waiting for your monitor." : noSources ? "No sources are connected." : "Your board is clear.");
    setText(elements.empty.querySelector("p"), !connected ? "Reconnect the local monitor to confirm conversation states." : noSources ? "Check the source connections below before waiting for new activity." : "New activity in Codex or Claude will appear here.");
    for (const status of Object.keys(priority)) {
      setText(document.getElementById(`count-${status}`), sessions.filter(session => visibleStatus(session) === status).length);
    }
    renderWorkspace(sessions, attention, cooking);
    reconcileList(elements["history-list"], history, true);
    elements.history.hidden = !history.length;
    setText(elements["history-count"], history.length);
    renderBackground();
    const retained = new Set([...sessions, ...history, ...(state.background || [])].map(session => session.key));
    for (const key of cards.keys()) if (!retained.has(key)) cards.delete(key);
    renderSources();
    setText(elements["last-sync"], lastSuccess ? `Updated ${new Date(lastSuccess * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "");
  }

  async function request(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const headers = new Headers(options.headers || {});
      if (accessToken && new URL(url, location.href).origin === location.origin) headers.set("X-Agent-Board-Access", accessToken);
      const response = await fetch(url, { ...options, headers, cache: "no-store", signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `The monitor returned an error (${response.status}).`);
      return data;
    } finally { clearTimeout(timer); }
  }

  async function unlockAttentionAudio(preview = false) {
    if (!soundEnabled && !preview) return;
    try {
      audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
      if (audioContext.state !== "running") await audioContext.resume();
    } catch (_) { /* Audio may be unavailable in this browser. */ }
  }

  function unlockAudioAfterInteraction(event) {
    if (!event.isTrusted || event.target?.closest?.("#sound-toggle")) return;
    unlockAttentionAudio();
  }

  document.addEventListener("pointerdown", unlockAudioAfterInteraction, { capture: true, passive: true });
  document.addEventListener("keydown", unlockAudioAfterInteraction, { capture: true });

  async function playAttentionSound(preview = false) {
    if (!soundEnabled && !preview) return;
    if (!preview && window.webkit?.messageHandlers?.agentBoard
        && (document.visibilityState !== "visible" || !document.hasFocus())) return;
    const generation = ++soundGeneration;
    const selected = soundID;
    try {
      await unlockAttentionAudio(preview);
      if (audioContext.state !== "running") return;
      if (!soundBuffers.has(selected)) {
        soundBuffers.set(selected, fetch(`/sounds/${selected}.wav`, { credentials: "same-origin", cache: "force-cache" })
          .then(response => { if (!response.ok) throw new Error("Local sound unavailable"); return response.arrayBuffer(); })
          .then(bytes => audioContext.decodeAudioData(bytes))
          .catch(error => { soundBuffers.delete(selected); throw error; }));
      }
      const buffer = await soundBuffers.get(selected);
      if (generation !== soundGeneration || selected !== soundID || (!preview && !soundEnabled)) return;
      if (!preview && window.webkit?.messageHandlers?.agentBoard
          && (document.visibilityState !== "visible" || !document.hasFocus())) return;
      if (activeSound) activeSound.stop();
      const source = audioContext.createBufferSource();
      source.buffer = buffer;
      source.connect(audioContext.destination);
      source.onended = () => { source.disconnect(); if (activeSound === source) activeSound = null; };
      activeSound = source;
      source.start();
    } catch (_) { /* Audio is optional. */ }
  }

  function captureConversationFocus() {
    const active = document.activeElement;
    const row = active?.closest?.(".session[data-key]");
    if (!row || active.tagName !== "BUTTON" || pending.has(row.dataset.key)) return null;
    for (const kind of ["action", "menuAction", "menu", "preview"]) {
      if (active.dataset[kind] !== undefined) return { key: row.dataset.key, kind, value: active.dataset[kind] };
    }
    return null;
  }

  function focusConversationControl(previous) {
    const item = cards.get(previous.key);
    const usable = element => element?.isConnected && !element.disabled && element.getClientRects().length > 0;
    let target = previous.kind === "action" ? [item?.open, item?.review].find(button => button?.dataset.action === previous.value)
      : previous.kind === "menuAction" ? item?.menuItems[previous.value]
      : previous.kind === "menu" ? item?.menuToggle : item?.previewToggle;
    if (!usable(target)) target = [item?.open, item?.review, item?.menuToggle, queueItems.get(previous.key)?.button, elements[`${activeTab}-tab`]].find(usable);
    target?.focus({ preventScroll: true });
  }

  function restoreConversationFocus(previous) {
    // A refresh may move a row between sections. Restore only focus lost by
    // that render; never replace focus that still belongs to another control.
    if (!previous) return;
    const active = document.activeElement;
    const previousDisabled = active?.disabled && active.closest?.(".session[data-key]")?.dataset.key === previous.key
      && active.dataset[previous.kind] === previous.value;
    if (active !== document.body && active?.isConnected && !previousDisabled) return;
    focusConversationControl(previous);
  }

  function announceConversationCounts() {
    if (!state || !connected) return;
    const counts = Object.fromEntries(Object.keys(priority).map(status => [status,
      state.sessions.filter(session => visibleStatus(session) === status).length]));
    const changed = Object.keys(priority).filter(status => previousAnnouncedCounts !== null
      && previousAnnouncedCounts[status] !== counts[status]);
    if (changed.length && activeTab === "board"
        && document.visibilityState === "visible" && document.hasFocus()
        && !elements["rename-dialog"].open) {
      const phrases = {
        needs_you: ["conversation needs your input", "conversations need your input"],
        ready: ["conversation is ready to review", "conversations are ready to review"],
        cooking: ["conversation is cooking", "conversations are cooking"],
        disconnected: ["conversation is disconnected", "conversations are disconnected"]
      };
      setText(elements["conversation-updates"], changed.map(status =>
        `${counts[status]} ${phrases[status][counts[status] === 1 ? 0 : 1]}.`).join(" "));
    }
    previousAnnouncedCounts = counts;
  }

  async function refreshOnce() {
    try {
      const next = await request("/api/state");
      if (!next || !Array.isArray(next.sessions) || !Array.isArray(next.history) || !next.csrf_token) throw new Error("The monitor returned an incomplete update.");
      const nextStatuses = new Map(next.sessions.filter(session => !isSnoozed(session)).map(session => [session.key, `${session.event_id || ""}:${session.history_checking ? session.confirmed_status || session.status : session.status}`]));
      if (state && next.sessions.some(session => session.source_unavailable !== true && !session.history_checking && !session.reconfirmation_silent && session.notification_eligible !== false && !isSnoozed(session) && !pending.has(session.key) && ["ready", "needs_you"].includes(session.status) && previousStatuses.get(session.key) !== nextStatuses.get(session.key))) playAttentionSound();
      previousStatuses = nextStatuses;
      state = next;
      serverOffset = Number(next.now || Date.now() / 1000) - Date.now() / 1000;
      connected = true;
      lastSuccess = Date.now() / 1000;
    } catch (_) {
      connected = false;
      if (!state) {
        setText(elements["connection-text"], "Monitor disconnected");
        elements.connection.className = "connection offline";
        elements.loading.querySelector("p").textContent = "The local monitor is unavailable. Reconnecting automatically.";
      }
    } finally {
      attemptedConnection = true;
      const previousFocus = captureConversationFocus();
      render();
      restoreConversationFocus(previousFocus);
      announceConversationCounts();
    }
  }

  async function refresh(force = false) {
    while (polling) {
      await polling;
      if (!force) return;
    }
    const update = refreshOnce();
    polling = update;
    try { await update; } finally { if (polling === update) polling = null; }
  }

  function closeMenus(except = null) {
    for (const [key, item] of cards) {
      if (key === except) continue;
      item.menu.hidden = true;
      item.menuToggle.setAttribute("aria-expanded", "false");
      item.card.classList.remove("menu-open");
    }
  }

  function openMenu(key, focusFirst = false) {
    const item = cards.get(key);
    if (!item || item.menuToggle.disabled) return;
    closeMenus(key);
    item.menu.hidden = false;
    item.menuToggle.setAttribute("aria-expanded", "true");
    item.card.classList.add("menu-open");
    item.options.classList.remove("menu-above");
    item.options.classList.toggle("menu-above", item.menu.getBoundingClientRect().bottom > window.innerHeight - 12);
    if (focusFirst) [...item.menu.querySelectorAll("button")].find(button => !button.hidden && !button.disabled)?.focus();
  }

  function openRename(key) {
    const session = findSession(key);
    if (!session) return;
    closeMenus();
    renameKey = key;
    renameRevision += 1;
    elements["rename-input"].value = session.title_override || session.title || "";
    elements["rename-error"].hidden = true;
    elements["rename-dialog"].showModal();
    elements["rename-input"].focus();
    elements["rename-input"].select();
  }

  async function performAction(key, action, body = {}) {
    if (!state || !connected || pending.has(key)) return false;
    const focusedAtStart = document.activeElement;
    pending.add(key);
    let succeeded = false;
    elements["action-message"].hidden = true;
    render();
    try {
      const result = await request(`/api/sessions/${encodeURIComponent(key)}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Agent-Board-Token": state.csrf_token },
        body: JSON.stringify(body)
      });
      if (action === "open" && result.native_open_url) {
        const native = window.webkit?.messageHandlers?.agentBoard;
        if (!native) throw new Error("Open this conversation from the Agent Board Mac app.");
        native.postMessage({ kind: "openChat", provider: result.provider, url: result.native_open_url });
      }
      await refresh(true);
      succeeded = true;
    } catch (error) {
      setText(elements["action-message"], error.name === "AbortError" ? "The monitor did not respond. Please try again." : error.message || "The action could not be completed. Please try again.");
      elements["action-message"].hidden = false;
      await refresh(true);
    } finally {
      pending.delete(key);
      render();
      // Recover action focus after controls are enabled or a row moves.
      if (["open", "review", "restore"].includes(action)) {
        const active = document.activeElement;
        if (active === focusedAtStart || active === document.body || !active?.isConnected) {
          if (succeeded && action === "review") elements.history.querySelector("summary").focus({ preventScroll: true });
          else focusConversationControl({ key, kind: "action", value: succeeded && action === "restore" ? "open" : action });
        }
      }
    }
    return succeeded;
  }

  elements["sample-controls"].addEventListener("click", async event => {
    const button = event.target.closest("button[data-sample-action]");
    if (!button || !state?.sample_mode || !connected || samplePending) return;
    samplePending = true;
    elements["action-message"].hidden = true;
    render();
    try {
      await request("/api/samples/advance", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Agent-Board-Token": state.csrf_token },
        body: JSON.stringify({action:button.dataset.sampleAction})
      });
    } catch (error) {
      setText(elements["action-message"], error.name === "AbortError" ? "The sample board did not respond. Please try again." : error.message || "The sample action could not be completed.");
      elements["action-message"].hidden = false;
    } finally {
      samplePending = false;
      await refresh(true);
      render();
    }
  });

  document.addEventListener("click", async event => {
    const selection = event.target.closest("button[data-select]");
    if (selection) {
      closeMenus();
      selectedKey = selection.dataset.select;
      try { localStorage.setItem("agent-board:selected", selectedKey); } catch (_) { /* Optional preference. */ }
      render();
      return;
    }
    if (!event.target.closest(".appearance-menu")) elements["appearance-menu"].open = false;
    const previewButton = event.target.closest("button[data-preview]");
    if (previewButton) {
      const item = cards.get(previewButton.dataset.preview);
      if (!item) return;
      const expanded = previewButton.getAttribute("aria-expanded") !== "true";
      previewButton.setAttribute("aria-expanded", String(expanded));
      item.previewContent.hidden = !expanded;
      const session = findSession(previewButton.dataset.preview);
      previewButton.setAttribute("aria-label", `${expanded ? "Hide" : "Show"} ${session?.preview_kind === "permission" ? "request details" : "reply preview"}: ${item.title.textContent}`);
      return;
    }
    const menuToggle = event.target.closest("button[data-menu]");
    if (menuToggle && !menuToggle.disabled) {
      if (menuToggle.getAttribute("aria-expanded") === "true") closeMenus();
      else openMenu(menuToggle.dataset.menu, event.detail === 0);
      return;
    }
    const menuAction = event.target.closest("button[data-menu-action]");
    if (menuAction && !menuAction.disabled) {
      const key = menuAction.closest(".session").dataset.key;
      const action = menuAction.dataset.menuAction;
      closeMenus();
      if (action === "rename") { openRename(key); return; }
      if (action === "automatic-title") await performAction(key, "rename", { title: null });
      if (action === "move") await performAction(key, "move", { placement: menuAction.dataset.placement });
      if (action === "automatic-placement") await performAction(key, "move", { placement: "auto" });
      if (action === "snooze") await performAction(key, "snooze", { minutes: 10, event_id: findSession(key)?.event_id });
      const toggle = cards.get(key)?.menuToggle;
      if (toggle?.getClientRects().length) toggle.focus({ preventScroll: true });
      else elements[`${activeTab}-tab`].focus({ preventScroll: true });
      return;
    }
    if (!event.target.closest(".session-options")) closeMenus();
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled || !state) return;
    const key = button.closest(".session").dataset.key;
    const action = button.dataset.action;
    const session = findSession(key);
    await performAction(key, action, action === "review" ? { event_id: session?.event_id } : {});
  });

  document.addEventListener("contextmenu", event => {
    const selection = event.target.closest("button[data-select]");
    if (selection && !window.getSelection()?.toString()) {
      event.preventDefault();
      selectedKey = selection.dataset.select;
      try { localStorage.setItem("agent-board:selected", selectedKey); } catch (_) { /* Optional preference. */ }
      render();
      openMenu(selectedKey, true);
      return;
    }
    const card = event.target.closest(".session");
    if (!card || window.getSelection()?.toString()) return;
    event.preventDefault();
    openMenu(card.dataset.key, true);
  });

  document.addEventListener("keydown", event => {
    const selection = event.target.closest("button[data-select]");
    if (selection && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const buttons = [...elements["split-list"].children];
      const current = buttons.indexOf(selection);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next]?.click();
      buttons[next]?.focus();
      return;
    }
    if (event.key === "Escape") {
      if (elements["appearance-menu"].open) {
        elements["appearance-menu"].open = false;
        elements["appearance-menu"].querySelector("summary").focus();
      }
      const menu = event.target.closest(".session-menu");
      const item = menu ? cards.get(menu.closest(".session").dataset.key) : null;
      closeMenus();
      item?.menuToggle.focus();
      return;
    }
    const toggle = event.target.closest("button[data-menu]");
    if (toggle && event.key === "ArrowDown") {
      event.preventDefault();
      openMenu(toggle.dataset.menu, true);
      return;
    }
    const menu = event.target.closest(".session-menu");
    if (!menu || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const buttons = [...menu.querySelectorAll("button")].filter(button => !button.hidden && !button.disabled);
    const current = buttons.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  });

  elements["rename-cancel"].addEventListener("click", () => elements["rename-dialog"].close());
  elements["rename-dialog"].addEventListener("close", () => {
    const toggle = cards.get(renameKey)?.menuToggle;
    renameKey = null;
    renameRevision += 1;
    if (toggle?.getClientRects().length) toggle.focus({ preventScroll: true });
  });
  elements["rename-form"].addEventListener("submit", async event => {
    event.preventDefault();
    if (!renameKey) return;
    const key = renameKey;
    const revision = renameRevision;
    elements["rename-save"].disabled = true;
    const succeeded = await performAction(key, "rename", { title: elements["rename-input"].value.trim() || null });
    elements["rename-save"].disabled = false;
    if (revision !== renameRevision || renameKey !== key || !elements["rename-dialog"].open) return;
    if (succeeded) elements["rename-dialog"].close();
    else {
      setText(elements["rename-error"], elements["action-message"].textContent || "The title could not be saved. Please try again.");
      elements["rename-error"].hidden = false;
    }
  });

  for (const tab of ["board", "background"]) {
    elements[`${tab}-tab`].addEventListener("click", () => selectTab(tab));
    elements[`${tab}-tab`].addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? "board" : event.key === "End" ? "background" : tab === "board" ? "background" : "board";
      selectTab(next);
      elements[`${next}-tab`].focus();
    });
  }

  function renderSoundButton() {
    elements["sound-toggle"].title = soundEnabled ? "Mute sound" : "Unmute sound";
    elements["sound-toggle"].setAttribute("aria-pressed", String(soundEnabled));
  }

  function renderAppearance() {
    document.documentElement.dataset.layout = layout;
    appearance = themes.apply(appearance);
    elements["layout-select"].value = layout;
    elements["theme-select"].value = appearance.themeId;
    elements["mode-select"].value = appearance.mode;
    elements["sound-select"].value = soundID;
    elements["sound-preview"].setAttribute("aria-label", `Preview ${alertSounds.find(([id]) => id === soundID)[1]} alert sound`);
    elements["motion-option"].hidden = !themes.items.find(item => item.id === appearance.themeId)?.effect;
    elements["motion-toggle"].checked = appearance.motion && !reducedMotion.matches;
    elements["motion-toggle"].disabled = reducedMotion.matches;
    elements["motion-option"].title = reducedMotion.matches ? "macOS Reduce motion is enabled." : "";
  }

  for (const theme of themes.items) {
    const option = make("option", "", theme.name);
    option.value = theme.id;
    elements["theme-select"].append(option);
  }
  for (const [id, name] of alertSounds) {
    const option = make("option", "", name);
    option.value = id;
    elements["sound-select"].append(option);
  }
  elements["sound-select"].addEventListener("change", () => {
    soundID = alertSounds.some(([id]) => id === elements["sound-select"].value) ? elements["sound-select"].value : "soft-ding";
    soundGeneration += 1;
    if (activeSound) { activeSound.stop(); activeSound = null; }
    try { localStorage.setItem("agent-board:sound-id", soundID); } catch (_) { /* Optional preference. */ }
    renderAppearance();
  });
  elements["sound-preview"].addEventListener("click", () => { playAttentionSound(true); });

  elements["layout-select"].addEventListener("change", () => {
    layout = layouts.includes(elements["layout-select"].value) ? elements["layout-select"].value : "ledger";
    closeMenus();
    try { localStorage.setItem("agent-board:layout", layout); } catch (_) { /* Optional preference. */ }
    renderAppearance();
    render();
  });

  elements["theme-select"].addEventListener("change", () => {
    appearance.themeId = elements["theme-select"].value;
    themes.savePreferences(appearance);
    renderAppearance();
  });

  elements["mode-select"].addEventListener("change", () => {
    appearance.mode = elements["mode-select"].value;
    themes.savePreferences(appearance);
    renderAppearance();
  });

  elements["motion-toggle"].addEventListener("change", () => {
    appearance.motion = elements["motion-toggle"].checked;
    themes.savePreferences(appearance);
    renderAppearance();
  });
  reducedMotion.addEventListener("change", renderAppearance);

  elements["sound-toggle"].addEventListener("click", async () => {
    soundEnabled = !soundEnabled;
    soundGeneration += 1;
    if (!soundEnabled && activeSound) { activeSound.stop(); activeSound = null; }
    try { localStorage.setItem("agent-board:sound", soundEnabled ? "on" : "off"); } catch (_) { /* Optional preference. */ }
    renderSoundButton();
    if (soundEnabled) await unlockAttentionAudio();
  });

  elements["settings-toggle"].addEventListener("click", () => nativeBoard?.postMessage("settings"));
  elements["empty-samples"].addEventListener("click", () => {
    if (nativeBoard && connected && state?.store_profile === true && state?.sample_mode !== true && !elements["empty-samples"].hidden) {
      nativeBoard.postMessage("sampleBoard");
    }
  });

  function renderFullscreenButton(active) {
    const button = elements["fullscreen-toggle"];
    const label = active ? "Exit full screen" : "Full screen";
    button.dataset.fullscreen = String(active);
    button.title = label;
    button.setAttribute("aria-label", label);
  }

  elements["fullscreen-toggle"].hidden = !nativeBoard && !document.documentElement.requestFullscreen;
  elements["fullscreen-toggle"].addEventListener("click", async () => {
    try {
      if (nativeBoard) {
        nativeBoard.postMessage("fullscreen");
        return;
      }
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch (_) { /* The browser can decline fullscreen. */ }
  });
  document.addEventListener("fullscreenchange", () => renderFullscreenButton(Boolean(document.fullscreenElement)));
  window.addEventListener("agentboard:fullscreenchange", event => renderFullscreenButton(event.detail === true));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  window.addEventListener("online", () => refresh());
  renderAppearance();
  renderSoundButton();
  refresh();
  setInterval(refresh, 2000);
  setInterval(() => {
    document.querySelectorAll(".session-age").forEach(updateAge);
    renderUsage();
  }, 10000);
})();
