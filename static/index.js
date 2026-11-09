// Startseite: Vorschläge bearbeiten, übertragen, filtern, auswählen.
// Werte vom Server kommen aus dem JSON-Block #page-config.
"use strict";

const config = JSON.parse(document.getElementById("page-config").textContent);
const checkButton = document.getElementById("check-button");
const checkStatus = document.getElementById("check-status");
let suggestionVersion = config.suggestionVersion;
let refreshingSuggestions = false;
const locale = document.documentElement.lang === "en" ? "en-GB" : "de-DE";

// ---------- Hilfsfunktionen ----------

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let result = {};
  try { result = await response.json(); } catch { /* keine JSON-Antwort */ }
  if (!response.ok) throw new Error(result.detail || t("Aktion fehlgeschlagen."));
  return result;
}

function postJson(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function titleOf(id) {
  const card = document.getElementById(`suggestion-${id}`);
  try { return JSON.parse(card.dataset.suggestion).title; } catch { return t("Vorschlag"); }
}

// ---------- Einblendungen (Toasts) mit optionalem „Rückgängig“ ----------

const toastRegion = document.getElementById("toast-region");

function toast(message, { action = null, onAction = null, error = false, timeout = 6000, choices = null, choicesLabel = "" } = {}) {
  const item = document.createElement("div");
  item.className = error ? "toast toast-error" : "toast";
  item.setAttribute("role", error ? "alert" : "status");
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = message;
  item.append(text);
  if (action && onAction) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-action";
    button.textContent = action;
    button.addEventListener("click", async () => {
      item.remove();
      try { await onAction(); } catch (actionError) { toast(actionError.message, { error: true }); }
    });
    item.append(button);
  }
  if (choices && choices.length) {
    const group = document.createElement("div");
    group.className = "toast-choices";
    group.setAttribute("role", "group");
    if (choicesLabel) {
      const label = document.createElement("span");
      label.className = "toast-choices-label";
      label.textContent = choicesLabel;
      group.append(label);
    }
    for (const choice of choices) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "toast-choice";
      button.textContent = choice.label;
      button.addEventListener("click", async () => {
        group.querySelectorAll("button").forEach((other) => { other.disabled = true; });
        button.setAttribute("aria-pressed", "true");
        try { await choice.onClick(); } catch (choiceError) { toast(choiceError.message, { error: true }); }
      });
      group.append(button);
    }
    item.append(group);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", t("Meldung schließen"));
  close.textContent = "×";
  close.addEventListener("click", () => item.remove());
  item.append(close);
  toastRegion.append(item);
  if (timeout) setTimeout(() => item.remove(), timeout);
  return item;
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".change-request-button");
  if (!button) return;
  const card = button.closest(".card");
  const feedback = card.querySelector(".change-request-feedback");
  const action = button.dataset.action;
  const buttons = card.querySelectorAll(".change-request-button");
  buttons.forEach((item) => { item.disabled = true; });
  feedback.textContent = action === "apply"
    ? t("Wird übernommen …")
    : action === "link"
      ? t("Zuordnung wird gespeichert …")
      : t("Wird abgelehnt …");
  try {
    const requestOptions = { method: "POST" };
    if (action === "link") {
      const suggestionId = card.querySelector(".change-request-target").value;
      if (!suggestionId) throw new Error(t("Bitte zuerst einen Zieltermin auswählen."));
      requestOptions.headers = { "Content-Type": "application/json" };
      requestOptions.body = JSON.stringify({ suggestion_id: Number(suggestionId) });
    }
    const response = await fetch(
      `/api/change-requests/${button.dataset.requestId}/${action}`,
      requestOptions,
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || t("Aktion fehlgeschlagen."));
    const title = card.querySelector("h3")?.textContent.trim() || t("Änderung");
    await refreshAll();
    if (action === "reject") {
      toast(t("Änderungsvorschlag abgelehnt."), {
        action: t("Rückgängig"),
        timeout: 8000,
        onAction: async () => {
          await api(`/api/change-requests/${button.dataset.requestId}/reopen`, { method: "POST" });
          await refreshAll();
          toast(t("Ablehnung rückgängig gemacht."));
        },
      });
    } else if (action === "apply") {
      toast(t("Übernommen: {title}", { title }));
    } else {
      toast(t("Zuordnung gespeichert."));
    }
  } catch (error) {
    feedback.textContent = error.message;
    toast(error.message, { error: true });
    buttons.forEach((item) => { item.disabled = false; });
  }
});

// Heute nur die Uhrzeit, sonst Datum und Uhrzeit.
function compactTime(value) {
  const date = new Date(value);
  const time = date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
  if (date.toDateString() === new Date().toDateString()) return t("{time} Uhr", { time });
  return `${date.toLocaleDateString(locale, { day: "numeric", month: "short" })}, ${time}`;
}

async function refreshActivity() {
  try {
    const response = await fetch("/api/activity?include_logs=false", { cache: "no-store" });
    if (!response.ok) throw new Error(t("Status nicht verfügbar"));
    const activity = await response.json();
    // Nicht neu zeichnen, solange jemand gerade ein Feld bearbeitet.
    if (
      activity.suggestion_version !== suggestionVersion &&
      !refreshingSuggestions &&
      !document.querySelector(".inline-editor")
    ) {
      await refreshAll();
    }

    renderServiceStatus(activity.health || []);
    const state = activity.status;
    checkButton.disabled = state.status === "running";
    let statusText;
    if (state.status === "running") {
      statusText = t("Prüfe Postfach …");
    } else if (state.status === "success") {
      statusText = t("Geprüft {time}", { time: compactTime(state.completed_at) });
    } else if (state.status === "error") {
      statusText = t("Prüfung fehlgeschlagen {time}", { time: compactTime(state.completed_at) });
    } else {
      statusText = config.demo
        ? t("Demo-Modus")
        : t("Prüft alle {minutes} Min.", { minutes: config.pollMinutes });
    }
    checkButton.classList.toggle("spinning", state.status === "running");
    // Nur bei Änderung setzen, sonst kündigen Screenreader den Text wiederholt an.
    if (checkStatus.textContent !== statusText) checkStatus.textContent = statusText;
  } catch {
    const offline = t("Live-Status momentan nicht verfügbar.");
    if (checkStatus.textContent !== offline) checkStatus.textContent = offline;
  }
}

let hadError = false;

function renderStatusSummary(items) {
  const panel = document.getElementById("status-panel");
  const summary = document.getElementById("status-summary");
  if (!panel || !summary) return;
  const problems = items.filter((item) => item.state === "warn" || item.state === "error");
  const text = problems.length
    ? `${problems.length === 1 ? t("1 Hinweis") : t("{count} Hinweise", { count: problems.length })}: ${problems.map((item) => item.label).join(", ")}`
    : t("Alles in Ordnung");
  if (summary.textContent !== text) summary.textContent = text;
  const worst = items.some((item) => item.state === "error") ? "error" : (problems.length ? "warn" : "ok");
  const dot = panel.querySelector(".status-dot");
  if (dot) dot.className = `status-dot dot ${worst}`;
  // Bei neu auftretendem Fehler aufklappen; sonst die Wahl des Nutzers respektieren.
  const hasError = items.some((item) => item.state === "error");
  if (hasError && !hadError) panel.open = true;
  hadError = hasError;
}

function renderServiceStatus(items) {
  const list = document.getElementById("service-status");
  if (!list || !items.length) return;
  renderStatusSummary(items);
  list.replaceChildren(...items.map((item) => {
    const row = document.createElement("li");
    row.className = `status-${item.state}`;
    row.dataset.key = item.key;
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.setAttribute("aria-hidden", "true");
    const label = document.createElement("strong");
    label.textContent = `${item.label}:`;
    const text = document.createElement("span");
    text.className = "status-text";
    text.textContent = item.text;
    row.append(dot, label, " ", text);
    return row;
  }));
}

async function refreshSuggestions() {
  const response = await fetch("/suggestions-fragment", { cache: "no-store" });
  if (!response.ok) throw new Error(t("Vorschläge konnten nicht aktualisiert werden."));
  const page = new DOMParser().parseFromString(await response.text(), "text/html");
  const updated = page.getElementById("suggestions-container");
  const current = document.getElementById("suggestions-container");
  if (!updated || !current) throw new Error(t("Vorschlagsliste ist nicht verfügbar."));
  const hiddenOpen = current.querySelector("#hidden-suggestions")?.open;
  const newHidden = updated.querySelector("#hidden-suggestions");
  if (newHidden && hiddenOpen) newHidden.open = true;
  current.replaceWith(updated);
  suggestionVersion = Number(updated.dataset.version);
  setDisplayValues(updated);
  afterSuggestionsRendered();
}

async function refreshAll() {
  if (refreshingSuggestions) return;
  refreshingSuggestions = true;
  try {
    await refreshSuggestions();
    await refreshChangeRequests();
  } finally {
    refreshingSuggestions = false;
  }
}

async function refreshChangeRequests() {
  const response = await fetch("/change-requests-fragment", { cache: "no-store" });
  if (!response.ok) throw new Error(t("Änderungsvorschläge konnten nicht geladen werden."));
  const page = new DOMParser().parseFromString(await response.text(), "text/html");
  const updated = page.getElementById("change-requests");
  const current = document.getElementById("change-requests");
  if (!updated || !current) throw new Error(t("Änderungsvorschläge sind nicht verfügbar."));
  current.replaceWith(updated);
}

async function exportSuggestion(button) {
  const card = button.closest(".card");
  const feedback = card.querySelector(".export-feedback");
  const action = button.dataset.action || "export";
  const calendar = button.dataset.calendar;
  const buttons = card.querySelectorAll(".export-button");
  buttons.forEach((item) => { item.disabled = true; });
  feedback.textContent = action === "resend"
    ? t("Erneuter Versand läuft …")
    : t("Wird in den Kalender „{calendar}“ übertragen …", { calendar });
  try {
    const options = { method: "POST" };
    if (calendar) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({ calendar });
    }
    const title = titleOf(button.dataset.suggestionId);
    const result = await api(`/api/suggestions/${button.dataset.suggestionId}/${action}`, options);
    selected.delete(button.dataset.suggestionId);
    await refreshSuggestions();
    toast(
      action === "resend"
        ? t("„{title}“ erneut an iCloud gesendet.", { title })
        : t("„{title}“ in „{calendar}“ übertragen.", { title, calendar: result.calendar || calendar }),
    );
  } catch (error) {
    feedback.textContent = error.message;
    toast(error.message, { error: true });
    // Die Kalenderwahl wurde evtl. schon gespeichert – Karte aktualisieren,
    // Fehlermeldung danach wieder anzeigen.
    const suggestionId = card.dataset.suggestionId;
    try { await refreshSuggestions(); } catch {}
    const current = document.getElementById(`suggestion-${suggestionId}`);
    const currentFeedback = current?.querySelector(".export-feedback");
    if (currentFeedback) currentFeedback.textContent = error.message;
    else buttons.forEach((item) => { item.disabled = false; });
  }
}

checkButton.addEventListener("click", async () => {
  checkButton.disabled = true;
  checkStatus.textContent = t("Prüfung wird gestartet …");
  try {
    const response = await fetch("/api/check", { method: "POST" });
    const result = await response.json();
    checkStatus.textContent = result.message;
    toast(result.message, { error: !response.ok });
    await refreshActivity();
  } catch {
    checkStatus.textContent = t("Prüfung konnte nicht gestartet werden.");
    checkButton.disabled = false;
  }
});


const editableFields = {
  title: { label: t("Titel"), type: "text", maxLength: 300 },
  "start-date": { label: t("Startdatum"), type: "date" },
  "start-time": { label: t("Startuhrzeit"), type: "time" },
  "end-date": { label: t("Enddatum"), type: "date" },
  "end-time": { label: t("Enduhrzeit"), type: "time" },
  location: { label: t("Ort"), type: "text", maxLength: 2000 },
  person: { label: t("Zielkalender") },
  notes: { label: t("Hinweise"), type: "textarea", maxLength: 2000 },
  recurrence: { label: t("Wiederholung") },
};
const personOptions = config.people;

function dateTimeParts(value) {
  if (!value) return { date: "", time: "" };
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return { date: value, time: "" };
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return { date: value.slice(0, 10), time: "" };
  const pad = (part) => String(part).padStart(2, "0");
  return {
    date: `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`,
    time: `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`,
  };
}

function defaultEventMinutes() {
  return Number(document.getElementById("suggestions-container")?.dataset.defaultMinutes || 60);
}

// Ohne Endzeit überträgt der Server Start + Standarddauer nach iCloud.
function assumedEnd(values) {
  if (values.end || !values.start || /^\d{4}-\d{2}-\d{2}$/.test(values.start)) return null;
  const start = new Date(values.start);
  if (Number.isNaN(start.getTime())) return null;
  const end = new Date(start.getTime() + defaultEventMinutes() * 60000);
  const pad = (part) => String(part).padStart(2, "0");
  return `${end.getFullYear()}-${pad(end.getMonth() + 1)}-${pad(end.getDate())}T${pad(end.getHours())}:${pad(end.getMinutes())}`;
}

function formatDate(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  const options = { weekday: "short", day: "numeric", month: "short" };
  if (year !== new Date().getFullYear()) options.year = "numeric";
  return new Date(year, month - 1, day).toLocaleDateString(locale, options);
}

function displayFieldValue(field, values) {
  if (field === "person") {
    return personOptions.includes(values.person) ? values.person : t("Kalender wählen");
  }
  const match = /^(start|end)-(date|time)$/.exec(field);
  if (!match) return values[field] || t("Hinzufügen");
  let value = values[match[1]];
  const assumed = match[1] === "end" && !value ? assumedEnd(values) : null;
  if (assumed) {
    const parts = dateTimeParts(assumed);
    if (match[2] === "time") return t("{time} Uhr (angenommen)", { time: parts.time });
    return parts.date === dateTimeParts(values.start).date
      ? t("gleicher Tag")
      : t("{date} (angenommen)", { date: formatDate(parts.date) });
  }
  if (!value) return match[1] === "end" ? t("Ende hinzufügen") : t("Hinzufügen");
  const parts = dateTimeParts(value);
  if (match[2] === "time") {
    if (!parts.time) return t("Ganztägig");
    return match[1] === "start" ? parts.time : t("{time} Uhr", { time: parts.time });
  }
  if (!parts.date) return t("Hinzufügen");
  return formatDate(parts.date);
}

function inputValue(field, values) {
  const match = /^(start|end)-(date|time)$/.exec(field);
  if (match) {
    const value = values[match[1]] || (match[1] === "end" ? assumedEnd(values) : null);
    return dateTimeParts(value)[match[2]];
  }
  return values[field] || "";
}

function updateWhenLine(card) {
  const line = card.querySelector(".fact-when");
  if (!line) return;
  const values = JSON.parse(card.dataset.suggestion);
  const start = dateTimeParts(values.start);
  const endValue = values.end || assumedEnd(values);
  const end = dateTimeParts(endValue);
  const endDate = line.querySelector("[data-field='end-date']");
  const endTime = line.querySelector("[data-field='end-time']");
  const startTime = line.querySelector("[data-field='start-time']");
  endDate.hidden = !endValue || end.date === start.date;
  endTime.hidden = Boolean(endValue) && !end.time;
  endTime.classList.toggle("add-end", !endValue);
  startTime.hidden = !start.time && Boolean(endValue) && !end.time && end.date !== start.date;
  line.querySelector(".range-sep").hidden = !endValue || (endDate.hidden && endTime.hidden);
}

function setDisplayValues(container) {
  container.querySelectorAll(".editable-value:not([data-field='recurrence'])").forEach((button) => {
    const values = JSON.parse(button.closest(".card").dataset.suggestion);
    const text = displayFieldValue(button.dataset.field, values);
    button.textContent = text;
    // Zugänglicher Name enthält den sichtbaren Text (WCAG 2.5.3).
    button.setAttribute("aria-label", t("{field} bearbeiten: {value}", { field: editableFields[button.dataset.field].label, value: text }));
    button.classList.toggle(
      "assumed",
      /^end-/.test(button.dataset.field) && !values.end && Boolean(assumedEnd(values)),
    );
  });
  container.querySelectorAll(".card").forEach(updateWhenLine);
}

function updateDateTimeValue(payload, field, value) {
  const match = /^(start|end)-(date|time)$/.exec(field);
  if (!match) {
    payload[field] = value || null;
    return null;
  }
  const [, key, part] = match;
  // Beim Bearbeiten eines angenommenen Endes von diesem Wert ausgehen.
  const currentParts = dateTimeParts(
    payload[key] || (key === "end" ? assumedEnd(payload) : null),
  );
  currentParts[part] = value;
  if (part === "time" && value && !currentParts.date) {
    return key === "start" ? t("Bitte zuerst ein Startdatum angeben.") : t("Bitte zuerst ein Enddatum angeben.");
  }
  if (key === "start" && !currentParts.date) {
    return t("Bitte ein Startdatum angeben.");
  }
  payload[key] = currentParts.date
    ? currentParts.time
      ? `${currentParts.date}T${currentParts.time}`
      : currentParts.date
    : null;
  return null;
}

const weekdayCodes = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const weekdayLabels = [t("Mo"), t("Di"), t("Mi"), t("Do"), t("Fr"), t("Sa"), t("So")];

function germanDate(iso) {
  const [year, month, day] = iso.split("-");
  return `${day}.${month}.${year}`;
}

function beginRecurrenceEdit(button) {
  const card = button.closest(".card");
  const values = JSON.parse(card.dataset.suggestion);
  const rule = values.recurrence || {};
  const startDate = dateTimeParts(values.start).date;
  const startWeekday = startDate
    ? weekdayCodes[(new Date(`${startDate}T12:00`).getDay() + 6) % 7]
    : "MO";
  const form = document.createElement("form");
  form.className = "inline-editor recurrence-editor";
  form.dataset.field = "recurrence";
  form.originalButton = button;
  const days = rule.weekdays && rule.weekdays.length ? rule.weekdays : [startWeekday];
  const endType = rule.until ? "until" : rule.count ? "count" : "none";
  form.innerHTML = `
    <label>${t("Wiederholen")}
      <select name="frequency">
        <option value="">${t("Keine")}</option>
        <option value="daily">${t("Täglich")}</option>
        <option value="weekly">${t("Wöchentlich")}</option>
        <option value="monthly">${t("Monatlich")}</option>
        <option value="yearly">${t("Jährlich")}</option>
      </select>
    </label>
    <label class="rule-part">${t("alle")} <input name="interval" type="number" min="1" max="99" step="1" style="width:4.5rem"> <span class="interval-unit"></span></label>
    <fieldset class="rule-part weekdays"><legend>${t("an")}</legend>${weekdayCodes.map((code, index) =>
      `<label><input type="checkbox" name="weekday" value="${code}"> ${weekdayLabels[index]}</label>`).join("")}</fieldset>
    <label class="rule-part">${t("Ende")}
      <select name="end_type">
        <option value="none">${t("Ohne Ende")}</option>
        <option value="until">${t("Bis Datum")}</option>
        <option value="count">${t("Nach Anzahl")}</option>
      </select>
    </label>
    <input class="rule-part end-until" name="until" type="date" aria-label="${t("Letzter Tag")}">
    <input class="rule-part end-count" name="count" type="number" min="1" max="500" step="1" style="width:5rem" aria-label="${t("Anzahl Termine")}">
    <label class="rule-part exdates">${t("Ausnahmen")} <input name="exdates" type="text" placeholder="${t("z. B. 22.12.2026, 29.12.2026")}" maxlength="1200"></label>
    <button type="submit">${t("Speichern")}</button>
    <button type="button" class="cancel-inline-edit">${t("Abbrechen")}</button>`;
  form.elements.frequency.value = rule.frequency || "";
  form.elements.interval.value = rule.interval || 1;
  form.querySelectorAll("input[name='weekday']").forEach((box) => { box.checked = days.includes(box.value); });
  form.elements.end_type.value = endType;
  form.elements.until.value = rule.until || "";
  form.elements.count.value = rule.count || "";
  form.elements.exdates.value = (rule.exdates || []).map(germanDate).join(", ");
  const units = { daily: t("Tag(e)"), weekly: t("Woche(n)"), monthly: t("Monat(e)"), yearly: t("Jahr(e)") };
  const update = () => {
    const frequency = form.elements.frequency.value;
    form.querySelectorAll(".rule-part").forEach((part) => { part.hidden = !frequency; });
    form.querySelector(".weekdays").hidden = frequency !== "weekly";
    form.querySelector(".interval-unit").textContent = units[frequency] || "";
    form.querySelector(".end-until").hidden = !frequency || form.elements.end_type.value !== "until";
    form.querySelector(".end-count").hidden = !frequency || form.elements.end_type.value !== "count";
  };
  form.addEventListener("change", update);
  update();
  button.replaceWith(form);
  form.elements.frequency.focus();
}

function collectRecurrence(form) {
  const frequency = form.elements.frequency.value;
  if (!frequency) return null;
  const endType = form.elements.end_type.value;
  return {
    frequency,
    interval: Number(form.elements.interval.value || 1),
    weekdays: frequency === "weekly"
      ? [...form.querySelectorAll("input[name='weekday']:checked")].map((box) => box.value)
      : [],
    until: endType === "until" ? form.elements.until.value || null : null,
    count: endType === "count" ? Number(form.elements.count.value) || null : null,
    exdates: form.elements.exdates.value.split(/[,;]/).map((part) => part.trim()).filter(Boolean),
  };
}

function beginInlineEdit(button) {
  const field = button.dataset.field;
  if (field === "person") {
    beginPersonSelection(button);
    return;
  }
  if (field === "recurrence") {
    beginRecurrenceEdit(button);
    return;
  }
  const config = editableFields[field];
  if (!config) return;
  const card = button.closest(".card");
  const current = JSON.parse(card.dataset.suggestion);
  const form = document.createElement("form");
  form.className = "inline-editor";
  form.dataset.field = field;
  const input = document.createElement(config.type === "textarea" ? "textarea" : "input");
  input.name = field;
  input.setAttribute("aria-label", config.label);
  if (config.type !== "textarea") input.type = config.type;
  if (config.maxLength) input.maxLength = config.maxLength;
  if (config.placeholder) input.placeholder = config.placeholder;
  input.value = inputValue(field, current);
  form.append(input);

  const save = document.createElement("button");
  save.type = "submit";
  save.textContent = t("Speichern");
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "cancel-inline-edit";
  cancel.textContent = t("Abbrechen");
  form.append(save, cancel);
  button.replaceWith(form);
  input.focus();
  if (input.select && input.type !== "date" && input.type !== "datetime-local") input.select();
}

function beginPersonSelection(button) {
  const current = JSON.parse(button.closest(".card").dataset.suggestion);
  const picker = document.createElement("div");
  picker.className = "inline-editor person-picker";
  picker.dataset.field = "person";
  const options = document.createElement("div");
  options.className = "person-options";
  for (const person of personOptions) {
    const option = document.createElement("button");
    option.type = "button";
    option.textContent = person;
    option.dataset.person = person;
    option.setAttribute("aria-pressed", String(current.person === person));
    options.append(option);
  }
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "cancel-inline-edit";
  cancel.textContent = t("Abbrechen");
  picker.append(options, cancel);
  button.replaceWith(picker);
}

async function saveInlineEdit(form, { refresh = true } = {}) {
  const field = form.dataset.field;
  const card = form.closest(".card");
  const payload = JSON.parse(card.dataset.suggestion);
  let validationMessage = null;
  if (field === "recurrence") {
    payload.recurrence = collectRecurrence(form);
    refresh = true;  // Beschreibung und nächster Termin kommen vom Server
    if (payload.recurrence?.frequency === "weekly" && !payload.recurrence.weekdays.length) {
      validationMessage = t("Bitte mindestens einen Wochentag wählen.");
    } else if (payload.recurrence && form.elements.end_type.value === "until" && !payload.recurrence.until) {
      validationMessage = t("Bitte das Enddatum angeben.");
    } else if (payload.recurrence && form.elements.end_type.value === "count" && !payload.recurrence.count) {
      validationMessage = t("Bitte die Anzahl der Termine angeben.");
    }
  } else if (field === "person") {
    payload.person = form.dataset.person;
  } else {
    validationMessage = updateDateTimeValue(
      payload,
      field,
      form.elements.namedItem(field).value,
    );
  }
  if (validationMessage) {
    const message = document.createElement("span");
    message.className = "inline-edit-error";
    message.setAttribute("role", "alert");
    message.textContent = validationMessage;
    form.append(message);
    return false;
  }
  const actionButtons = form.querySelectorAll("button");
  actionButtons.forEach((button) => { button.disabled = true; });
  form.querySelector(".inline-edit-error")?.remove();
  try {
    const response = await fetch(`/api/suggestions/${card.dataset.suggestionId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || t("Änderung konnte nicht gespeichert werden."));
    suggestionVersion = result.suggestion_version;
    if (refresh) {
      await refreshSuggestions();
    } else {
      card.dataset.suggestion = JSON.stringify(payload);
      const replacement = document.createElement("button");
      replacement.type = "button";
      replacement.className = "editable-value";
      replacement.dataset.field = field;
      replacement.setAttribute(
        "aria-label",
        t("{field} bearbeiten", { field: editableFields[field].label }),
      );
      form.replaceWith(replacement);
      setDisplayValues(card);
    }
    return true;
  } catch (saveError) {
    const message = document.createElement("span");
    message.className = "inline-edit-error";
    message.setAttribute("role", "alert");
    message.textContent = saveError.message;
    form.append(message);
    actionButtons.forEach((button) => { button.disabled = false; });
    return false;
  }
}

document.addEventListener("click", async (event) => {
  const activeEditor = document.querySelector(".inline-editor");
  if (activeEditor && !activeEditor.contains(event.target)) {
    const target = event.target.closest("a[href], form:not(.inline-editor) button[type='submit']");
    if (target) event.preventDefault();
    const saved = await saveInlineEdit(activeEditor, { refresh: false });
    if (!saved) return;
    if (target?.matches("a[href]")) {
      window.location.assign(target.href);
      return;
    }
    if (target?.matches("button[type='submit']")) {
      target.form.requestSubmit(target);
      return;
    }
  }

  const exportButton = event.target.closest(".export-button");
  if (exportButton) {
    void exportSuggestion(exportButton);
    return;
  }
  const value = event.target.closest(".editable-value");
  if (value) {
    beginInlineEdit(value);
    return;
  }
  const cancel = event.target.closest(".cancel-inline-edit");
  if (cancel) {
    const form = cancel.closest(".inline-editor");
    if (form.originalButton) {
      form.replaceWith(form.originalButton);
      return;
    }
    const card = form.closest(".card");
    const values = JSON.parse(card.dataset.suggestion);
    const replacement = document.createElement("button");
    replacement.type = "button";
    replacement.className = "editable-value";
    replacement.dataset.field = form.dataset.field;
    replacement.textContent = displayFieldValue(form.dataset.field, values);
    replacement.setAttribute(
      "aria-label",
      `${editableFields[form.dataset.field].label} bearbeiten: ${replacement.textContent}`,
    );
    form.replaceWith(replacement);
    return;
  }

  const personOption = event.target.closest(".person-options button");
  if (personOption) {
    const picker = personOption.closest(".person-picker");
    picker.dataset.person = personOption.dataset.person;
    void saveInlineEdit(picker);
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest(".inline-editor");
  if (!form) return;
  event.preventDefault();
  await saveInlineEdit(form);
});

// ---------- Entfernen mit Rückgängig ----------

async function dismissSuggestions(ids) {
  const titles = Object.fromEntries(ids.map((id) => [id, titleOf(id)]));
  const done = [];
  const failed = [];
  for (const id of ids) {
    try {
      await api(`/api/suggestions/${id}/dismiss`, { method: "POST" });
      done.push(id);
      selected.delete(String(id));
    } catch (error) {
      failed.push(`${titles[id]}: ${error.message}`);
    }
  }
  await refreshSuggestions();
  if (done.length) {
    const reasons = [
      ["kein_termin", t("Kein Termin")],
      ["nicht_relevant", t("Nicht für uns")],
      ["doppelt", t("Doppelt")],
      ["falsch", t("Angaben falsch")],
    ];
    toast(done.length === 1 ? t("„{title}“ entfernt.", { title: titles[done[0]] }) : t("{count} Vorschläge entfernt.", { count: done.length }), {
      action: t("Rückgängig"),
      timeout: 15000,
      choicesLabel: t("Warum? (hilft der KI)"),
      choices: reasons.map(([reason, label]) => ({
        label,
        onClick: async () => {
          for (const id of done) {
            await api(`/api/suggestions/${id}/reason`, postJson({ reason }));
          }
        },
      })),
      onAction: async () => {
        for (const id of done) await api(`/api/suggestions/${id}/undo-dismiss`, { method: "POST" });
        await refreshSuggestions();
        toast(done.length === 1 ? t("Vorschlag wiederhergestellt.") : t("{count} Vorschläge wiederhergestellt.", { count: done.length }));
      },
    });
  }
  if (failed.length) toast(t("Nicht entfernt: {list}", { list: failed.join(" · ") }), { error: true, timeout: 0 });
}

document.addEventListener("submit", (event) => {
  const form = event.target.closest(".dismiss-form");
  if (!form) return;
  event.preventDefault();
  void dismissSuggestions([form.closest(".card").dataset.suggestionId]);
});

// ---------- Absender ausblenden und Ausgeblendetes ----------

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".ignore-button");
  if (!button) return;
  const id = button.dataset.suggestionId;
  const title = titleOf(id);
  button.closest(".ignore-panel, .hint-actions").querySelectorAll("button").forEach((item) => { item.disabled = true; });
  let result;
  try {
    result = await api(`/api/suggestions/${id}/ignore-sender`, postJson({ scope: button.dataset.scope }));
  } catch (error) {
    button.closest(".ignore-panel, .hint-actions").querySelectorAll("button").forEach((item) => { item.disabled = false; });
    toast(error.message, { error: true });
    return;
  }
  selected.delete(String(id));
  await refreshSuggestions();
  const more = result.hidden === 1
    ? " " + t("Ein weiterer Vorschlag wurde ausgeblendet.")
    : result.hidden ? " " + t("{count} weitere Vorschläge wurden ausgeblendet.", { count: result.hidden }) : "";
  toast(t("„{title}“ entfernt; {pattern} wird künftig ausgeblendet.", { title, pattern: result.pattern }) + more, {
    action: t("Rückgängig"),
    timeout: 10000,
    onAction: async () => {
      if (result.created) await api("/api/ignored-senders/remove", postJson({ pattern: result.pattern }));
      if (result.dismissed) await api(`/api/suggestions/${id}/undo-dismiss`, { method: "POST" });
      await refreshSuggestions();
      toast(t("{pattern} wird wieder normal angezeigt.", { pattern: result.pattern }));
    },
  });
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".trust-button");
  if (!button) return;
  button.disabled = true;
  try {
    const result = await api("/api/sender-rules/trust", postJson({ pattern: button.dataset.pattern }));
    await refreshSuggestions();
    const shown = !result.shown ? ""
      : " " + (result.shown === 1 ? t("Ein Vorschlag ist wieder in der Liste.") : t("{count} Vorschläge sind wieder in der Liste.", { count: result.shown }));
    toast(t("{pattern} wird immer angezeigt.", { pattern: result.pattern }) + shown, {
      action: t("Rückgängig"),
      timeout: 8000,
      onAction: async () => {
        if (result.created) await api("/api/ignored-senders/remove", postJson({ pattern: result.pattern }));
        await refreshSuggestions();
      },
    });
  } catch (error) {
    button.disabled = false;
    toast(error.message, { error: true });
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".unhide-button, .hidden-dismiss-button");
  if (!button) return;
  const row = button.closest(".hidden-item");
  const id = row.dataset.suggestionId;
  const title = row.dataset.title;
  row.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  try {
    if (button.classList.contains("unhide-button")) {
      await api(`/api/suggestions/${id}/unhide`, { method: "POST" });
      await refreshSuggestions();
      toast(t("„{title}“ steht wieder in der Liste.", { title }));
    } else {
      await api(`/api/suggestions/${id}/dismiss`, { method: "POST" });
      await refreshSuggestions();
      toast(t("„{title}“ entfernt.", { title }), {
        action: t("Rückgängig"),
        timeout: 8000,
        onAction: async () => {
          await api(`/api/suggestions/${id}/undo-dismiss`, { method: "POST" });
          await refreshSuggestions();
        },
      });
    }
  } catch (error) {
    row.querySelectorAll("button").forEach((item) => { item.disabled = false; });
    toast(error.message, { error: true });
  }
});

// ---------- Erinnerung pro Termin ----------

document.addEventListener("change", async (event) => {
  const select = event.target.closest(".reminder-select");
  if (!select) return;
  const feedback = select.closest(".reminder-row").querySelector(".reminder-feedback");
  select.disabled = true;
  feedback.textContent = t("Wird gespeichert …");
  try {
    const result = await api(`/api/suggestions/${select.dataset.suggestionId}/reminder`, postJson({ reminder: select.value || null }));
    suggestionVersion = result.suggestion_version;
    feedback.textContent = t("Gespeichert – gilt beim Übertragen.");
    setTimeout(() => { if (feedback.isConnected) feedback.textContent = ""; }, 4000);
  } catch (error) {
    feedback.textContent = "";
    toast(error.message, { error: true });
  } finally {
    select.disabled = false;
  }
});

// ---------- Mail erneut auswerten ----------

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".reanalyze-button");
  if (!button) return;
  const sourceKey = button.dataset.sourceKey;
  button.disabled = true;
  let progress;
  try {
    const started = await api("/api/messages/reanalyze", postJson({ source_key: sourceKey }));
    progress = toast(started.waiting
      ? t("Erneute Auswertung wartet, bis der laufende Postfachabruf fertig ist …")
      : t("Mail wird erneut ausgewertet … Das kann einige Minuten dauern."), { timeout: 0 });
    const query = `/api/messages/reanalyze?source_key=${encodeURIComponent(sourceKey)}`;
    const deadline = Date.now() + 45 * 60 * 1000;
    let state = { status: "queued" };
    while (["queued", "running"].includes(state.status) && Date.now() < deadline) {
      await sleep(3000);
      state = await api(query);
    }
    progress.remove();
    if (state.status === "done") {
      if (button.dataset.reload) { window.location.reload(); return; }
      await refreshAll();
      toast(state.message, { timeout: 10000 });
    } else if (state.status === "error") {
      toast(state.message, { error: true, timeout: 0 });
    } else {
      toast(t("Die Auswertung dauert ungewöhnlich lange. Ergebnis später im Protokoll."), { timeout: 0 });
    }
  } catch (error) {
    progress?.remove();
    toast(error.message, { error: true });
  } finally {
    if (button.isConnected) button.disabled = false;
  }
});

// ---------- Filter und Suche ----------

const filterState = { person: "all", query: "" };
try { Object.assign(filterState, JSON.parse(sessionStorage.getItem("suggestionFilter") || "{}")); } catch { /* egal */ }
const searchInput = document.getElementById("suggestion-search");
searchInput.value = filterState.query;
const searchToggle = document.getElementById("search-toggle");
const searchRow = document.getElementById("search-row");
function showSearch(open) {
  searchRow.hidden = !open;
  searchToggle.setAttribute("aria-expanded", String(open));
}
showSearch(Boolean(filterState.query));
searchToggle.addEventListener("click", () => {
  const open = searchRow.hidden;
  showSearch(open);
  if (open) searchInput.focus();
  else if (searchInput.value) {
    searchInput.value = "";
    searchInput.dispatchEvent(new Event("input"));
  }
});

function cardMatches(card) {
  const person = card.dataset.person;
  const personMatch =
    filterState.person === "all" ||
    (filterState.person === "none" ? !person : person === filterState.person);
  const query = filterState.query.trim().toLowerCase();
  return personMatch && (!query || card.dataset.search.includes(query));
}

function applyFilter() {
  const container = document.getElementById("suggestions-container");
  const cards = [...container.querySelectorAll(".card")];
  const counts = { all: cards.length, none: 0 };
  for (const card of cards) {
    const key = card.dataset.person || "none";
    counts[key] = (counts[key] || 0) + 1;
    card.hidden = !cardMatches(card);
  }
  // Gruppenüberschriften ausblenden, wenn darunter nichts sichtbar ist.
  let heading = null;
  let visible = 0;
  const closeGroup = () => {
    if (!heading) return;
    heading.hidden = visible === 0;
    heading.querySelector(".group-count").textContent = visible;
  };
  for (const element of container.children) {
    if (element.classList.contains("group-heading")) {
      closeGroup();
      heading = element;
      visible = 0;
    } else if (element.classList.contains("card") && !element.hidden) {
      visible += 1;
    }
  }
  closeGroup();
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.setAttribute("aria-pressed", String(chip.dataset.person === filterState.person));
    chip.querySelector(".chip-count").textContent = counts[chip.dataset.person] || 0;
  });
  const anyVisible = cards.some((card) => !card.hidden);
  document.getElementById("filter-empty").hidden = !cards.length || anyVisible;
  try { sessionStorage.setItem("suggestionFilter", JSON.stringify(filterState)); } catch { /* egal */ }
}

document.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  filterState.person = chip.dataset.person;
  applyFilter();
});
searchInput.addEventListener("input", () => {
  filterState.query = searchInput.value;
  applyFilter();
});

// ---------- Mehrfachauswahl ----------

const selected = new Set();
const selectionBar = document.getElementById("selection-bar");

function updateSelection() {
  const present = new Set([...document.querySelectorAll(".card-select")].map((box) => box.value));
  for (const id of [...selected]) if (!present.has(id)) selected.delete(id);
  document.querySelectorAll(".card-select").forEach((box) => {
    box.checked = selected.has(box.value);
    box.closest(".card").classList.toggle("selected", box.checked);
  });
  selectionBar.hidden = selected.size === 0;
  document.getElementById("selection-count").textContent =
    selected.size === 1 ? t("1 Vorschlag ausgewählt") : t("{count} Vorschläge ausgewählt", { count: selected.size });
  document.body.classList.toggle("has-selection", selected.size > 0);
}

document.addEventListener("change", (event) => {
  const box = event.target.closest(".card-select");
  if (!box) return;
  if (box.checked) selected.add(box.value);
  else selected.delete(box.value);
  updateSelection();
});

document.getElementById("select-visible").addEventListener("click", () => {
  document.querySelector(".filter-menu")?.removeAttribute("open");
  document.querySelectorAll(".card:not([hidden]) .card-select").forEach((box) => selected.add(box.value));
  updateSelection();
});
document.getElementById("selection-clear").addEventListener("click", () => {
  selected.clear();
  updateSelection();
});
document.getElementById("batch-dismiss").addEventListener("click", () => {
  void dismissSuggestions([...selected]);
});

async function exportSelected(calendar) {
  const ids = [...selected];
  const titles = Object.fromEntries(ids.map((id) => [id, titleOf(id)]));
  const buttons = selectionBar.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  const progress = toast(t("Übertrage {done} von {total} …", { done: 0, total: ids.length }), { timeout: 0 });
  let succeeded = 0;
  const failed = [];
  for (const [index, id] of ids.entries()) {
    try {
      await api(`/api/suggestions/${id}/export`, postJson({ calendar }));
      succeeded += 1;
      selected.delete(id);
    } catch (error) {
      failed.push(`${titles[id]}: ${error.message}`);
    }
    progress.querySelector(".toast-text").textContent = t("Übertrage {done} von {total} …", { done: index + 1, total: ids.length });
  }
  progress.remove();
  buttons.forEach((button) => { button.disabled = false; });
  await refreshSuggestions();
  if (succeeded) {
    toast(succeeded === 1 ? t("1 Vorschlag in „{calendar}“ übertragen.", { calendar }) : t("{count} Vorschläge in „{calendar}“ übertragen.", { count: succeeded, calendar }));
  }
  if (failed.length) toast(t("Nicht übertragen: {list}", { list: failed.join(" · ") }), { error: true, timeout: 0 });
}

document.addEventListener("click", (event) => {
  const button = event.target.closest(".batch-export");
  if (button) void exportSelected(button.dataset.calendar);
});

// ---------- Kompakte Karten / alle Details ----------

const detailsToggle = document.getElementById("show-details");
try { detailsToggle.checked = localStorage.getItem("showCardDetails") === "1"; } catch { /* egal */ }

function applyDetailsPreference() {
  document.querySelectorAll("details.card-more").forEach((details) => { details.open = detailsToggle.checked; });
}
detailsToggle.addEventListener("change", () => {
  try { localStorage.setItem("showCardDetails", detailsToggle.checked ? "1" : "0"); } catch { /* egal */ }
  applyDetailsPreference();
});

// Nach jedem Neuzeichnen der Liste: Filter, Auswahl und Details wiederherstellen.
function afterSuggestionsRendered() {
  const badge = document.getElementById("nav-open-count");
  if (badge) {
    const open = document.querySelectorAll("#suggestions-container .card-select").length;
    badge.textContent = open;
    badge.hidden = !open;
  }
  applyFilter();
  updateSelection();
  applyDetailsPreference();
}

setDisplayValues(document.getElementById("suggestions-container"));
afterSuggestionsRendered();
refreshActivity();
window.setInterval(refreshActivity, 2000);

// Lange Fundstellen sind auf drei Zeilen gekürzt; Antippen zeigt alles.
document.addEventListener("click", (event) => {
  const quote = event.target.closest(".source-quote blockquote");
  if (quote) quote.classList.toggle("clamped");
});
document.addEventListener("keydown", (event) => {
  const quote = event.target.closest?.(".source-quote blockquote");
  if (quote && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    quote.classList.toggle("clamped");
  }
});
// Menüs schließen beim Tipp daneben.
document.addEventListener("click", (event) => {
  document.querySelectorAll(".filter-menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
});
