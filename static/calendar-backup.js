// Kalender-Sicherung durchsuchen: Filter ohne Neuladen, „Weitere laden“, Rückfrage vor dem Wiederherstellen.
// Ohne JavaScript funktioniert die Seite über normale Links und das Suchformular.
"use strict";
(function () {
  const results = document.getElementById("backup-results");
  const form = document.getElementById("backup-search");
  if (!results || !form) return;
  document.documentElement.classList.add("js");
  const search = form.elements.q;
  let request = 0;

  function withPart(href, part) {
    const url = new URL(href, window.location.origin);
    url.searchParams.set("teil", part);
    return url;
  }

  function fragment(html) {
    const template = document.createElement("template");
    template.innerHTML = html;
    return template.content;
  }

  async function load(href) {
    const current = ++request;
    results.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(withPart(href, "liste"), { headers: { Accept: "text/html" } });
      if (!response.ok) throw new Error(String(response.status));
      const html = await response.text();
      if (current !== request) return; // eine neuere Anfrage ist schon unterwegs
      results.replaceChildren(fragment(html));
      const url = new URL(href, window.location.origin);
      url.searchParams.delete("seite");
      window.history.replaceState(null, "", url.pathname + url.search);
      syncForm(url);
    } catch {
      window.location.href = href; // Fallback: normal laden
    } finally {
      if (current === request) results.removeAttribute("aria-busy");
    }
  }

  // Versteckte Felder des Suchformulars an die aktuellen Filter angleichen.
  function syncForm(url) {
    for (const key of ["stand", "kalender", "status", "zeitraum"]) {
      let input = form.querySelector(`input[type="hidden"][name="${key}"]`);
      const value = url.searchParams.get(key) || "";
      if (!value) { if (input) input.remove(); continue; }
      if (!input) {
        input = Object.assign(document.createElement("input"), { type: "hidden", name: key });
        form.prepend(input);
      }
      input.value = value;
    }
  }

  function currentHref(changes) {
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(changes)) {
      if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
    }
    url.searchParams.delete("seite");
    url.searchParams.delete("restored");
    url.searchParams.delete("problem");
    return url.pathname + url.search;
  }

  let timer;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(currentHref({ q: search.value.trim() })), 250);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(timer);
    load(currentHref({ q: search.value.trim() }));
  });

  // Zusammengehörige Monats-/Tagesgruppen beim Nachladen zusammenführen.
  function appendGroups(list, content) {
    for (const node of [...content.childNodes]) {
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      if (node.matches(".pager")) continue;
      const last = list.lastElementChild;
      if (node.matches(".month-group") && last && last.matches(".month-group") && last.dataset.key === node.dataset.key) {
        for (const day of [...node.querySelectorAll(":scope > .day-group")]) {
          const lastDay = last.lastElementChild;
          if (lastDay && lastDay.matches(".day-group") && lastDay.dataset.key === day.dataset.key) {
            lastDay.querySelector(".day-items").append(...day.querySelector(".day-items").children);
          } else {
            last.append(day);
          }
        }
        continue;
      }
      if (node.matches("[data-divider]") && list.querySelector("[data-divider]")) continue;
      list.append(node);
    }
  }

  document.addEventListener("click", async (event) => {
    const live = event.target.closest("a[data-live]");
    if (live && results.contains(live)) {
      event.preventDefault();
      load(live.getAttribute("href"));
      return;
    }
    const more = event.target.closest("a.more-link");
    if (more && results.contains(more)) {
      event.preventDefault();
      const pager = more.closest(".pager");
      more.setAttribute("aria-disabled", "true");
      more.textContent = t("Lädt …");
      try {
        const response = await fetch(withPart(more.getAttribute("href"), "mehr"));
        if (!response.ok) throw new Error(String(response.status));
        const content = fragment(await response.text());
        const nextPager = content.querySelector(".pager");
        appendGroups(document.getElementById("backup-list"), content);
        if (nextPager) pager.replaceWith(nextPager); else pager.remove();
      } catch {
        window.location.href = more.getAttribute("href");
      }
    }
  });

  document.addEventListener("submit", (event) => {
    const restore = event.target.closest("form[data-confirm]");
    if (!restore) return;
    if (!window.confirm(restore.dataset.confirm)) { event.preventDefault(); return; }
    const button = restore.querySelector("button");
    button.disabled = true;
    button.textContent = t("Wird wiederhergestellt …");
  });
})();
