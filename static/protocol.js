// Protokollseite: Live-Meldungen nachladen.
"use strict";
(function () {
  var log = document.getElementById("activity-log");
  var lastId = 0;

  function addEntry(entry) {
    var empty = document.getElementById("activity-empty");
    if (empty) empty.remove();
    var row = document.createElement("li");
    var time = document.createElement("time");
    time.dateTime = entry.timestamp;
    time.textContent = new Date(entry.timestamp).toLocaleString("de-DE");
    var level = document.createElement("span");
    level.className = "level " + entry.level.toLowerCase();
    level.textContent = entry.level;
    var message = document.createElement("span");
    message.textContent = entry.message;
    row.append(time, level, message);
    // Nur mitscrollen, wenn man ohnehin unten war.
    var atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.append(row);
    while (log.children.length > 200) log.firstElementChild.remove();
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  async function refresh() {
    try {
      var response = await fetch("/api/activity?after=" + lastId, { cache: "no-store" });
      if (!response.ok) return;
      var activity = await response.json();
      activity.logs.forEach(function (entry) {
        addEntry(entry);
        lastId = Math.max(lastId, entry.id);
      });
    } catch (error) { /* nächster Versuch beim nächsten Intervall */ }
  }

  refresh();
  setInterval(refresh, 1000);
})();
