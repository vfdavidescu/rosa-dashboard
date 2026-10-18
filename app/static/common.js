/* Shared by every Rosa Dash page: theme toggle, formatting, units, API calls. */
const $ = (id) => document.getElementById(id);

/* ---------- theme ---------- */
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
function effectiveTheme() {
  const set = document.documentElement.getAttribute("data-theme");
  return set === "light" || set === "dark" ? set : (darkQuery.matches ? "dark" : "light");
}
function syncThemeChrome() {
  const dark = effectiveTheme() === "dark";
  $("themecolor")?.setAttribute("content", dark ? "#14110f" : "#f4f1ee");
  $("statusbar")?.setAttribute("content", dark ? "black-translucent" : "default");
  const btn = $("theme");
  if (btn) {
    btn.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    btn.title = btn.getAttribute("aria-label");
  }
}
$("theme")?.addEventListener("click", () => {
  const next = effectiveTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("rosa-theme", next); } catch (e) {}
  syncThemeChrome();
});
darkQuery.addEventListener?.("change", syncThemeChrome);
syncThemeChrome();

/* ---------- formatting ---------- */
const KM_PER_MI = 1.609344;
const MPG_UK_L100 = 282.481; // L/100km = 282.481 / mpg (UK gallon)

const fmt = {
  n: (v, d = 0) => v == null || Number.isNaN(Number(v)) ? "–"
    : Number(v).toLocaleString("en-GB", { maximumFractionDigits: d, minimumFractionDigits: d }),
  rel(iso) {
    if (!iso) return "unknown";
    const s = (Date.now() - new Date(iso)) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  },
  when(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }) + " · " +
           d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  },
  date(iso) {
    if (!iso) return "–";
    return new Date(iso.length === 10 ? iso + "T12:00:00" : iso)
      .toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  },
  dur(min) {
    if (min == null) return "–";
    const h = Math.floor(min / 60), m = Math.round(min % 60);
    return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m} min`;
  },
};

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- units ---------- */
// Convert a distance from one unit to another ("mi" / "km").
function convertDistance(value, from, to) {
  if (value == null || from === to) return value;
  return from === "km" ? value / KM_PER_MI : value * KM_PER_MI;
}

// A value in the server's unit plus the other unit, as strings.
function both(value, kind, units, digits = 0) {
  if (value == null) return { pri: "–", priUnit: "", alt: "" };
  if (kind === "distance") {
    const isMi = units.distance === "mi";
    const other = isMi ? value * KM_PER_MI : value / KM_PER_MI;
    return { pri: fmt.n(value, digits), priUnit: units.distance, alt: `${fmt.n(other, digits)} ${isMi ? "km" : "mi"}` };
  }
  // consumption: mpg (UK) <-> L/100km
  if (!value) return { pri: "–", priUnit: "", alt: "" };
  const isMpg = units.consumption === "mpg";
  const other = MPG_UK_L100 / value;
  return { pri: fmt.n(value, 1), priUnit: units.consumption, alt: `${fmt.n(other, 1)} ${isMpg ? "L/100km" : "mpg"}` };
}

// Big tile: main number with unit, other unit underneath in grey.
function bigDual(value, kind, units, cls = "big") {
  const b = both(value, kind, units);
  return `<div class="${cls}">${b.pri}${b.priUnit ? `<span class="unit">${b.priUnit}</span>` : ""}</div>` +
         (b.alt ? `<span class="alt">${b.alt}</span>` : "");
}
// Inline: "14.2 mi  23 km" with the second part grey.
function inlineDual(value, kind, units, digits = 0) {
  const b = both(value, kind, units, digits);
  return `${b.pri}${b.priUnit ? " " + b.priUnit : ""}${b.alt ? `<span class="alt">${b.alt}</span>` : ""}`;
}

/* ---------- API ---------- */
// Every write carries X-Rosa: 1 (the server rejects writes without it; see main.py).
async function api(path, { method = "GET", body, headers = {} } = {}) {
  const opts = { method, cache: "no-store", headers: { ...headers } };
  if (method !== "GET") opts.headers["X-Rosa"] = "1";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) {
    let msg = data?.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    const err = new Error(msg || `Request failed (${r.status})`);
    err.status = r.status;
    throw err;
  }
  return data;
}
