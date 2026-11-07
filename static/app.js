// Übersetzung: deutscher Text ist der Schlüssel, window.I18N kommt aus /i18n/<lang>.js.
// Aufruf mit Text und optionalen Werten; Platzhalter stehen in geschweiften Klammern.
window.t = function (text, values) {
  var catalog = window.I18N || {};
  var result = Object.prototype.hasOwnProperty.call(catalog, text) ? catalog[text] : text;
  if (values) {
    result = result.replace(/\{(\w+)\}/g, function (match, key) {
      return Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : match;
    });
  }
  return result;
};

// Sprache umschalten (pro Gerät, als Cookie für den Server).
document.addEventListener("click", function (event) {
  var button = event.target.closest && event.target.closest("#language-toggle");
  if (!button) return;
  document.cookie = "lang=" + button.dataset.lang + "; path=/; max-age=31536000; SameSite=Lax";
  window.location.reload();
});

// Übersprungene Einrichtung: Assistent erneut starten.
document.addEventListener("click", function (event) {
  var button = event.target.closest && event.target.closest("#setup-resume");
  if (!button) return;
  button.disabled = true;
  fetch("/api/setup/resume", { method: "POST" }).then(function (response) {
    window.location.href = response.ok ? "/einrichtung" : "/";
  }).catch(function () { button.disabled = false; });
});

// Darstellung umschalten: Automatisch → Hell → Dunkel → Automatisch.
(function () {
  var labels = { auto: t("Automatisch"), light: t("Hell"), dark: t("Dunkel") };
  var order = ["auto", "light", "dark"];

  function current() {
    return document.documentElement.dataset.theme || "auto";
  }

  function apply(theme) {
    if (theme === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    try {
      if (theme === "auto") localStorage.removeItem("theme");
      else localStorage.setItem("theme", theme);
    } catch (error) { /* nicht speicherbar: gilt nur für diese Seite */ }
    update();
  }

  function update() {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    var theme = current();
    button.querySelector(".theme-label").textContent = labels[theme];
    button.setAttribute("aria-label", t("Darstellung: {mode} – zum Wechseln tippen", { mode: labels[theme] }));
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest("#theme-toggle")) return;
    var next = order[(order.indexOf(current()) + 1) % order.length];
    apply(next);
  });
  document.addEventListener("DOMContentLoaded", update);
})();

// Ziehen zum Aktualisieren – nur als Vollbild-App vom Home-Bildschirm, weil
// dort die Browserfunktion fehlt. Im normalen Browser übernimmt das der Browser.
(function () {
  var standalone =
    window.navigator.standalone === true ||
    (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
  if (!standalone || !("ontouchstart" in window)) return;

  var THRESHOLD = 80; // Zugweite in px bis zum Auslösen
  var MAX_PULL = 130;
  var startY = null;
  var pull = 0;
  var indicator = null;

  function ensureIndicator() {
    if (indicator) return indicator;
    indicator = document.createElement("div");
    indicator.className = "pull-refresh";
    indicator.setAttribute("aria-hidden", "true");
    indicator.innerHTML =
      '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" ' +
      'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M12 4v13"/><path d="m6 11 6 6 6-6"/></svg><span></span>';
    document.body.appendChild(indicator);
    return indicator;
  }

  function show(distance, text, ready) {
    var element = ensureIndicator();
    element.style.transform = "translate(-50%, " + (distance - 60) + "px)";
    element.style.opacity = String(Math.min(1, distance / THRESHOLD));
    element.classList.toggle("ready", ready);
    element.querySelector("span").textContent = text;
  }

  function hide() {
    if (!indicator) return;
    indicator.style.transform = "translate(-50%, -60px)";
    indicator.style.opacity = "0";
    indicator.classList.remove("ready", "loading");
  }

  document.addEventListener("touchstart", function (event) {
    // Nur am Seitenanfang und nicht innerhalb scrollbarer Bereiche (z. B. Live-Log).
    if (window.scrollY > 0 || event.touches.length !== 1 || event.target.closest(".activity ol")) {
      startY = null;
      return;
    }
    startY = event.touches[0].clientY;
    pull = 0;
  }, { passive: true });

  document.addEventListener("touchmove", function (event) {
    if (startY === null) return;
    var distance = event.touches[0].clientY - startY;
    if (distance <= 0 || window.scrollY > 0) {
      pull = 0;
      hide();
      return;
    }
    pull = Math.min(MAX_PULL, distance * 0.6); // gedämpft wie das Original
    var ready = pull >= THRESHOLD;
    show(pull, ready ? t("Loslassen zum Aktualisieren") : t("Zum Aktualisieren ziehen"), ready);
  }, { passive: true });

  document.addEventListener("touchend", function () {
    if (startY === null) return;
    startY = null;
    if (pull < THRESHOLD) {
      hide();
      return;
    }
    if (document.querySelector(".inline-editor")) {
      show(THRESHOLD, t("Erst speichern oder abbrechen"), false);
      setTimeout(hide, 2200);
      return;
    }
    show(THRESHOLD, t("Wird aktualisiert …"), true);
    indicator.classList.add("loading");
    window.location.reload();
  }, { passive: true });

  document.addEventListener("touchcancel", function () {
    startY = null;
    hide();
  }, { passive: true });
})();

// Neue Server-Version → Seite neu laden. Nicht während ungespeicherter
// Eingaben; dann stattdessen ein Hinweis mit „Neu laden“.
(function () {
  var meta = document.querySelector('meta[name="app-version"]');
  if (!meta || !window.fetch) return;
  var pageVersion = meta.content;
  var CHECK_MS = 30000;
  var dirty = false;
  var bannerShown = false;

  // Geänderte Formularfelder (außer Filter/Suche) gelten als ungespeichert.
  document.addEventListener("input", function (event) {
    var form = event.target.closest && event.target.closest("form");
    if (form && !form.closest(".filter-bar") && form.method && form.method.toLowerCase() === "post") dirty = true;
  });
  document.addEventListener("submit", function () { dirty = false; });

  function hasUnsavedInput() {
    return dirty || Boolean(document.querySelector(".inline-editor"));
  }

  function showBanner(version) {
    if (bannerShown) return;
    bannerShown = true;
    var banner = document.createElement("div");
    banner.className = "update-banner";
    banner.setAttribute("role", "status");
    var text = document.createElement("span");
    text.textContent = t("Neue Version {version} verfügbar. Bitte zuerst speichern, dann neu laden.", { version: version });
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = t("Neu laden");
    button.addEventListener("click", function () { window.location.reload(); });
    banner.append(text, button);
    document.body.prepend(banner);
  }

  async function check() {
    try {
      var response = await fetch("/api/version", { cache: "no-store" });
      if (!response.ok) return; // z. B. während des Neustarts
      var data = await response.json();
      if (!data.version || data.version === pageVersion) return;
      if (hasUnsavedInput()) showBanner(data.version);
      else window.location.reload();
    } catch (error) { /* Server kurz nicht erreichbar: später erneut */ }
  }

  setInterval(check, CHECK_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") check();
  });
  window.__checkAppVersion = check; // für Tests
})();

// Info-Symbole: Erklärung direkt unter dem Symbol auf- und zuklappen.
(function () {
  function textFor(button) {
    var holder = button.closest(".info");
    return holder && holder.nextElementSibling && holder.nextElementSibling.classList.contains("info-text")
      ? holder.nextElementSibling
      : null;
  }
  function setOpen(button, open) {
    var text = textFor(button);
    if (!text) return;
    text.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest(".info-toggle");
    if (!button) return;
    event.preventDefault(); // z. B. innerhalb von <summary> nicht auf-/zuklappen
    setOpen(button, button.getAttribute("aria-expanded") !== "true");
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    document.querySelectorAll('.info-toggle[aria-expanded="true"]').forEach(function (button) {
      setOpen(button, false);
      if (document.activeElement && textFor(button) && textFor(button).contains(document.activeElement)) button.focus();
    });
  });
})();

// Einstellungen: Test-Benachrichtigung senden.
document.addEventListener("click", function (event) {
  var button = event.target.closest && event.target.closest("#notify-test");
  if (!button) return;
  var result = document.getElementById("notify-test-result");
  button.disabled = true;
  result.textContent = t("Sende …");
  fetch("/api/benachrichtigungen/test", { method: "POST" }).then(function (response) {
    return response.json().then(function (data) {
      if (!response.ok) throw new Error(data.detail || t("Aktion fehlgeschlagen."));
      result.textContent = Object.keys(data.results).map(function (name) {
        return name + ": " + (data.results[name] === "ok" ? t("gesendet") : data.results[name]);
      }).join(" · ");
    });
  }).catch(function (error) { result.textContent = error.message; }).then(function () { button.disabled = false; });
});
