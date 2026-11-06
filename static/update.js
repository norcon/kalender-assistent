// Version und Updates: Abschnitt nachladen, Update anstoßen, Fortschritt verfolgen.
"use strict";
(function () {
  const container = document.getElementById("update-container");
  if (!container) return;
  let polling = false;

  async function load() {
    try {
      const response = await fetch(container.dataset.src, { cache: "no-store" });
      if (!response.ok) throw new Error();
      container.innerHTML = await response.text();
      const running = container.querySelector(".update-progress");
      if (running && !polling) poll();
    } catch {
      container.innerHTML = `<p class="meta">${t("Versionsinformationen sind gerade nicht verfügbar.")}</p>`;
    }
  }

  function feedback(text, problem = false) {
    const element = container.querySelector(".update-feedback");
    if (!element) return;
    element.textContent = text;
    element.classList.toggle("problem-text", problem);
  }

  async function poll() {
    polling = true;
    const deadline = Date.now() + 15 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => { setTimeout(resolve, 4000); });
      try {
        const response = await fetch("/api/update/status", { cache: "no-store" });
        if (!response.ok) throw new Error();
        const data = await response.json();
        const state = data.updater && data.updater.state;
        if (state !== "running") { polling = false; window.location.reload(); return; }
        const log = container.querySelector(".update-log");
        if (log && data.updater.log) {
          log.replaceChildren(...data.updater.log.slice(-8).map((line) => {
            const item = document.createElement("li");
            item.textContent = line;
            return item;
          }));
        }
      } catch {
        feedback(t("Die App startet gerade neu …"));
      }
    }
    polling = false;
  }

  container.addEventListener("click", async (event) => {
    const button = event.target.closest(".update-button");
    if (!button || button.disabled) return;
    const rollback = Boolean(button.dataset.rollback);
    const version = rollback ? container.querySelector("#rollback-version").value : button.dataset.version;
    const restore = rollback && container.querySelector("#rollback-restore").checked;
    if (!confirm(button.dataset.confirm)) return;
    container.querySelectorAll(".update-button").forEach((item) => { item.disabled = true; });
    feedback(t("Sicherung wird angelegt und Update gestartet …"));
    try {
      const response = await fetch("/api/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version, restore_data: restore }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || t("Update konnte nicht gestartet werden."));
      feedback(t("Update auf {version} läuft. Die Seite lädt danach von selbst.", { version }));
      poll();
    } catch (error) {
      feedback(error.message, true);
      container.querySelectorAll(".update-button").forEach((item) => { item.disabled = false; });
    }
  });

  load();
})();
