(() => {
  "use strict";

  const variants = ["ledger", "split", "columns", "focus", "instrument"];
  const names = {ledger: "Ledger", split: "Split desk", columns: "Columns", focus: "Focus", instrument: "Instrument"};
  const chats = [
    {id: "notifications", title: "Mac app notification actions", provider: "Codex", project: "Agent Board", status: "ready", label: "Ready", waiting: "47 minutes", compact: "47m", activity: "Waiting for you for 47 minutes.", preview: "Added native notifications with Open chat and Snooze actions. The menu bar now shows the number of unreviewed conversations.\n\nRenaming a conversation and moving it to Background both persist after restarting the app.", time: "12:14 AM"},
    {id: "simulator", title: "Check the simulator layout", provider: "Claude", project: "Sample app", status: "needs", label: "Needs you", waiting: "22 minutes", compact: "22m", activity: "Waiting for your permission for 22 minutes.", preview: "Claude is waiting for permission to capture the simulator screen before checking the updated layout.", time: "12:39 AM"},
    {id: "archive", title: "Formula archive search", provider: "Codex", project: "Bureau Obscura", status: "cooking", label: "Cooking", waiting: "12 minutes", compact: "12m", activity: "Started 12 minutes ago.", preview: "Checking how the archive handles material names and formula revisions.", time: "12:49 AM"},
    {id: "studio", title: "Studio booking flow", provider: "Claude", project: "Spacely Digital", status: "cooking", label: "Cooking", waiting: "7 minutes", compact: "7m", activity: "Started 7 minutes ago.", preview: "Reviewing the booking form and confirmation screen.", time: "12:54 AM"}
  ];
  const usage = [
    {provider: "Claude", window: "5 hour", remaining: 86, reset: "3h 12m"},
    {provider: "Claude", window: "Weekly", remaining: 93, reset: "6d 11h"},
    {provider: "Codex", window: "Weekly", remaining: 24, reset: "5d 9h"}
  ];
  const stage = document.querySelector("#design-stage");
  let selectedChat = chats[0].id;
  let active = "ledger";
  const esc = text => String(text).replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[character]);
  const disabled = 'disabled title="Visual preview only"';
  const button = (label, className = "") => `<button class="action ${className}" type="button" ${disabled}>${label}</button>`;
  const actions = chat => `<div class="actions">${button(`Open ${chat.provider}`)}${chat.status === "ready" || chat.status === "needs" ? button("Reviewed", "primary") : ""}${button("···", "more")}</div>`;
  const badge = chat => `<span class="badge ${chat.status}"><i aria-hidden="true"></i>${chat.label}</span>`;
  const meta = chat => `<span class="provider">${chat.provider}</span><span class="meta-slash" aria-hidden="true">/</span><span>${chat.project}</span>`;
  const tabs = () => `<div class="board-tabs" aria-label="Preview conversation tabs"><button class="selected" type="button" ${disabled}>Board <span>4</span></button><button type="button" ${disabled}>Background <span>8</span></button></div>`;
  const connection = () => '<span class="connection"><i aria-hidden="true"></i>Connected locally</span>';
  const controls = () => `<div class="board-controls">${button("Light")}${button("Notifications on")}</div>`;
  const usageWindows = (className = "") => `<section class="usage ${className}" aria-label="Example remaining usage">${usage.map(item => `<div class="usage-item"><div class="usage-name"><strong>${item.provider}</strong><span>${item.window}</span></div><div class="usage-amount"><strong>${item.remaining}<span>%</span></strong><span>remaining</span></div><div class="usage-meter" aria-hidden="true"><span class="meter-${item.remaining}"></span></div><p>Resets in ${item.reset}</p></div>`).join("")}</section>`;
  const footer = () => '<footer class="board-footer"><div><span><i aria-hidden="true"></i>Codex</span><span><i aria-hidden="true"></i>Claude</span></div><span>Updated just now</span></footer>';
  const counts = () => '<div class="counts"><span><b>2</b> waiting for you</span><span><b>2</b> cooking</span></div>';
  const history = () => `<div class="reviewed-row"><span>Reviewed <span>12</span></span><span aria-hidden="true">⌄</span></div>`;
  const heading = () => `<header class="board-heading"><div class="title-lockup"><h2>Agent Board</h2>${connection()}</div>${controls()}</header>`;
  const card = (chat, extraClass = "") => `<article class="chat-card ${chat.status} ${extraClass}"><div class="card-top">${badge(chat)}<span class="time">${chat.status === "cooking" ? `Running ${chat.compact}` : `Waiting ${chat.compact}`}</span></div><h3>${esc(chat.title)}</h3><div class="chat-meta">${meta(chat)}</div><p class="card-preview">${esc(chat.preview.split("\n")[0])}</p>${actions(chat)}</article>`;

  function ledger() {
    return `<section class="board ledger" aria-label="Ledger design">${heading()}<div class="board-subhead">${tabs()}${counts()}</div>${usageWindows()}<div class="ledger-table" role="table" aria-label="Example conversations"><div class="ledger-labels" role="row"><span role="columnheader">Conversation</span><span role="columnheader">Status</span><span role="columnheader">Waiting</span><span role="columnheader">Actions</span></div>${chats.map(chat => `<article class="ledger-row ${chat.status}" role="row"><div class="ledger-conversation" role="cell"><h3>${esc(chat.title)}</h3><div class="chat-meta">${meta(chat)}</div>${chat.status === "ready" ? `<p class="ledger-preview">${esc(chat.preview.split("\n")[0])}</p>` : ""}</div><div role="cell">${badge(chat)}</div><div class="ledger-wait" role="cell">${chat.compact}<span>${chat.status === "cooking" ? "running" : "waiting"}</span></div><div role="cell">${actions(chat)}</div></article>`).join("")}</div>${history()}${footer()}</section>`;
  }

  function detail(chat) {
    return `<div class="detail-head">${badge(chat)}<span class="time">${chat.time}</span></div><h3>${esc(chat.title)}</h3><div class="chat-meta">${meta(chat)}</div><p class="detail-wait">${chat.activity}</p><div class="detail-preview"><h4>${chat.status === "cooking" ? "Latest activity" : chat.status === "needs" ? "Permission request" : "Handoff"}</h4><p>${esc(chat.preview)}</p></div>${actions(chat)}`;
  }

  function split() {
    const selected = chats.find(chat => chat.id === selectedChat) || chats[0];
    return `<section class="board split" aria-label="Split desk design">${heading()}<div class="board-subhead">${tabs()}${counts()}</div><div class="split-workspace"><div class="split-queue"><h3 class="pane-heading">Conversations</h3><div class="split-list">${chats.map(chat => `<button class="split-item ${chat.status} ${chat.id === selected.id ? "is-selected" : ""}" type="button" data-chat="${chat.id}" aria-pressed="${chat.id === selected.id}"><span class="split-row-top">${badge(chat)}<span class="time">${chat.compact}</span></span><strong>${esc(chat.title)}</strong><span class="chat-meta">${meta(chat)}</span></button>`).join("")}</div>${history()}</div><article class="split-detail" aria-label="Selected conversation">${detail(selected)}</article></div>${usageWindows("split-usage")}${footer()}</section>`;
  }

  function columns() {
    return `<section class="board columns" aria-label="Columns design">${heading()}<div class="board-subhead">${tabs()}${counts()}</div><div class="column-workspace"><section class="status-column"><h3 class="column-heading">Waiting for you <span>02</span></h3>${chats.filter(chat => chat.status !== "cooking").map(chat => card(chat)).join("")}</section><section class="status-column"><h3 class="column-heading">Cooking <span>02</span></h3>${chats.filter(chat => chat.status === "cooking").map(chat => card(chat)).join("")}</section></div><div class="columns-bottom">${usageWindows()}${history()}</div>${footer()}</section>`;
  }

  function focus() {
    const oldest = chats[0];
    return `<section class="board focus" aria-label="Focus design">${heading()}<div class="board-subhead">${tabs()}${counts()}</div><div class="focus-workspace"><article class="focus-primary"><div class="focus-title"><h3>${esc(oldest.title)}</h3>${badge(oldest)}</div><div class="chat-meta">${meta(oldest)}</div><div class="focus-duration"><strong>47<span>m</span></strong><p>This response is the oldest<br>waiting for your review.</p></div><div class="focus-handoff"><p>${esc(oldest.preview.split("\n")[0])}</p></div>${actions(oldest)}</article><section class="focus-queue" aria-labelledby="other-heading"><h3 id="other-heading" class="pane-heading">Other conversations <span>3</span></h3>${chats.slice(1).map(chat => `<article class="focus-small ${chat.status}"><div class="card-top">${badge(chat)}<span class="time">${chat.compact}</span></div><h4>${esc(chat.title)}</h4><div class="chat-meta">${meta(chat)}</div>${actions(chat)}</article>`).join("")}</section></div>${usageWindows()}${history()}${footer()}</section>`;
  }

  function instrument() {
    return `<section class="board instrument" aria-label="Instrument design"><aside class="instrument-sidebar"><h2>Agent<br>Board</h2>${connection()}<div class="instrument-nav"><div class="instrument-nav-item active">Board <span>04</span></div><div class="instrument-nav-item">Background <span>08</span></div><div class="instrument-nav-item">Reviewed <span>12</span></div></div><div class="instrument-counts"><div><span>Waiting for you</span><strong>02</strong></div><div><span>Cooking</span><strong>02</strong></div></div><div class="instrument-side-bottom"><span><i aria-hidden="true"></i>Both sources are connected.</span>${button("Notifications on")}${button("Light mode")}</div></aside><div class="instrument-main"><header class="instrument-heading"><h3>Conversations</h3><span>Updated just now</span></header><div class="instrument-table"><div class="instrument-labels"><span>Conversation</span><span>Status</span><span>Elapsed</span><span></span></div>${chats.map((chat, index) => `<article class="instrument-row ${chat.status}"><div class="instrument-name"><span class="row-number" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span><div><h4>${esc(chat.title)}</h4><div class="chat-meta">${meta(chat)}</div></div></div><div>${badge(chat)}</div><div class="instrument-elapsed">${chat.compact}</div><div>${button("↗", "icon-open")}<span class="sr-only">Open ${chat.provider}</span></div>${chat.status === "ready" ? `<div class="instrument-handoff"><p>${esc(chat.preview.split("\n")[0])}</p><div class="actions">${button("Reviewed", "primary")}${button("Snooze 10 minutes")}${button("···", "more")}</div></div>` : ""}</article>`).join("")}</div><div class="instrument-main-bottom">${usageWindows()}</div></div></section>`;
  }

  const renderers = {ledger, split, columns, focus, instrument};
  function render(variant, updateHash = true) {
    active = variants.includes(variant) ? variant : "ledger";
    stage.innerHTML = renderers[active]();
    stage.dataset.design = active;
    document.querySelectorAll("[data-design]").forEach(button => {
      if (button.dataset.design === active) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    });
    document.querySelector("#design-announcement").textContent = `${names[active]} design selected.`;
    document.title = `${names[active]} · Agent Board designs`;
    if (updateHash && location.hash !== `#${active}`) window.history.replaceState(null, "", `#${active}`);
  }

  function step(direction) { render(variants[(variants.indexOf(active) + direction + variants.length) % variants.length]); }
  document.querySelectorAll("[data-design]").forEach(button => button.addEventListener("click", () => render(button.dataset.design)));
  document.querySelector("#previous-design").addEventListener("click", () => step(-1));
  document.querySelector("#next-design").addEventListener("click", () => step(1));
  document.querySelector(".design-picker").addEventListener("keydown", event => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight" && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    if (event.key === "Home") render(variants[0]);
    else if (event.key === "End") render(variants[variants.length - 1]);
    else step(event.key === "ArrowRight" ? 1 : -1);
    document.querySelector(`[data-design="${active}"]`).focus();
  });
  stage.addEventListener("click", event => {
    const selected = event.target.closest("[data-chat]");
    if (!selected) return;
    selectedChat = selected.dataset.chat;
    render("split", false);
    stage.querySelector(`[data-chat="${selectedChat}"]`).focus();
  });
  addEventListener("hashchange", () => render(location.hash.slice(1), false));
  render(location.hash.slice(1), false);
})();
