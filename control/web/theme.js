const systemTheme = matchMedia("(prefers-color-scheme: dark)");
let theme = "system";
try {
  const saved = localStorage.getItem("theme");
  if (saved === "light" || saved === "dark") theme = saved;
} catch {
  // Follow the browser when storage is unavailable.
}
function applyTheme() {
  const dark = theme === "dark" || (theme === "system" && systemTheme.matches);
  document.documentElement.classList.toggle("wa-dark", dark);
  document.documentElement.classList.toggle("wa-light", !dark);
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.textContent = dark ? "Light Mode" : "Dark Mode";
    button.title = `Switch to ${dark ? "Light" : "Dark"} Mode${theme === "system" ? " · Following Browser" : ""}`;
  });
}
systemTheme.addEventListener("change", applyTheme);
function toggleTheme() {
  const dark = !document.documentElement.classList.contains("wa-dark");
  // Toggling back to the browser's preference restores automatic changes.
  theme = dark === systemTheme.matches ? "system" : dark ? "dark" : "light";
  try {
    if (theme === "system") localStorage.removeItem("theme");
    else localStorage.setItem("theme", theme);
  } catch {
    // Keep the choice for this visit.
  }
  applyTheme();
}
// Menu items toggle on selection, which also covers keyboard use; buttons toggle on click.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-theme-toggle]");
  if (toggle && toggle.localName !== "wa-dropdown-item") toggleTheme();
});
document.addEventListener("wa-select", (event) => {
  if (event.detail?.item?.matches("[data-theme-toggle]")) toggleTheme();
});
applyTheme();
