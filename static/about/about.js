(() => {
  "use strict";
  const themes = window.AgentBoardThemes;
  if (themes) themes.apply({ ...themes.readPreferences(), motion: false });

  function showLinkedDetails() {
    if (window.location.hash !== "#privacy") return;
    const details = document.getElementById("privacy");
    if (details) details.open = true;
  }

  showLinkedDetails();
  window.addEventListener("hashchange", showLinkedDetails);
})();
