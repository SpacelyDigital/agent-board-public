(() => {
  "use strict";

  const themes = window.AgentBoardThemes.items;
  const themeSelect = document.querySelector("#gallery-theme");
  const modeSelect = document.querySelector("#gallery-mode");
  const motionInput = document.querySelector("#gallery-motion");
  const motionLabel = document.querySelector("#gallery-motion-label");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const chats = [
    {id: "notifications", title: "Mac app notification actions", provider: "Codex", project: "Agent Board", status: "ready", label: "Ready", age: "Waiting 47m", preview: "Added native notifications with Open chat and Snooze actions. The menu bar now shows the number of unreviewed conversations.\n\nRenaming a conversation and moving it to Background both persist after restarting the app."},
    {id: "simulator", title: "Check the simulator layout", provider: "Claude", project: "Sample app", status: "needs_you", label: "Needs you", age: "Waiting 22m", preview: "Claude is waiting for permission to capture the simulator screen before checking the updated layout."},
    {id: "archive", title: "Formula archive search", provider: "Codex", project: "Bureau Obscura", status: "cooking", label: "Cooking", age: "Running 12m", preview: "Checking how the archive handles material names and formula revisions."},
    {id: "studio", title: "Studio booking flow", provider: "Claude", project: "Spacely Digital", status: "cooking", label: "Cooking", age: "Running 7m", preview: "Reviewing the booking form and confirmation screen."}
  ];
  const usage = [
    {provider: "Claude", remaining: 93, reset: "6d 11h", credits: "$25.00"},
    {provider: "Codex", remaining: 24, reset: "5d 9h", credits: "240"}
  ];
  const preview = document.querySelector("#color-preview");
  const escape = value => String(value).replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[character]);
  let active = {themeId: "terminal", mode: "dark", motion: true};

  themes.forEach(theme => {
    const option = document.createElement("option");
    option.value = theme.id;
    option.textContent = theme.name;
    themeSelect.append(option);
  });

  function card(chat, index) {
    return `<article class="session" data-status="${chat.status}">
      <div class="session-main">
        <h3 class="session-title">${escape(chat.title)}</h3>
        <div class="session-meta"><span class="provider">${chat.provider}</span><span class="meta-separator" aria-hidden="true">/</span><span class="project">${escape(chat.project)}</span><button class="preview-toggle" type="button" data-preview="${chat.id}" aria-expanded="${index === 0}" aria-controls="preview-${chat.id}">${index === 0 ? "Hide preview" : "Preview"}</button></div>
        <div id="preview-${chat.id}" class="preview-content" ${index === 0 ? "" : "hidden"}><p class="preview-text">${escape(chat.preview)}</p></div>
      </div>
      <div class="session-side">
        <div class="session-state"><span class="status-badge" data-status="${chat.status}">${chat.label}</span><span class="session-age">${chat.age}</span></div>
        <div class="session-actions" aria-label="Example inactive chat actions"><span class="action-button">Open ${chat.provider}</span>${chat.status !== "cooking" ? '<span class="action-button review-button">Reviewed</span>' : ""}<span class="action-button menu-toggle" aria-hidden="true">···</span></div>
      </div>
    </article>`;
  }

  function providerUsage(provider) {
    const id = provider.toLowerCase();
    const example = usage.find(item => item.provider === provider);
    const mark = document.documentElement.dataset.providerMarks === "off" ? ""
      : `<span class="usage-logo usage-logo-${id}" aria-hidden="true"></span>`;
    return `<section class="usage-provider" data-provider="${id}" aria-labelledby="example-${id}-usage-heading">
      <h2 id="example-${id}-usage-heading" class="usage-provider-heading">${mark}${provider}</h2>
      <div class="usage-windows">
        <div class="usage-window" aria-label="${provider} weekly usage"><div class="usage-top"><span class="usage-label">Weekly</span><span class="usage-value">${example.remaining}% remaining</span></div><div class="usage-bottom"><span class="usage-reset">Resets in ${example.reset}</span></div></div>
        <div class="usage-window usage-credits" aria-label="${provider} credits available"><div class="usage-top"><span class="usage-label">Credits available</span><span class="usage-value">${example.credits}</span></div><div class="usage-bottom"><span class="usage-freshness">Sample balance</span></div></div>
      </div>
    </section>`;
  }

  preview.innerHTML = `<header class="header"><div class="heading"><h2>Agent Board</h2><div class="connection live"><span class="connection-dot" aria-hidden="true"></span><span>Connected locally</span></div></div><div class="board-tabs"><span class="board-tab" aria-selected="true">Board</span><span class="board-tab">Background <span class="tab-count">8</span></span></div><span id="preview-color-label" class="preview-color-label">Bureau green</span></header>
    <section class="usage" aria-label="Example account usage">${["Claude", "Codex"].map(providerUsage).join("")}</section>
    <div class="summary" aria-label="Example conversation counts"><div class="summary-item needs-you"><span class="summary-count">1</span><span class="summary-label"><span class="status-dot" aria-hidden="true"></span>Needs you</span></div><div class="summary-item ready"><span class="summary-count">1</span><span class="summary-label"><span class="status-dot" aria-hidden="true"></span>Ready</span></div><div class="summary-item cooking"><span class="summary-count">2</span><span class="summary-label"><span class="status-dot" aria-hidden="true"></span>Cooking</span></div><div class="summary-item disconnected"><span class="summary-count">0</span><span class="summary-label"><span class="status-dot" aria-hidden="true"></span>Disconnected</span></div></div>
    <div class="active-workspace"><div class="layout-sections"><section class="session-section" aria-labelledby="example-attention-heading"><div class="section-heading"><h2 id="example-attention-heading">Needs your attention</h2><span class="section-count">2</span></div><div class="session-list">${chats.slice(0, 2).map(card).join("")}</div></section><section class="session-section" aria-labelledby="example-cooking-heading"><div class="section-heading"><h2 id="example-cooking-heading">Cooking</h2><span class="section-count">2</span></div><div class="session-list">${chats.slice(2).map((chat, index) => card(chat, index + 2)).join("")}</div></section></div></div>
    <div class="reviewed-preview"><span>Reviewed <span>12</span></span><span aria-hidden="true">⌄</span></div>
    <footer class="footer"><div class="sources"><span class="source available"><span class="source-dot" aria-hidden="true"></span><span class="source-name">Codex</span><span>connected</span></span><span class="source available"><span class="source-dot" aria-hidden="true"></span><span class="source-name">Claude</span><span>connected</span></span></div><span class="last-sync">Updated just now</span></footer>`;

  function fromHash() {
    const hash = location.hash.slice(1).toLowerCase();
    const match = /^(.*)-(light|dark)$/.exec(hash);
    const themeId = match ? match[1] : hash;
    if (!themes.some(theme => theme.id === themeId)) return {themeId: "terminal", mode: "dark"};
    return {themeId, mode: match ? match[2] : themeId === "paper" ? "light" : "dark"};
  }

  function render(selection) {
    const theme = themes.find(item => item.id === selection.themeId) || themes[0];
    active = {themeId: theme.id, mode: selection.mode === "light" ? "light" : "dark", motion: selection.motion ?? active.motion};
    window.AgentBoardThemes.apply(active);
    themeSelect.value = active.themeId;
    modeSelect.value = active.mode;
    motionInput.checked = active.motion && !reducedMotion.matches;
    motionInput.disabled = reducedMotion.matches;
    motionLabel.hidden = !theme.effect;
    motionLabel.title = reducedMotion.matches ? "macOS Reduce motion is enabled." : "";
    const label = `${theme.name} · ${active.mode === "light" ? "Light" : "Dark"}`;
    document.querySelector("#preview-color-label").textContent = label;
    document.querySelector("#color-announcement").textContent = `${theme.name} ${active.mode} preview selected.`;
    preview.setAttribute("aria-label", `${theme.name} ${active.mode} preview`);
    document.title = `${label} · Agent Board themes`;
    const hash = `#${active.themeId}-${active.mode}`;
    if (location.hash !== hash) window.history.replaceState(null, "", hash);
  }

  document.querySelector(".theme-picker").addEventListener("submit", event => event.preventDefault());
  themeSelect.addEventListener("change", () => render({...active, themeId: themeSelect.value}));
  modeSelect.addEventListener("change", () => render({...active, mode: modeSelect.value}));
  motionInput.addEventListener("change", () => render({...active, motion: motionInput.checked}));
  reducedMotion.addEventListener("change", () => render(active));
  preview.addEventListener("click", event => {
    const button = event.target.closest("[data-preview]");
    if (!button) return;
    const content = document.getElementById(button.getAttribute("aria-controls"));
    content.hidden = !content.hidden;
    button.setAttribute("aria-expanded", String(!content.hidden));
    button.textContent = content.hidden ? "Preview" : "Hide preview";
  });
  addEventListener("hashchange", () => render({...active, ...fromHash()}));
  render({...active, ...fromHash()});
})();
