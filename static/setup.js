// Einrichtungsassistent: Schritte, Verbindungsprüfungen, Abschluss.
"use strict";
(function () {
  const form = document.getElementById("setup-form");
  if (!form) return;
  const steps = [...form.querySelectorAll(".setup-step")];
  const progress = [...document.querySelectorAll(".setup-steps li")];
  const back = document.getElementById("setup-back");
  const next = document.getElementById("setup-next");
  const finish = document.getElementById("setup-finish");
  let current = 1;
  const checked = { imap: false, ai: false };
  let calendars = [];

  const value = (name) => (form.elements[name] ? form.elements[name].value.trim() : "");
  const result = (key) => form.querySelector(`.check-result[data-for="${key}"]`);

  function show(step) {
    current = step;
    steps.forEach((section) => { section.hidden = Number(section.dataset.step) !== step; });
    progress.forEach((item) => {
      const number = Number(item.dataset.step);
      item.classList.toggle("done", number < step);
      if (number === step) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
    });
    back.hidden = step === 1;
    next.hidden = step === steps.length;
    finish.hidden = step !== steps.length;
    if (step === steps.length) summary();
    const heading = steps[step - 1].querySelector("h2");
    heading.setAttribute("tabindex", "-1");
    heading.focus();
  }

  async function post(url, body) {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? t("Bitte alle Pflichtfelder ausfüllen.") : data.detail;
      throw new Error(detail || t("Aktion fehlgeschlagen."));
    }
    return data;
  }

  function report(key, text, ok) {
    const element = result(key);
    element.textContent = text;
    element.classList.toggle("ok", ok === true);
    element.classList.toggle("problem-text", ok === false);
  }

  function targets() {
    return [...document.querySelectorAll("#target-list li")]
      .filter((row) => row.querySelector(".target-pick").checked)
      .map((row) => ({
        name: row.dataset.name,
        description: row.querySelector(".target-description").value.trim(),
        shared: row.querySelector(".target-shared").checked,
      }));
  }

  const checks = {
    async imap(button) {
      report("imap", t("Prüfe …"));
      const data = await post("/api/setup/imap", {
        host: value("imap_host"), port: Number(value("imap_port") || 993),
        username: value("imap_username"), password: form.elements.imap_password.value, folder: value("imap_folder") || "INBOX",
      });
      document.getElementById("imap-folders").replaceChildren(...data.folders.map((name) => Object.assign(document.createElement("option"), { value: name })));
      checked.imap = true;
      report("imap", t("Verbunden. {count} Ordner gefunden.", { count: data.folders.length }), true);
    },
    async "ai-models"() {
      report("ai-models", t("Frage Modelle ab …"));
      const data = await post("/api/setup/ai-models", { base_url: value("ai_base_url"), api_key: form.elements.ai_api_key.value });
      const select = form.elements.ai_model;
      select.replaceChildren(...data.models.map((name) => Object.assign(document.createElement("option"), { value: name, textContent: name })));
      select.disabled = false;
      form.querySelector('[data-check="ai-probe"]').disabled = false;
      checked.ai = true;
      report("ai-models", t("{count} Modelle gefunden.", { count: data.models.length }), true);
    },
    async "ai-probe"() {
      const element = result("ai-probe");
      element.textContent = t("Die KI wertet eine Beispiel-Mail aus … Das kann eine Minute dauern.");
      const data = await post("/api/setup/ai-probe", {
        base_url: value("ai_base_url"), api_key: form.elements.ai_api_key.value, model: value("ai_model"),
        language: document.documentElement.lang, calendars: targets(),
      });
      element.replaceChildren();
      const intro = document.createElement("p");
      intro.className = "ok";
      intro.textContent = data.events.length
        ? t("Erkannt (die Paket-Zeile sollte fehlen):")
        : t("Die KI hat keinen Termin erkannt. Ein anderes Modell ausprobieren.");
      element.append(intro);
      const list = document.createElement("ul");
      for (const event of data.events) {
        const item = document.createElement("li");
        item.textContent = `${event.title} – ${event.when} (${event.confidence} %)`;
        list.append(item);
      }
      element.append(list);
    },
    async caldav() {
      report("caldav", t("Verbinde mit dem Kalender-Server …"));
      const data = await post("/api/setup/caldav", { url: value("caldav_url"), username: value("caldav_username"), password: form.elements.caldav_password.value });
      calendars = data.calendars;
      const list = document.querySelector("#target-list ul");
      list.replaceChildren(...calendars.map((calendar) => {
        const row = document.createElement("li");
        row.className = "target-row";
        row.dataset.name = calendar.name;
        row.innerHTML = `<label class="target-check"><input type="checkbox" class="target-pick" checked> <span class="cal-dot" aria-hidden="true"></span> <strong></strong></label>
          <label class="target-field">${t("Beschreibung für die KI")}<input type="text" class="target-description" maxlength="500"></label>
          <label class="target-options"><input type="checkbox" class="target-shared"> ${t("Gemeinsamer Kalender")}</label>`;
        row.querySelector("strong").textContent = calendar.name;
        if (calendar.color) row.querySelector(".cal-dot").style.setProperty("--cal-color", calendar.color);
        row.querySelector(".target-shared").checked = /^(familie|family|gemeinsam|haushalt|household|home)$/i.test(calendar.name);
        return row;
      }));
      document.getElementById("target-list").hidden = calendars.length === 0;
      report("caldav", t("{count} Kalender gefunden.", { count: calendars.length }), true);
    },
  };

  form.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-check]");
    if (!button) return;
    button.disabled = true;
    try {
      await checks[button.dataset.check](button);
    } catch (error) {
      report(button.dataset.check, error.message, false);
    } finally {
      button.disabled = false;
    }
  });

  function summary() {
    const rows = [
      [t("Sprache"), form.elements.language.selectedOptions[0].textContent],
      [t("Zeitzone"), value("timezone")],
      [t("Postfach"), `${value("imap_username")} @ ${value("imap_host")}:${value("imap_port") || 993} · ${value("imap_folder") || "INBOX"}`],
      [t("KI"), `${value("ai_model")} · ${value("ai_base_url")}`],
      [t("Kalender"), value("caldav_username") ? `${form.elements.caldav_provider.selectedOptions[0].textContent.trim()} · ${value("caldav_username")}` : t("nicht eingerichtet")],
      [t("Zielkalender"), targets().map((target) => target.name).join(", ") || "–"],
      [t("Zugangsschutz"), form.elements.app_password.value ? t("mit Passwort") : t("ohne Passwort")],
    ];
    const list = form.querySelector(".setup-summary");
    list.replaceChildren(...rows.flatMap(([label, text]) => [
      Object.assign(document.createElement("dt"), { textContent: label }),
      Object.assign(document.createElement("dd"), { textContent: text }),
    ]));
  }

  function validate(step) {
    if (step === 2 && !checked.imap) return t("Bitte zuerst die Verbindung zum Postfach prüfen.");
    if (step === 3 && (!checked.ai || !value("ai_model"))) return t("Bitte Modelle laden und ein Modell wählen.");
    return "";
  }

  next.addEventListener("click", () => {
    const problem = validate(current);
    const key = { 2: "imap", 3: "ai-models" }[current];
    if (problem) { if (key) report(key, problem, false); return; }
    show(current + 1);
  });
  back.addEventListener("click", () => show(current - 1));

  async function waitForRestart() {
    report("finish", t("Gespeichert. Die App startet mit den neuen Einstellungen neu …"), true);
    for (let attempt = 0; attempt < 90; attempt += 1) {
      await new Promise((resolve) => { setTimeout(resolve, 3000); });
      try {
        const response = await fetch("/api/setup/state", { cache: "no-store" });
        if (response.ok && !(await response.json()).setup_mode) { window.location.href = "/"; return; }
      } catch { /* App startet gerade */ }
    }
    report("finish", t("Die App meldet sich noch nicht zurück. Bitte auf dem Server docker compose ps und die Logs prüfen."), false);
  }

  finish.addEventListener("click", async () => {
    const appPassword = form.elements.app_password.value;
    if (appPassword && appPassword.length < 8) { report("finish", t("Das Passwort muss mindestens 8 Zeichen haben."), false); return; }
    finish.disabled = true;
    report("finish", t("Speichere …"));
    try {
      await post("/api/setup/finish", {
        language: value("language"), timezone: value("timezone"),
        imap_host: value("imap_host"), imap_port: Number(value("imap_port") || 993), imap_username: value("imap_username"),
        imap_password: form.elements.imap_password.value, imap_folder: value("imap_folder") || "INBOX",
        ai_base_url: value("ai_base_url"), ai_api_key: form.elements.ai_api_key.value, ai_model: value("ai_model"),
        caldav_url: value("caldav_url"), caldav_username: value("caldav_username"), caldav_password: form.elements.caldav_password.value,
        caldav_name: value("caldav_name"),
        app_password: form.elements.app_password.value,
        edit_without_password: !form.elements.app_password.value && form.elements.edit_without_password.checked,
        targets: targets(),
      });
      if (value("language") !== "auto") document.cookie = `lang=${value("language")}; path=/; max-age=31536000; SameSite=Lax`;
      await waitForRestart();
    } catch (error) {
      report("finish", error.message, false);
      finish.disabled = false;
    }
  });

  document.getElementById("setup-skip").addEventListener("click", async () => {
    if (!window.confirm(t("Einrichtung überspringen? Die App startet dann ohne Postfach und der Assistent erscheint nicht mehr. Zugangsdaten trägst du später unter Einstellungen → Verbindungen ein – oder startest den Assistenten über den Hinweis oben auf jeder Seite erneut."))) return;
    try {
      await post("/api/setup/skip", {});
      window.location.href = "/";
    } catch (error) {
      window.alert(error.message);
    }
  });

  form.elements.app_password.addEventListener("input", summary);
  // Mit Passwort ist Bearbeiten ohnehin möglich – der Haken gilt nur ohne.
  const withoutPassword = () => {
    document.getElementById("edit-without-password-row").hidden = Boolean(form.elements.app_password.value);
  };
  form.elements.app_password.addEventListener("input", withoutPassword);
  withoutPassword();

  // Kalender-Anbieter: Adresse, Anzeigename und Hinweis vorbelegen.
  form.elements.caldav_provider.addEventListener("change", () => {
    const option = form.elements.caldav_provider.selectedOptions[0];
    form.elements.caldav_url.value = option.dataset.url;
    form.elements.caldav_name.value = option.dataset.name;
    document.getElementById("caldav-hint").textContent = option.dataset.hint;
    if (!option.dataset.url || option.value === "nextcloud") form.elements.caldav_url.focus();
  });

  show(1);
})();
