(() => {
  "use strict";

  const items = Object.freeze([
    { id: "terminal", name: "Bureau green", effect: null },
    { id: "graphite", name: "Graphite", effect: null },
    { id: "paper", name: "Paper", effect: null },
    { id: "amber", name: "Amber", effect: null },
    { id: "arctic", name: "Arctic", effect: null },
    { id: "orchid", name: "Orchid", effect: null },
    { id: "moss", name: "Moss", effect: null },
    { id: "rose", name: "Rose", effect: null },
    { id: "matrix", name: "Matrix", effect: "matrix" },
    { id: "hearth", name: "Fireplace", effect: "hearth" },
    { id: "aurora", name: "Aurora", effect: "aurora" },
    { id: "starfield", name: "Deep space", effect: "starfield" },
    { id: "aquarium", name: "Aquarium", effect: "aquarium" },
    { id: "lava", name: "Lava lamp", effect: "lava" },
    { id: "rain", name: "Rainy window", effect: "rain" },
  ].map(item => Object.freeze(item)));

  const ids = new Set(items.map(item => item.id));
  const legacyIds = new Set(["terminal", "amber", "arctic", "orchid", "paper"]);
  const modes = new Set(["light", "dark"]);

  function normalize(preferences = {}) {
    const value = preferences && typeof preferences === "object" ? preferences : {};
    return {
      themeId: ids.has(value.themeId) ? value.themeId : "terminal",
      mode: modes.has(value.mode) ? value.mode : "dark",
      motion: value.motion !== false,
    };
  }

  function stored(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }

  function readPreferences() {
    const themeId = stored("agent-board:theme-id");
    const mode = stored("agent-board:mode");
    const motion = stored("agent-board:motion") !== "false";

    if (themeId !== null || mode !== null) return normalize({ themeId, mode, motion });

    // The first appearance picker combined the theme and mode in one choice.
    const oldPalette = stored("agent-board:palette");
    const oldMode = stored("agent-board:theme");
    return normalize({
      themeId: legacyIds.has(oldPalette) ? oldPalette : "terminal",
      mode: oldPalette === "paper" ? "light" : oldMode,
      motion,
    });
  }

  function apply(preferences) {
    const next = normalize(preferences);
    const item = items.find(theme => theme.id === next.themeId);
    const root = document.documentElement;
    root.dataset.palette = next.themeId;
    root.dataset.theme = next.mode;
    if (item.effect) root.dataset.effect = item.effect;
    else delete root.dataset.effect;
    root.dataset.motion = String(next.motion);

    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) {
      const background = getComputedStyle(root).getPropertyValue("--paper").trim();
      themeColor.content = background || (next.mode === "dark" ? "#050805" : "#edf4e9");
    }
    try {
      window.webkit?.messageHandlers?.agentBoard?.postMessage(`theme:${next.mode}`);
    } catch (_) { /* The native window is optional in a regular browser. */ }

    window.AgentBoardEffects?.configure({ effect: item.effect, ...next });
    return next;
  }

  function savePreferences(preferences) {
    const next = normalize(preferences);
    try {
      localStorage.setItem("agent-board:theme-id", next.themeId);
      localStorage.setItem("agent-board:mode", next.mode);
      localStorage.setItem("agent-board:motion", String(next.motion));
      localStorage.setItem("agent-board:theme", next.mode);
    } catch (_) { /* This session still works when storage is unavailable. */ }
    return next;
  }

  window.AgentBoardThemes = Object.freeze({ items, readPreferences, apply, savePreferences });
})();
