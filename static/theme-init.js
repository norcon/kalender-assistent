// Vor dem ersten Zeichnen: gewählten Modus anwenden (verhindert helles Aufblitzen).
(function () {
  try {
    var theme = localStorage.getItem("theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  } catch (error) { /* privater Modus o. Ä.: Gerät entscheidet */ }
})();
