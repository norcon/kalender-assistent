// Kopier-Knopf an jedem Codeblock.
document.querySelectorAll("pre > code").forEach((code) => {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy";
  const label = document.documentElement.lang === "en" ? ["Copy", "Copied"] : ["Kopieren", "Kopiert"];
  button.textContent = label[0];
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(code.innerText);
      button.textContent = label[1];
      button.classList.add("done");
      setTimeout(() => { button.textContent = label[0]; button.classList.remove("done"); }, 1800);
    } catch { /* Zwischenablage nicht verfügbar */ }
  });
  code.parentElement.append(button);
});
