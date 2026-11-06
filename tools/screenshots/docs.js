// Screenshots für die Projektseite aus dem Demo-Modus (DE und EN).
// Aufruf: BASE=http://127.0.0.1:8091 LANG_CODE=de node docs.js
const { webkit, chromium, devices } = require("playwright");
const base = process.env.BASE || "http://127.0.0.1:8091";
const lang = process.env.LANG_CODE || "de";
const out = (name) => `/out/${lang}-${name}.png`;

async function phone(browser, scheme) {
  const context = await browser.newContext({ ...devices["iPhone 13"], colorScheme: scheme, locale: lang === "en" ? "en-GB" : "de-DE", timezoneId: "Europe/Berlin" });
  await context.addCookies([{ name: "lang", value: lang, url: base }]);
  const page = await context.newPage();
  return { context, page };
}

(async () => {
  const errors = [];
  const webkitBrowser = await webkit.launch();
  for (const scheme of ["light", "dark"]) {
    const { context, page } = await phone(webkitBrowser, scheme);
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(base + "/", { waitUntil: "load" });
    await page.waitForTimeout(1200);
    const card = page.locator(".card[data-suggestion-id]").first();
    await card.evaluate((el) => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 70));
    await page.waitForTimeout(200);
    await page.screenshot({ path: out(`phone-${scheme}-card`) });
    if (scheme === "light") {
      const hint = page.locator(".hide-hint").first();
      await hint.scrollIntoViewIfNeeded();
      await page.evaluate(() => window.scrollBy(0, -220));
      await page.screenshot({ path: out("phone-light-hint") });
      await page.locator("#hidden-suggestions summary").click();
      await page.locator("#hidden-suggestions").scrollIntoViewIfNeeded();
      await page.waitForTimeout(300);
      await page.screenshot({ path: out("phone-light-hidden") });
    }
    await context.close();
  }
  await webkitBrowser.close();

  const desktop = await chromium.launch();
  for (const scheme of ["light", "dark"]) {
    const context = await desktop.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1.5, colorScheme: scheme, locale: lang === "en" ? "en-GB" : "de-DE", timezoneId: "Europe/Berlin" });
    await context.addCookies([{ name: "lang", value: lang, url: base }]);
    const page = await context.newPage();
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(base + "/", { waitUntil: "load" });
    await page.waitForTimeout(1200);
    await page.locator("#status-panel").evaluate((el) => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 330));
    await page.waitForTimeout(200);
    await page.screenshot({ path: out(`desktop-${scheme}-start`) });
    if (scheme === "light") {
      for (const [path, name, anchor] of [
        ["/einstellungen", "settings", "#zielkalender"],
        ["/korrekturen", "rules", "#quote"],
        ["/sicherungen/kalender", "restore", null],
        ["/protokoll/wartung", "maintenance", null],
      ]) {
        await page.goto(base + path, { waitUntil: "load" });
        await page.waitForTimeout(600);
        if (anchor) { await page.locator(anchor).scrollIntoViewIfNeeded(); await page.evaluate(() => window.scrollBy(0, -20)); }
        await page.screenshot({ path: out(`desktop-${name}`) });
      }
    }
    await context.close();
  }
  await desktop.close();
  console.log(lang, "Fehler:", errors.length ? errors : "keine");
})();
