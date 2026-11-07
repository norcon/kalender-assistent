// Einstellungen → Verbindungen: prüfen, speichern, Neustart abwarten.
"use strict";
(function () {
  function collect(form) {
    if (form.dataset.section === "postfaecher") {
      const mailboxes = [...form.querySelectorAll("fieldset.mailbox")].map((fieldset) => {
        const entry = { orig: fieldset.dataset.orig || null };
        for (const input of fieldset.querySelectorAll("[data-field]")) {
          entry[input.dataset.field] = input.type === "checkbox" ? input.checked : input.value;
        }
        return entry;
      });
      return { mailboxes };
    }
    const data = {};
    for (const element of form.elements) {
      if (!element.name) continue;
      data[element.name] = element.type === "checkbox" ? element.checked : element.value;
    }
    return data;
  }

  async function waitForRestart(result) {
    result.textContent = t("Gespeichert. Die App startet mit den neuen Verbindungen neu …");
    await new Promise((resolve) => { setTimeout(resolve, 4000); });
    for (let attempt = 0; attempt < 60; attempt += 1) {
      try {
        const response = await fetch("/api/version", { cache: "no-store" });
        if (response.ok) { window.location.reload(); return; }
      } catch { /* startet noch */ }
      await new Promise((resolve) => { setTimeout(resolve, 2000); });
    }
    result.textContent = t("Die App meldet sich noch nicht zurück. Bitte auf dem Server docker compose ps und die Logs prüfen.");
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest(".connection-form");
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const result = form.querySelector(".check-result");
    button.disabled = true;
    result.className = "check-result";
    result.textContent = t("Prüfe Verbindung …");
    try {
      const response = await fetch(`/api/verbindungen/${form.dataset.section}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collect(form)),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || t("Aktion fehlgeschlagen."));
      if (data.status === "unchanged") {
        result.textContent = t("Keine Änderung.");
        button.disabled = false;
        return;
      }
      result.classList.add("ok");
      await waitForRestart(result);
    } catch (error) {
      result.classList.add("problem-text");
      result.textContent = error.message;
      button.disabled = false;
    }
  });

  const add = document.getElementById("add-mailbox");
  if (add) {
    add.addEventListener("click", () => {
      const list = document.querySelector(".mailbox-list");
      list.append(document.getElementById("mailbox-template").content.cloneNode(true));
      list.lastElementChild.querySelector('[data-field="host"]').focus();
      add.hidden = list.children.length >= 9;
    });
  }

  const cleanup = document.getElementById("cleanup-env");
  if (cleanup) {
    cleanup.addEventListener("click", async () => {
      cleanup.disabled = true;
      const response = await fetch("/api/verbindungen-aufraeumen", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (response.ok && !(data.pending || []).length) { window.location.reload(); return; }
      cleanup.disabled = false;
      cleanup.insertAdjacentText("afterend", " " + (data.message || t("Aktion fehlgeschlagen.")));
    });
  }
})();
