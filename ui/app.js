"use strict";

/* ───────────────────────── basics ───────────────────────── */
const TOKEN = new URLSearchParams(location.hash.slice(1)).get("t") || "";
const root = document.documentElement;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const plural = (n, w, p) => `${n.toLocaleString()} ${n === 1 ? w : (p || w + "s")}`;
const human = (n) => {
  if (!n || n <= 0) return n === 0 ? "0 B" : "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < 4) { n /= 1024; i++; }
  return (i < 2 ? n.toFixed(0) : n >= 100 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
};
const ago = (ts) => {
  if (!ts) return "";
  const d = (Date.now() / 1000 - ts) / 86400;
  if (d < 1) return "today"; if (d < 2) return "yesterday"; if (d < 14) return `${Math.floor(d)} days ago`;
  if (d < 60) return `${Math.floor(d / 7)} weeks ago`; if (d < 730) return `${Math.floor(d / 30)} months ago`;
  return `${Math.floor(d / 365)} years ago`;
};
const fmtUp = (s) => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`; };

/* ───────────────────────── spring motion ─────────────────────────
   Every transition uses a real spring: we simulate it and hand CSS the curve as linear() stops. */
const SPRINGS = { micro: { stiffness: 400, damping: 40 }, standard: { stiffness: 260, damping: 20 },
                  modal: { stiffness: 100, damping: 15 }, heavy: { stiffness: 80, damping: 25, mass: 2 } };
function springCurve({ stiffness, damping, mass = 1 }) {
  const dt = 1 / 240; let x = 0, v = 0, t = 0; const pts = [];
  while (t < 4) {
    const a = (-stiffness * (x - 1) - damping * v) / mass;
    v += a * dt; x += v * dt; t += dt; pts.push(x);
    if (t > 0.08 && Math.abs(x - 1) < 0.002 && Math.abs(v) < 0.02) break;
  }
  const step = Math.max(1, Math.floor(pts.length / 40)), stops = [];
  for (let i = 0; i < pts.length; i += step) stops.push(+pts[i].toFixed(4));
  stops.push(1);
  return { easing: `linear(0, ${stops.join(", ")})`, ms: Math.round(t * 1000) };
}
(function installSprings() {
  if (!(window.CSS && CSS.supports("transition-timing-function", "linear(0, 0.5, 1)"))) return;
  for (const [k, p] of Object.entries(SPRINGS)) {
    const s = springCurve(p);
    root.style.setProperty(`--spring-${k}`, s.easing);
    root.style.setProperty(`--dur-${k}`, s.ms + "ms");
  }
})();

/* ───────────────────────── server ───────────────────────── */
async function api(path, body) {
  const r = await fetch(path, { method: body ? "POST" : "GET", headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
                                body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || `The app's helper answered ${r.status}`);
  return j;
}
async function run(path, body, { openLog = true } = {}) {
  try { const j = await api(path, body); if (openLog) openConsole(true); return j; }
  catch (e) { toast(e.message, "err"); return null; }
}
function toast(msg, kind = "ok") {
  const t = document.createElement("div");
  t.className = "toast";
  t.innerHTML = `<span class="mark ${kind === "err" ? "danger" : "safe"}">${icon(kind === "err" ? "alert" : "check")}</span><span>${esc(msg)}</span>`;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "err" ? 7000 : 4200);
}

/* ───────────────────────── state & navigation ───────────────────────── */
const S = {
  view: "overview", sys: null, live: null, apps: null, settings: {}, tweaks: null, tasks: null, catalog: null, clean: null, org: null,
  appTab: "installed", group: null, q: "", scope: "user", sort: "size", show: "all", limit: 120,
  sel: new Set(), withData: new Set(), catSel: new Set(), cleanSel: new Set(), orgSel: new Set(),
  busy: null, lastLine: "", drawerId: null,
};
const loaders = { overview: () => Promise.all([loadSys(), S.apps ? null : loadApps()]), apps: loadApps, get: loadCatalog,
                  tweaks: loadTweaks, tasks: loadTasks, clean: loadClean, organize: loadOrg };

function go(view) {
  S.view = view;
  $$(".nav[data-view]").forEach((b) => b.dataset.view === view ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
  $$(".view").forEach((v) => (v.hidden = v.id !== "v-" + view));
  render();
  (loaders[view] || (() => {}))()?.catch?.((e) => toast(e.message, "err"));
  renderSelbar();
}
$$(".nav[data-view]").forEach((b) => (b.onclick = () => go(b.dataset.view)));
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-go]");
  if (g) { if (g.dataset.group) { S.group = g.dataset.group; S.scope = "all"; } if (g.dataset.tab) S.appTab = g.dataset.tab; if (g.dataset.show) S.show = g.dataset.show; go(g.dataset.go); }
  const t = e.target.closest("[data-task]");
  if (t) run("/api/task", { id: t.dataset.task });
});

function render() {
  ({ overview: renderOverview, apps: renderApps, get: renderCatalog, tweaks: renderTweaks, tasks: renderTasks,
     clean: renderClean, organize: renderOrg })[S.view]?.();
}

/* ───────────────────────── theme ───────────────────────── */
function applyTheme() {
  const pref = S.settings.theme || "system";
  const dark = pref === "dark" || (pref === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  root.dataset.theme = dark ? "dark" : "light";
  const acc = S.settings.match_cosmic && S.settings.cosmic_accent;
  if (acc) {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(acc.slice(i, i + 2), 16) / 255);
    const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    root.style.setProperty("--cosmic-accent", acc);
    root.style.setProperty("--primary-foreground", lum > 0.45 ? "#0f1216" : "#f7f9fc");
    root.dataset.accent = "cosmic";
  } else {
    delete root.dataset.accent;
    root.style.removeProperty("--cosmic-accent");
    root.style.removeProperty("--primary-foreground");
  }
  $("#themeIcon").setAttribute("href", dark ? "#i-sun" : "#i-moon");
  $("#themeText").textContent = dark ? "Light look" : "Dark look";
}
$("#themeBtn").onclick = async () => {
  S.settings.theme = root.dataset.theme === "dark" ? "light" : "dark";
  applyTheme();
  api("/api/settings", { theme: S.settings.theme }).catch(() => {});
};
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

/* ───────────────────────── overview ───────────────────────── */
async function loadSys() { S.sys = await api("/api/sysinfo"); renderOverview(); tickLive(); }
async function tickLive() {
  if (S.view !== "overview" || document.hidden) return;
  S.live = await api("/api/live").catch(() => S.live);
  updateMeters();
}
setInterval(tickLive, 2000);

function meterRows() {
  const L = S.live; if (!L) return [];
  const rows = [["Processor", L.cpu, 100, `${Math.round(L.cpu)}%${L.temp != null ? ` at ${L.temp} °C` : ""}`],
                ["Memory", L.mem_used, L.mem_total, `${human(L.mem_used)} of ${human(L.mem_total)}`]];
  if (L.swap_total) rows.push(["Swap", L.swap_used, L.swap_total, `${human(L.swap_used)} of ${human(L.swap_total)}`]);
  L.disks.forEach((d, i) => rows.push([L.disks.length > 1 ? (i ? "Home drive" : "System drive") : "Drive", d.used, d.total, `${human(d.free)} free`]));
  if (L.gpu && L.gpu.util != null) rows.push(["Graphics", L.gpu.util, 100, `${Math.round(L.gpu.util)}%${L.gpu.temp ? ` at ${L.gpu.temp} °C` : ""}`]);
  return rows;
}
function updateMeters() {
  const box = $("#meters"); if (!box) return;
  const rows = meterRows();
  if (box.childElementCount !== rows.length) {
    box.innerHTML = rows.map((r, i) => `<div class="meter"><span>${esc(r[0])}</span><div class="track"><div class="fill" id="mf${i}"></div></div><b class="num" id="mt${i}"></b></div>`).join("");
  }
  rows.forEach((r, i) => {
    const f = r[2] ? r[1] / r[2] : 0;
    const fill = $("#mf" + i);
    fill.style.width = (f * 100).toFixed(1) + "%";
    fill.classList.toggle("hot", f > (r[0].includes("drive") || r[0] === "Drive" ? 0.9 : 0.85));
    $("#mt" + i).textContent = r[3];
  });
  const up = $("#uptime"); if (up && S.live) up.textContent = fmtUp(S.live.uptime);
}

function lookItems() {
  const items = [], A = S.apps;
  const mig = S.sys && S.sys.migration;
  if (mig && mig.stuck && mig.stuck.length) items.push({ kind: "danger", ic: "alert", t: "An old PopUtil copy needs your password to remove",
    s: `Run this in a terminal: ${mig.fix}`, go: "overview" });
  else if (mig && mig.removed && mig.removed.length && Date.now() / 1000 - mig.when < 7 * 86400)
    items.push({ kind: "safe", ic: "check", t: `Removed ${plural(mig.removed.length, "piece")} of older PopUtil installs`,
      s: "Only this Flatpak version is left. Your settings and Removed items were kept.", go: "overview" });
  const L = S.live || S.sys;
  const disks = (S.live && S.live.disks) || (S.sys && S.sys.disks) || [];
  disks.forEach((d) => { if (d.total && d.free / d.total < 0.1) items.push({ kind: "danger", ic: "alert", t: `${d.path === "/" ? "System" : "Home"} drive is almost full`, s: `Only ${human(d.free)} left. Disk cleanup can find caches and leftovers to clear.`, go: "clean" }); });
  if (!A || !A.scanned) {
    items.push({ ic: "scan", t: "Find out what's installed on this PC", s: "Scan everywhere apps end up: packages, Flatpaks, AppImages, /opt, your home folder and more.", go: "apps", scan: true });
  } else {
    const apps = A.apps;
    const broken = apps.filter((a) => a.source === "broken");
    const left = apps.filter((a) => a.source === "leftover");
    const dupes = apps.filter((a) => (a.reason || "").startsWith("Also installed as"));
    const unused = apps.filter((a) => a.by_user && (a.reason || "").startsWith("Not opened"));
    if (broken.length) items.push({ kind: "danger", ic: "alert", t: `${plural(broken.length, "broken launcher")}`, s: "Shortcuts or commands that point at apps that are gone.", go: "apps", group: "Leftovers" });
    if (left.length) items.push({ ic: "box", t: `${human(left.reduce((s, a) => s + a.size + a.data_size, 0))} left behind by removed apps`, s: `${plural(left.length, "leftover")}, like old Flatpak data and Proton files.`, go: "apps", group: "Leftovers" });
    if (dupes.length) items.push({ ic: "apps", t: `${plural(dupes.length, "app")} installed twice`, s: "The same app from two places, for example a .deb and a Flatpak.", go: "apps", show: "dupes" });
    if (unused.length) items.push({ ic: "restore", t: `${plural(unused.length, "app")} you haven't opened in months`, s: `Together they take ${human(unused.reduce((s, a) => s + a.size + a.data_size, 0))}.`, go: "apps", show: "unused" });
    if (A.held_bytes) items.push({ ic: "trash", t: `${human(A.held_bytes)} waiting in Removed`, s: "Apps you removed are kept until you empty it, so you can still restore them.", go: "apps", tab: "removed" });
    if (!items.length) items.push({ kind: "safe", ic: "check", t: "Nothing needs your attention", s: `Scanned ${ago(A.scanned)}. Everything installed looks accounted for.`, go: "apps" });
  }
  return items;
}

function renderOverview() {
  const box = $("#overview"); if (!box) return;
  const s = S.sys;
  const hr = new Date().getHours();
  const hello = `${hr < 5 ? "Working late" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening"}${s && s.user ? `, ${s.user}` : ""}.`;
  const spec = s ? [["Machine", s.host], ["Processor", `${s.cpu || "Unknown"} (${s.cores} threads)`], ["Graphics", s.gpus.join(" and ") || "Unknown"],
                    ["Memory", human(s.ram)], ["System", s.os], ["Kernel", s.kernel], ["Desktop", s.desktop],
                    ["Packages", `${s.packages.apt.toLocaleString()} system, ${s.packages.flatpak} Flatpak`]]
                 : [];
  if (s && s.nvidia_driver) spec[2][1] += ` (driver ${s.nvidia_driver})`;
  box.innerHTML = `
    <h1 class="hello">${esc(hello)}</h1>
    <p class="hello-sub">${s ? `${esc(s.host)} has been up for <span id="uptime">${S.live ? fmtUp(S.live.uptime) : "a while"}</span>.` : "Reading this machine…"}</p>
    <div class="ov-grid">
      <div>
        <div class="panel meters" id="meters"></div>
        <h2>Worth a look</h2>
        <div class="panel look">${lookItems().map((it) => `
          <button class="lk" data-go="${it.go}" ${it.group ? `data-group="${esc(it.group)}"` : ""} ${it.tab ? `data-tab="${it.tab}"` : ""} ${it.show ? `data-show="${it.show}"` : ""} ${it.scan ? "data-scan" : ""}>
            <span class="mark ${it.kind || ""}">${icon(it.ic)}</span>
            <span class="t"><b>${esc(it.t)}</b><span>${esc(it.s)}</span></span>${icon("next")}</button>`).join("")}
        </div>
      </div>
      <div>
        <div class="panel spec"><dl>${spec.map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("")}</dl></div>
        <h2>Quick actions</h2>
        <div class="quick">
          <button class="btn" data-task="update-all">${icon("get")}Update everything</button>
          <button class="btn" data-task="cleanup">${icon("broom")}Quick system cleanup</button>
          <button class="btn" data-task="launchers">${icon("restore")}Refresh the app library</button>
        </div>
      </div>
    </div>`;
  $$("[data-scan]", box).forEach((b) => b.addEventListener("click", () => scanApps()));
  updateMeters();
}

/* ───────────────────────── apps on this PC ───────────────────────── */
const GROUP_VAR = {};
function groupColor(g) {
  const order = (S.apps && S.apps.groups) || [];
  const i = order.indexOf(g);
  return `var(--g${i < 0 ? 12 : i % 13})`;
}
async function loadApps() {
  S.apps = await api("/api/apps");
  const ids = new Set(S.apps.apps.map((a) => a.id));
  S.sel.forEach((id) => { if (!ids.has(id)) S.sel.delete(id); });
  const n = S.apps.apps.filter((a) => a.source === "broken" || a.source === "leftover").length;
  const b = $("#badgeApps"); b.hidden = !n; b.textContent = n;
  if (S.view === "apps") renderApps();
  if (S.view === "overview") renderOverview();
  if (S.drawerId) renderDrawer(S.drawerId);
}
function scanApps() { if (S.view !== "apps") go("apps"); run("/api/apps/scan", {}, { openLog: false }).then(() => { S.busy = "apps"; renderApps(); }); }

function visibleApps() {
  let list = S.apps ? S.apps.apps : [];
  if (S.scope === "user") list = list.filter((a) => a.by_user || a.source === "leftover" || a.source === "broken");
  if (S.group) list = list.filter((a) => a.group === S.group);
  if (S.show === "review") list = list.filter((a) => ["review", "remove"].includes((a.ai && a.ai.verdict) || a.verdict));
  if (S.show === "nolib") list = list.filter((a) => !a.in_library && a.kind !== "leftover");
  if (S.show === "unused") list = list.filter((a) => (a.reason || "").startsWith("Not opened"));
  if (S.show === "dupes") list = list.filter((a) => (a.reason || "").startsWith("Also installed as"));
  if (S.show === "running") list = list.filter((a) => a.running);
  const q = S.q.trim().toLowerCase();
  if (q) list = list.filter((a) => `${a.name} ${a.desc} ${a.location} ${a.source_label} ${(a.ai && a.ai.what) || ""}`.toLowerCase().includes(q));
  const sorters = {
    size: (a, b) => (b.size + b.data_size) - (a.size + a.data_size),
    unused: (a, b) => (a.last_used || 9e12) - (b.last_used || 9e12),
    name: (a, b) => a.name.localeCompare(b.name),
    newest: (a, b) => (b.installed || 0) - (a.installed || 0),
  };
  return [...list].sort(sorters[S.sort] || sorters.size);
}

function verdictOf(a) {
  const v = (a.ai && a.ai.verdict) || a.verdict;
  const reason = (a.ai && a.ai.reason) || a.reason;
  return v ? { v, reason, ai: !!(a.ai && a.ai.verdict) } : null;
}
const VERDICT_LABEL = { keep: "Keep", review: "Worth a look", remove: "Safe to remove" };

function appIcon(a, lg = false) {
  const c = groupColor(a.group);
  if (a.has_icon) return `<span class="appicon ${lg ? "lg" : ""}" style="--c:${c}"><img alt="" loading="lazy" src="/api/appicon?id=${a.id}&t=${encodeURIComponent(TOKEN)}" data-fallback="${esc((a.name || "?").trim()[0] || "?")}"></span>`;
  return `<span class="appicon mono ${lg ? "lg" : ""}" style="--c:${c}">${esc(((a.name || "?").trim()[0] || "?").toUpperCase())}</span>`;
}
document.addEventListener("error", (e) => {
  const img = e.target;
  if (img.tagName === "IMG" && img.dataset.fallback !== undefined) {
    const span = img.parentElement; span.classList.add("mono"); span.textContent = img.dataset.fallback.toUpperCase();
  }
}, true);

function renderApps() {
  const box = $("#apps"); if (!box) return;
  const A = S.apps;
  const scanning = S.busy === "apps";
  const removedHeld = A ? A.held_bytes : 0;
  const head = `<div class="pagehead"><div><h1>Apps on this PC</h1>
      <p>${A && A.scanned ? `${plural(A.apps.length, "app and tool", "apps and tools")} found in ${new Set(A.apps.map((a) => a.group)).size} kinds of places. Scanned ${ago(A.scanned)}.`
                           : "Everything installed, wherever it came from, and what each one takes up."}</p></div>
      <div class="tools">
        ${A && A.scanned ? `<div class="seg" role="tablist" aria-label="Show">
          <button role="tab" data-apptab="installed" aria-pressed="${S.appTab === "installed"}">Installed</button>
          <button role="tab" data-apptab="removed" aria-pressed="${S.appTab === "removed"}">Removed${removedHeld ? ` (${human(removedHeld)})` : ""}</button></div>` : ""}
        ${A && A.scanned ? `<button class="btn" id="aiBtn" title="Ask ${A.ai === "ollama" ? "Ollama" : "Claude"} what each app is and whether you need it">${icon("spark")}Ask for advice</button>` : ""}
        <button class="btn ${A && A.scanned ? "" : "primary"}" id="scanBtn" ${scanning ? "disabled" : ""}>${icon("scan")}${A && A.scanned ? "Scan again" : "Scan this PC"}</button>
      </div></div>`;
  if (!A) { box.innerHTML = head + `<p class="progress-line">Loading…</p>`; return bindAppsHead(); }
  const progress = scanning ? `<p class="progress-line">${esc(S.lastLine || "Scanning…")}</p>` : "";
  if (!A.scanned) {
    box.innerHTML = head + progress + (scanning ? "" : `<div class="panel empty">
      <b>See everything installed on this PC</b>
      <p style="max-width:58ch;margin:0 auto">Linux apps end up in a lot of places: system packages, .deb files, Flatpak, Snap, AppImages, folders in /opt,
      install scripts in /usr/local, your home folder, pip, npm, cargo, game engines, Wine and Steam. PopUtil looks in all of them and shows where each one lives.</p>
      <button class="btn primary" id="scanBtn2">${icon("scan")}Scan this PC</button></div>`);
    bindAppsHead();
    $("#scanBtn2")?.addEventListener("click", scanApps);
    return;
  }
  if (S.appTab === "removed") { box.innerHTML = head + renderRemoved(); bindAppsHead(); bindRemoved(); return; }

  // where apps live
  const base = S.scope === "user" ? A.apps.filter((a) => a.by_user || a.source === "leftover" || a.source === "broken") : A.apps;
  const groups = {};
  base.forEach((a) => { const g = (groups[a.group] ||= { n: 0, size: 0 }); g.n++; g.size += a.size + a.data_size; });
  const order = A.groups.filter((g) => groups[g]);
  const total = order.reduce((s, g) => s + groups[g].size, 0) || 1;
  const strata = `<div class="strata-wrap"><h2 style="margin-top:0">Where your apps live <small>${human(total)} in total</small></h2>
    <div class="strata ${S.group ? "filtered" : ""}" role="group" aria-label="Filter by where apps live">${order.map((g) => {
      const share = groups[g].size / total;
      return `<button class="stratum" style="--c:${groupColor(g)};flex:${Math.max(share * 100, 1.2).toFixed(2)} 1 0" data-group-f="${esc(g)}" aria-pressed="${S.group === g}" title="${esc(g)}: ${plural(groups[g].n, "item")}, ${human(groups[g].size)}">${share > 0.09 ? `<span>${esc(g)}</span>` : ""}</button>`;
    }).join("")}</div>
    <div class="legend">${order.map((g) => `<button data-group-f="${esc(g)}" aria-pressed="${S.group === g}" style="--c:${groupColor(g)}"><i></i><span><b>${esc(g)}</b> ${groups[g].n}</span><em>${human(groups[g].size)}</em></button>`).join("")}</div></div>`;

  const list = visibleApps();
  const shown = list.slice(0, S.limit);
  const tools = `<div class="applist-tools">
      <label class="field-search">${icon("search")}<span class="sr">Search apps</span><input id="appQ" placeholder="Search by name, place or description" value="${esc(S.q)}" autocomplete="off"><kbd>/</kbd></label>
      <div class="seg" aria-label="Which apps">
        <button data-scope="user" aria-pressed="${S.scope === "user"}">Installed by you</button>
        <button data-scope="all" aria-pressed="${S.scope === "all"}">Everything</button></div>
      <select class="sel" id="appShow" aria-label="Filter">
        ${[["all", "All of them"], ["review", "Worth reviewing"], ["unused", "Not opened in months"], ["dupes", "Installed twice"], ["nolib", "Not in the app library"], ["running", "Open right now"]]
          .map(([v, l]) => `<option value="${v}" ${S.show === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      <select class="sel" id="appSort" aria-label="Sort">
        ${[["size", "Largest first"], ["unused", "Longest unused"], ["newest", "Newest first"], ["name", "Name"]].map(([v, l]) => `<option value="${v}" ${S.sort === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      <span class="grow"></span>
      ${S.group ? `<button class="btn quiet" id="clearGroup">${icon("close")}${esc(S.group)}</button>` : ""}
    </div>`;
  const rows = shown.length ? shown.map(appRow).join("") + (list.length > shown.length ? `<div class="more-row"><button class="btn" id="moreApps">Show ${plural(list.length - shown.length, "more")}</button></div>` : "")
                            : `<div class="empty"><b>Nothing matches</b>${S.q ? `No app matches “${esc(S.q)}”.` : "Try another filter, or show everything."}</div>`;
  box.innerHTML = head + progress + strata + tools + `<div class="panel applist" id="applist">${rows}</div>`;
  bindAppsHead();
  $$("[data-group-f]", box).forEach((b) => (b.onclick = () => { S.group = S.group === b.dataset.groupF ? null : b.dataset.groupF; S.limit = 120; renderApps(); }));
  $$("[data-scope]", box).forEach((b) => (b.onclick = () => { S.scope = b.dataset.scope; renderApps(); }));
  $("#appShow").onchange = (e) => { S.show = e.target.value; renderApps(); };
  $("#appSort").onchange = (e) => { S.sort = e.target.value; renderApps(); };
  $("#clearGroup")?.addEventListener("click", () => { S.group = null; renderApps(); });
  $("#moreApps")?.addEventListener("click", () => { S.limit += 200; renderApps(); });
  const qi = $("#appQ");
  qi.oninput = () => { S.q = qi.value; S.limit = 120; renderAppListOnly(); };
  bindRows();
}
function renderAppListOnly() {
  const list = visibleApps(), shown = list.slice(0, S.limit);
  $("#applist").innerHTML = shown.length ? shown.map(appRow).join("") : `<div class="empty"><b>Nothing matches</b>No app matches “${esc(S.q)}”.</div>`;
  bindRows();
}
function appRow(a) {
  const v = verdictOf(a);
  const used = a.running ? "Open right now" : a.last_used ? `Used ${ago(a.last_used)}` : "";
  const label = (a.ai && a.ai.what) || a.source_label;
  return `<div class="app-row ${S.sel.has(a.id) ? "selected" : ""}" data-app="${a.id}" tabindex="0" role="button" aria-label="${esc(a.name)}, ${esc(a.source_label)}">
    ${a.removable ? `<input type="checkbox" data-sel="${a.id}" ${S.sel.has(a.id) ? "checked" : ""} aria-label="Select ${esc(a.name)}">` : "<span></span>"}
    ${appIcon(a)}
    <span class="who"><b>${esc(a.name)}${a.version ? `<small>${esc(a.version)}</small>` : ""}</b>
      <span><span class="src" style="--c:${groupColor(a.group)}">${esc(label)}</span>&ensp;${esc(a.location_t)}</span></span>
    <span class="used">${esc(used)}</span>
    <span class="size">${human(a.size)}${a.data_size ? `<small>+ ${human(a.data_size)} data</small>` : ""}</span>
    <span class="verdict">${a.running ? `<span class="pill run">Open</span>` : v ? `<span class="pill ${v.v} ${v.ai ? "ai" : ""}" title="${esc(v.reason || "")}">${VERDICT_LABEL[v.v]}</span>` : ""}</span>
  </div>`;
}
function bindRows() {
  $$("#applist [data-app]").forEach((row) => {
    row.onclick = (e) => { if (e.target.matches("input")) return; openDrawer(row.dataset.app); };
    row.onkeydown = (e) => {
      if (e.key === "Enter") openDrawer(row.dataset.app);
      if (e.key === " ") { e.preventDefault(); const cb = $("input", row); if (cb) { cb.checked = !cb.checked; cb.onchange(); } }
    };
  });
  $$("#applist [data-sel]").forEach((cb) => (cb.onchange = () => {
    cb.checked ? S.sel.add(cb.dataset.sel) : S.sel.delete(cb.dataset.sel);
    cb.closest(".app-row").classList.toggle("selected", cb.checked);
    renderSelbar();
  }));
}
function bindAppsHead() {
  $("#scanBtn")?.addEventListener("click", scanApps);
  $("#aiBtn")?.addEventListener("click", () => {
    if (S.apps.ai === "none") { toast("Add a Claude key or an Ollama model in Settings first.", "err"); openSettings(); return; }
    const ids = visibleApps().slice(0, 300).map((a) => a.id);
    run("/api/apps/ai", { ids });
    toast(`Asking about ${plural(ids.length, "app")}. Advice shows up in the list as it arrives.`);
  });
  $$("[data-apptab]").forEach((b) => (b.onclick = () => { S.appTab = b.dataset.apptab; renderApps(); renderSelbar(); }));
}

/* ── removed apps ── */
function renderRemoved() {
  const runs = S.apps.removed || [];
  const held = runs.filter((r) => r.state === "held");
  if (!runs.length) return `<div class="panel empty"><b>Nothing removed yet</b>When you remove apps, their files wait here so you can restore them.</div>`;
  return `<p class="lede">Removed apps wait here until you empty it, so you can change your mind. Packages that came from a store are reinstalled when you restore them.</p>
    ${held.length ? `<div class="applist-tools"><span class="grow"></span><button class="btn" id="emptyRemoved">${icon("trash")}Empty Removed (${human(S.apps.held_bytes)})</button></div>` : ""}
    <div class="panel rows">${runs.map((r) => `
      <div class="row"><div class="t"><b>${esc(r.apps.map((a) => a.name).slice(0, 4).join(", "))}${r.apps.length > 4 ? ` and ${r.apps.length - 4} more` : ""}</b>
        <span>${new Date(r.when * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}${r.state === "held" ? (r.held_bytes ? `, holding ${human(r.held_bytes)}` : "") : r.state === "restored" ? ", restored" : ", deleted for good"}</span></div>
        ${r.state === "held" ? `<button class="btn" data-restore="${r.id}">${icon("restore")}Restore</button><button class="btn quiet" data-purge="${r.id}">Delete for good</button>` : ""}
      </div>`).join("")}</div>`;
}
function bindRemoved() {
  $$("[data-restore]").forEach((b) => (b.onclick = () => run("/api/apps/restore", { id: b.dataset.restore })));
  $$("[data-purge]").forEach((b) => (b.onclick = () => { if (confirm("Delete these files for good? This can't be undone.")) run("/api/apps/purge", { id: b.dataset.purge }); }));
  $("#emptyRemoved")?.addEventListener("click", () => { if (confirm(`Delete everything in Removed (${human(S.apps.held_bytes)}) for good? This can't be undone.`)) run("/api/apps/purge", { all: true }); });
}

/* ── app drawer ── */
let lastFocus = null;
function openDrawer(id) {
  lastFocus = document.activeElement;
  S.drawerId = id;
  renderDrawer(id);
  $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  setTimeout(() => $("#drawer .x")?.focus(), 60);
}
function closeDrawer() {
  S.drawerId = null;
  $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true");
  if (!$("#sheet").classList.contains("open")) $("#scrim").classList.remove("open");
  lastFocus?.focus?.();
}
const ROLE = { program: "Program", command: "Command", launcher: "Launcher", service: "Service", settings: "Settings", data: "Data",
               cache: "Cache", icon: "Icon", shortcut: "Shortcut", models: "Models" };
function renderDrawer(id) {
  const a = S.apps && S.apps.apps.find((x) => x.id === id);
  const d = $("#drawer");
  if (!a) { closeDrawer(); return; }
  const v = verdictOf(a);
  const lib = a.in_library ? "Yes. It has a launcher in the app library." : a.kind === "leftover" ? "No. These are files an app left behind." : "No. It doesn't show up in the app library, so you'd only find it here or in a terminal.";
  const withData = S.withData.has(a.id);
  d.innerHTML = `
    <header>${appIcon(a, true)}<div><h3 id="drawerTitle">${esc(a.name)}</h3><p>${esc(a.source_label)}${a.version ? `, version ${esc(a.version)}` : ""}</p></div>
      <button class="btn quiet x" aria-label="Close">${icon("close")}</button></header>
    <div class="body">
      ${v ? `<p style="margin:0 0 18px"><span class="pill ${v.v} ${v.ai ? "ai" : ""}">${VERDICT_LABEL[v.v]}</span>&ensp;<span style="color:var(--muted)">${esc(v.reason || "")}${v.ai ? ` (advice from ${a.ai.by === "ollama" ? "Ollama" : "Claude"})` : ""}</span></p>` : ""}
      <div class="facts">
        <p><span class="lbl">What it is</span>${(a.ai && a.ai.what) || a.desc ? esc((a.ai && a.ai.what) || a.desc) : `<span class="na">PopUtil doesn't know yet.</span> <button class="btn quiet" id="dwAsk" style="padding:2px 8px">${icon("spark")}Ask for advice</button>`}</p>
        <p><span class="lbl">How it got here</span>${esc(a.how || a.source_label)}</p>
        <p><span class="lbl">In your app library</span>${esc(lib)}</p>
        <p><span class="lbl">Last used</span>${a.running ? "It's open right now." : a.last_used ? esc(ago(a.last_used).replace(/^./, (c) => c.toUpperCase())) : "PopUtil can't tell."}</p>
        <p><span class="lbl">Space</span>${human(a.size)} for the app${a.data_size ? `, plus ${human(a.data_size)} of settings and data` : ""}</p>
      </div>
      <div class="where"><h4>Where it lives</h4>
        ${a.parts.map((p) => `<div class="part"><span class="role">${ROLE[p.role] || esc(p.role)}</span><span class="p">${esc(p.path_t)}${p.note ? `<small>${esc(p.note)}</small>` : ""}${p.root ? `<small>${icon("lock")} Needs your password to change</small>` : ""}</span><span class="sz">${p.size ? human(p.size) : ""}</span></div>`).join("") || `<p class="na">No files recorded.</p>`}
      </div>
      ${a.notes && a.notes.length ? `<div class="notes">${a.notes.map((n) => `<p>${esc(n)}</p>`).join("")}</div>` : ""}
    </div>
    <div class="foot">
      ${a.location ? `<button class="btn quiet" id="dwReveal" title="Open its folder in Files">${icon("folder")}Show in Files</button>` : ""}
      ${a.protected ? `<span class="na">${esc(a.protected)}, so PopUtil won't remove it.</span>`
        : a.external ? `<span class="na">${esc(a.external)}</span>`
        : `${a.data_size && a.kind !== "leftover" ? `<label class="check"><input type="checkbox" id="dwData" ${withData ? "checked" : ""}><span>Also remove its settings and data<small>${human(a.data_size)}. Kept by default in case you reinstall.</small></span></label>` : ""}
           <span style="flex:1"></span><button class="btn danger" id="dwRemove">${icon("trash")}Remove…</button>`}
    </div>`;
  $(".x", d).onclick = closeDrawer;
  $("#dwAsk")?.addEventListener("click", () => {
    if (S.apps.ai === "none") { toast("Add a Claude key or an Ollama model in Settings first.", "err"); openSettings(); return; }
    run("/api/apps/ai", { ids: [a.id] });
  });
  $("#dwData")?.addEventListener("change", (e) => { e.target.checked ? S.withData.add(a.id) : S.withData.delete(a.id); });
  $("#dwRemove")?.addEventListener("click", () => openReview([a.id]));
  $("#dwReveal")?.addEventListener("click", () => api("/api/apps/reveal", { id: a.id }).catch((e) => toast(e.message, "err")));
}

/* ── removal review ── */
async function openReview(ids) {
  const sh = $("#sheet");
  sh.innerHTML = `<header><h3 id="sheetTitle">Checking what removing ${ids.length === 1 ? "it" : "them"} would do…</h3></header><div class="body"><p class="progress-line">Simulating the removal. Nothing changes yet.</p></div>`;
  sh.classList.add("open"); sh.setAttribute("aria-hidden", "false"); $("#scrim").classList.add("open");
  let plans;
  try { plans = (await api("/api/apps/plan", { ids, with_data: [...S.withData] })).plans; }
  catch (e) { closeReview(); toast(e.message, "err"); return; }
  const ok = plans.filter((p) => !p.blocked), blocked = plans.filter((p) => p.blocked);
  const frees = ok.reduce((s, p) => s + p.frees, 0), held = ok.reduce((s, p) => s + p.held, 0);
  const pw = ok.some((p) => p.needs_password);
  sh.innerHTML = `
    <header><h3 id="sheetTitle">${ok.length ? `Remove ${ok.length === 1 ? esc(ok[0].name) : plural(ok.length, "app")}?` : "These can't be removed"}</h3>
      <p>${ok.length ? "Here's exactly what PopUtil will do. Nothing has changed yet." : "PopUtil stopped before changing anything."}</p></header>
    <div class="body">${plans.map((p) => {
      const a = S.apps.apps.find((x) => x.id === p.id) || { name: p.name, group: "", has_icon: false, id: p.id };
      return `<div class="plan ${p.blocked ? "blocked" : ""}"><div class="ph">${appIcon(a)}<span><b>${esc(p.name)}</b><br><span>${esc(p.source_label)}</span></span></div>
        ${p.blocked ? `<p class="blockmsg">${esc(p.blocked)}</p>` : `<ul>${p.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
          ${p.warnings.map((w) => `<p class="warn">${esc(w)}</p>`).join("")}
          ${p.data_size && a.kind !== "leftover" ? `<label class="check" style="margin-top:10px"><input type="checkbox" data-plan-data="${p.id}" ${p.with_data ? "checked" : ""}><span>Also remove its settings and data (${human(p.data_size)})</span></label>` : ""}
          <details><summary>Show the exact steps</summary>${p.commands.map((c) => `<code>${esc(c)}</code>`).join("")}</details>`}
      </div>`; }).join("")}</div>
    <div class="foot">
      ${ok.length ? `<span class="sum">${[frees ? `Frees about ${human(frees)} now.` : "", held ? `${human(held)} moves to Removed until you empty it.` : "", pw ? "Asks for your password once." : ""].filter(Boolean).join(" ")}</span>` : ""}
      <button class="btn quiet" id="rvCancel">${ok.length ? "Cancel" : "Close"}</button>
      ${ok.length ? `<button class="btn danger" id="rvGo">${icon("trash")}Remove ${ok.length === 1 ? "it" : ok.length}</button>` : ""}
    </div>`;
  $$("[data-plan-data]", sh).forEach((cb) => (cb.onchange = () => { cb.checked ? S.withData.add(cb.dataset.planData) : S.withData.delete(cb.dataset.planData); openReview(ids); }));
  $("#rvCancel").onclick = closeReview;
  $("#rvGo")?.addEventListener("click", async () => {
    const j = await run("/api/apps/remove", { ids: ok.map((p) => p.id), with_data: [...S.withData] });
    if (!j) return;
    ok.forEach((p) => S.sel.delete(p.id));
    closeReview(); closeDrawer(); renderSelbar();
    if (j.skipped && j.skipped.length) toast(`Skipped: ${j.skipped.join(", ")}`, "err");
  });
  setTimeout(() => ($("#rvGo") || $("#rvCancel")).focus(), 60);
}
function closeReview() {
  const sh = $("#sheet");
  sh.classList.remove("open"); sh.setAttribute("aria-hidden", "true");
  if (!$("#drawer").classList.contains("open")) $("#scrim").classList.remove("open");
}
$("#scrim").onclick = () => { if ($("#sheet").classList.contains("open")) closeReview(); else closeDrawer(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if ($("#sheet").classList.contains("open")) closeReview();
    else if ($("#drawer").classList.contains("open")) closeDrawer();
  }
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    const q = $(S.view === "apps" ? "#appQ" : S.view === "get" ? "#catQ" : "#none");
    if (q) { e.preventDefault(); q.focus(); }
  }
});

/* ───────────────────────── selection bar ───────────────────────── */
function renderSelbar() {
  const bar = $("#selbar");
  let text = "", btns = "";
  if (S.view === "apps" && S.appTab === "installed" && S.sel.size && S.apps) {
    const chosen = S.apps.apps.filter((a) => S.sel.has(a.id));
    text = `${plural(chosen.length, "app")} selected, ${human(chosen.reduce((s, a) => s + a.size, 0))}`;
    btns = `<button class="btn quiet" data-sb="clear">Clear</button><button class="btn danger" data-sb="review">${icon("trash")}Review removal</button>`;
  } else if (S.view === "get" && S.catSel.size && S.catalog) {
    const chosen = S.catalog.apps.filter((a) => S.catSel.has(a.id));
    const ins = chosen.filter((a) => !a.installed), rem = chosen.filter((a) => a.installed);
    text = `${plural(chosen.length, "app")} selected`;
    btns = `<button class="btn quiet" data-sb="catclear">Clear</button>${rem.length ? `<button class="btn" data-sb="uninstall">Uninstall ${rem.length}</button>` : ""}${ins.length ? `<button class="btn primary" data-sb="install">${icon("get")}Install ${ins.length}</button>` : ""}`;
  } else if (S.view === "clean" && S.cleanSel.size && S.clean) {
    const chosen = S.clean.items.filter((i) => S.cleanSel.has(i.id));
    text = `${plural(chosen.length, "item")} selected, ${human(chosen.reduce((s, i) => s + i.size, 0))}`;
    btns = `<button class="btn quiet" data-sb="cleanclear">Clear</button><button class="btn primary" data-sb="clean">${icon("broom")}Clear them</button>`;
  } else if (S.view === "organize" && S.orgSel.size) {
    text = `${plural(S.orgSel.size, "move")} selected`;
    btns = `<button class="btn quiet" data-sb="orgclear">Clear</button><button class="btn primary" data-sb="org">${icon("folder")}Organize ${S.orgSel.size}</button>`;
  }
  bar.classList.toggle("show", !!btns);
  if (!btns) return;
  $("#selText").textContent = text;
  $("#selBtns").innerHTML = btns;
}
$("#selBtns").addEventListener("click", (e) => {
  const b = e.target.closest("[data-sb]"); if (!b) return;
  const act = b.dataset.sb;
  if (act === "clear") { S.sel.clear(); renderApps(); }
  if (act === "review") openReview([...S.sel]);
  if (act === "catclear") { S.catSel.clear(); renderCatalog(); }
  if (act === "install" || act === "uninstall") {
    const ids = S.catalog.apps.filter((a) => S.catSel.has(a.id) && (act === "install" ? !a.installed : a.installed)).map((a) => a.id);
    if (act === "uninstall" && !confirm(`Uninstall ${plural(ids.length, "app")}?`)) return;
    run(`/api/catalog/${act === "install" ? "install" : "remove"}`, { ids }); ids.forEach((i) => S.catSel.delete(i)); renderCatalog();
  }
  if (act === "cleanclear") { S.cleanSel.clear(); renderClean(); }
  if (act === "clean") {
    const items = S.clean.items.filter((i) => S.cleanSel.has(i.id));
    if (!confirm(`Clear ${plural(items.length, "item")}, about ${human(items.reduce((s, i) => s + i.size, 0))}?\n\nCaches and build files are deleted. Personal files go to the Trash.`)) return;
    run("/api/clean/apply", { ids: items.map((i) => String(i.id)) }); S.cleanSel.clear();
  }
  if (act === "orgclear") { S.orgSel.clear(); renderOrg(); }
  if (act === "org") {
    if (!confirm(`Move ${plural(S.orgSel.size, "item")}? Launchers, project lists and shortcuts that point at them are updated, and you can undo the whole run.`)) return;
    run("/api/organize/apply", { ids: [...S.orgSel].map(String) }); S.orgSel.clear();
  }
  renderSelbar();
});

/* ───────────────────────── get apps ───────────────────────── */
async function loadCatalog() { S.catalog = await api("/api/catalog"); renderCatalog(); }
let catQ = "";
function renderCatalog() {
  const box = $("#get"); if (!box) return;
  const C = S.catalog;
  const n = S.catSel.size, b = $("#badgeGet"); b.hidden = !n; b.textContent = n;
  if (!C) { box.innerHTML = `<div class="pagehead"><div><h1>Get apps</h1></div></div><p class="progress-line">Loading the catalog…</p>`; return; }
  const q = catQ.trim().toLowerCase();
  const cats = {};
  C.apps.filter((a) => !q || `${a.name} ${a.desc} ${a.cat}`.toLowerCase().includes(q)).forEach((a) => (cats[a.cat] ||= []).push(a));
  box.innerHTML = `<div class="pagehead"><div><h1>Get apps</h1><p>Pick a few, then install them together. Flatpaks install without your password.</p></div>
      <div class="tools"><label class="field-search">${icon("search")}<span class="sr">Search the catalog</span><input id="catQ" placeholder="Search apps" value="${esc(catQ)}"><kbd>/</kbd></label></div></div>
    <div class="chips">${Object.keys(C.presets).map((p) => `<button class="chip" data-preset="${esc(p)}">Add the ${esc(p.toLowerCase())}</button>`).join("")}</div>
    ${Object.keys(cats).length ? Object.entries(cats).map(([c, list]) => `<h2>${esc(c)}</h2><div class="panel catalog-panel"><div class="catalog">${list.map((a) => `
      <button class="cat-item" data-cat="${a.id}" aria-pressed="${S.catSel.has(a.id)}"><span class="box"></span>
        <span><b>${esc(a.name)}</b><span>${esc(a.desc)}</span><em class="${a.installed ? "on" : ""}">${a.installed ? "Installed" : a.source === "apt" ? "System package" : "Flatpak"}</em></span></button>`).join("")}</div></div>`).join("")
      : `<div class="panel empty"><b>Not in the catalog</b>Add it to apps.json in PopUtil's folder and it shows up here.</div>`}`;
  $("#catQ").oninput = (e) => { catQ = e.target.value; const pos = e.target.selectionStart; renderCatalog(); const i = $("#catQ"); i.focus(); i.setSelectionRange(pos, pos); };
  $$("[data-cat]", box).forEach((el) => (el.onclick = () => { const id = el.dataset.cat; S.catSel.has(id) ? S.catSel.delete(id) : S.catSel.add(id); el.setAttribute("aria-pressed", S.catSel.has(id)); const b = $("#badgeGet"); b.hidden = !S.catSel.size; b.textContent = S.catSel.size; renderSelbar(); }));
  $$("[data-preset]", box).forEach((el) => (el.onclick = () => {
    let added = 0;
    C.presets[el.dataset.preset].forEach((id) => { const a = C.apps.find((x) => x.id === id); if (a && !a.installed && !S.catSel.has(id)) { S.catSel.add(id); added++; } });
    renderCatalog(); renderSelbar();
    toast(added ? `Selected ${plural(added, "app")} from the ${el.dataset.preset.toLowerCase()}` : `Everything in the ${el.dataset.preset.toLowerCase()} is already installed`);
  }));
}

/* ───────────────────────── tweaks & maintenance ───────────────────────── */
async function loadTweaks() { S.tweaks = await api("/api/tweaks"); renderTweaks(); }
const lockNote = `<span class="lock">${icon("lock")}Asks for your password</span>`;
function renderTweaks() {
  const box = $("#tweaks"); if (!box) return;
  const T = S.tweaks;
  if (!T) { box.innerHTML = `<div class="pagehead"><div><h1>Tweaks</h1></div></div><p class="progress-line">Reading the current settings…</p>`; return; }
  const names = { battery: "Battery saver", "power-saver": "Battery saver", balanced: "Balanced", performance: "Performance" };
  const groups = {}; T.tweaks.forEach((t) => (groups[t.group] ||= []).push(t));
  box.innerHTML = `<div class="pagehead"><div><h1>Tweaks</h1><p>Every switch shows how your system is set right now. Turning one off puts things back the way they were.</p></div></div>
    <h2 style="margin-top:0">Power</h2>
    <div class="panel rows"><div class="row"><div class="t"><b>Power profile</b><span>How hard the processor and graphics are allowed to work.</span></div>
      ${T.power ? `<div class="seg" role="group" aria-label="Power profile">${T.power.options.map((o) => `<button data-power="${o}" aria-pressed="${o === T.power.current}">${names[o] || o}</button>`).join("")}</div>` : `<span class="na">Not available on this machine</span>`}</div></div>
    ${Object.entries(groups).map(([g, list]) => `<h2>${esc(g)}</h2><div class="panel rows">${list.map((t) => `
      <div class="row"><div class="t"><b>${esc(t.name)}</b><span>${esc(t.desc)}${t.note ? ` <span class="note">${esc(t.note)}</span>` : ""}</span></div>
        ${t.root ? lockNote : ""}
        ${t.on === null ? `<span class="na">Not available</span>` : `<button class="switch" role="switch" aria-label="${esc(t.name)}" aria-checked="${!!t.on}" data-tweak="${t.id}" ${t.on && !t.can_off ? "disabled" : ""}></button>`}
      </div>`).join("")}</div>`).join("")}`;
  $$("[data-power]", box).forEach((b) => (b.onclick = () => { $$("[data-power]", box).forEach((x) => x.setAttribute("aria-pressed", x === b)); run("/api/power", { value: b.dataset.power }, { openLog: false }); }));
  $$("[data-tweak]", box).forEach((s) => (s.onclick = () => { const on = s.getAttribute("aria-checked") !== "true"; s.setAttribute("aria-checked", on); s.classList.add("pending"); run("/api/tweak", { id: s.dataset.tweak, on }); }));
}
async function loadTasks() { S.tasks = await api("/api/tasks"); renderTasks(); }
function renderTasks() {
  const box = $("#tasks"); if (!box) return;
  if (!S.tasks) { box.innerHTML = `<div class="pagehead"><div><h1>Maintenance</h1></div></div>`; return; }
  const groups = {}; S.tasks.tasks.forEach((t) => (groups[t.group] ||= []).push(t));
  box.innerHTML = `<div class="pagehead"><div><h1>Maintenance</h1><p>One click each. What they do shows in Activity at the bottom.</p></div></div>
    ${Object.entries(groups).map(([g, list], i) => `<h2 ${i ? "" : 'style="margin-top:0"'}>${esc(g)}</h2><div class="panel rows">${list.map((t) => `
      <div class="row"><div class="t"><b>${esc(t.name)}</b><span>${esc(t.desc)}</span></div>${t.root ? lockNote : ""}<button class="btn" data-task="${t.id}">Run</button></div>`).join("")}</div>`).join("")}`;
}

/* ───────────────────────── disk cleanup ───────────────────────── */
const AI_TEXT = { claude: ["Reviewed by Claude", "Claude rates each item. Only paths, sizes and ages are sent."],
                  ollama: ["Reviewed by Ollama", "A model on this PC rates each item. Nothing leaves it."],
                  none: ["Rules only", "Built-in rules rate each item. Add Claude or Ollama in Settings for smarter advice."] };
async function loadClean() { S.clean = await api("/api/clean"); renderClean(); }
function renderClean() {
  const box = $("#clean"); if (!box) return;
  const C = S.clean;
  const [aiName, aiDesc] = AI_TEXT[(C && C.ai) || "none"];
  const scanning = S.busy === "clean";
  let body = "";
  if (scanning) body = `<p class="progress-line">${esc(S.lastLine || "Looking for clutter…")}</p>`;
  else if (!C || !C.scanned) body = `<div class="panel empty"><b>No scan yet</b>Finds caches, build leftovers, old downloads and big forgotten files. Nothing is deleted until you choose.</div>`;
  else if (!C.items.length) body = `<div class="panel empty"><b>All clear</b>Nothing above the size limit is worth clearing right now.</div>`;
  else {
    const safe = C.items.filter((i) => i.verdict === "safe");
    body = `${C.summary ? `<p class="lede">${esc(C.summary)}</p>` : ""}
      <div class="applist-tools"><span class="na">Scanned ${ago(C.scanned)}. About ${human(safe.reduce((s, i) => s + i.size, 0))} is safe to clear.</span><span class="grow"></span><button class="btn" id="pickSafe">Select everything safe</button></div>
      <div class="panel">${C.items.map((i) => `
        <label class="citem"><input type="checkbox" data-c="${i.id}" ${S.cleanSel.has(i.id) ? "checked" : ""}>
          <span><span class="pill ${i.verdict === "safe" ? "keep" : i.verdict === "review" ? "review" : "remove"}">${i.verdict === "safe" ? "Safe" : i.verdict === "review" ? "Your call" : "Keep"}</span></span>
          <span class="sz">${i.size ? human(i.size) : "–"}</span>
          <span class="w"><b>${esc(i.category)}</b> <span>${esc(i.reason)}</span><small>${esc(i.action === "command" ? `${i.display}, runs ${i.command}` : i.display)}</small></span></label>`).join("")}</div>`;
  }
  box.innerHTML = `<div class="pagehead"><div><h1>Disk cleanup</h1><p>Caches, build leftovers and forgotten files. You choose what goes.</p></div>
      <div class="tools"><span class="na" title="${esc(aiDesc)}">${esc(aiName)}</span><button class="btn quiet" id="cleanAi">Change</button><button class="btn primary" id="cleanScan" ${scanning ? "disabled" : ""}>${icon("scan")}${C && C.scanned ? "Scan again" : "Scan for clutter"}</button></div></div>${body}`;
  $("#cleanScan").onclick = () => { S.cleanSel.clear(); S.busy = "clean"; run("/api/clean/scan", {}, { openLog: false }); renderClean(); };
  $("#cleanAi").onclick = openSettings;
  $("#pickSafe")?.addEventListener("click", () => { C.items.filter((i) => i.verdict === "safe").forEach((i) => S.cleanSel.add(i.id)); renderClean(); renderSelbar(); });
  $$("[data-c]", box).forEach((c) => (c.onchange = () => { c.checked ? S.cleanSel.add(+c.dataset.c) : S.cleanSel.delete(+c.dataset.c); renderSelbar(); }));
}

/* ───────────────────────── organize ───────────────────────── */
async function loadOrg() { S.org = await api("/api/organize"); renderOrg(); }
let orgInstr = "";
function renderOrg() {
  const box = $("#organize"); if (!box) return;
  const O = S.org;
  const [aiName] = AI_TEXT[(O && O.ai) || "none"];
  const scanning = S.busy === "organize";
  let body = "";
  if (scanning) body = `<p class="progress-line">${esc(S.lastLine || "Planning…")}</p>`;
  else if (!O || !O.scanned) body = `<div class="panel empty"><b>No plan yet</b>Plan to see suggested moves. Nothing moves until you approve it.</div>`;
  else {
    const ok = O.ops.filter((o) => !o.blocked), blocked = O.ops.filter((o) => o.blocked);
    const groups = {}; ok.forEach((o) => (groups[o.merge ? o.dst_t : o.dst_t.replace(/\/[^/]+$/, "")] ||= []).push(o));
    const row = (o) => `<label class="citem" style="grid-template-columns:22px minmax(0,1fr)"><input type="checkbox" data-o="${o.id}" ${o.blocked ? "disabled" : S.orgSel.has(o.id) ? "checked" : ""}>
      <span class="w"><b>${esc(o.src_t)}</b><small>${o.blocked ? "" : `${o.merge ? "Contents merge into" : "Ends up at"} ${esc(o.dst_t)}`}</small><span>${o.blocked ? "Skipped: " + esc(o.blocked) : esc(o.reason)}</span>
      <div class="tags">${o.op === "relocate" && !o.blocked ? `<span class="tag">Leaves a link behind so everything keeps working</span>` : ""}${(o.blocked ? [] : o.fixups).map((f) => `<span class="tag">Updates ${esc(f)}</span>`).join("")}${o.warnings.map((w) => `<span class="tag warn">${esc(w)}</span>`).join("")}</div></span></label>`;
    body = `${O.summary ? `<p class="lede">${esc(O.summary)}</p>` : ""}
      ${ok.length ? `<div class="applist-tools"><span class="na">${plural(ok.length, "move")} planned${blocked.length ? `, ${blocked.length} skipped for safety` : ""}.</span><span class="grow"></span><button class="btn" id="orgAll">Select all moves</button></div>` : O.ops.length ? `<p class="na">All planned moves are done. Plan again any time.</p>` : `<div class="panel empty"><b>Nothing to move</b>Everything already looks sensibly placed.</div>`}
      ${Object.entries(groups).map(([d, list]) => `<h2>${esc(d)}</h2><div class="panel">${list.map(row).join("")}</div>`).join("")}
      ${blocked.length ? `<h2>Skipped for safety</h2><div class="panel">${blocked.map(row).join("")}</div>` : ""}`;
  }
  const hist = O && O.history && O.history.length ? `<h2>History</h2><div class="panel rows">${O.history.map((r) => `
      <div class="row"><div class="t"><b>${new Date(r.when * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</b><span>${plural(r.moved, "item")} moved${r.undone ? ", then undone" : ""}</span></div>
      ${r.undone || !r.moved ? "" : `<button class="btn" data-undo="${esc(r.id)}">${icon("restore")}Undo</button>`}</div>`).join("")}</div>` : "";
  box.innerHTML = `<div class="pagehead"><div><h1>Organize files</h1><p>Plans a tidier layout for your home folder and drives. You approve every move, and anything that pointed at the old spot is updated.</p></div>
      <div class="tools"><span class="na">${esc(aiName)}</span><button class="btn quiet" id="orgAi">Change</button></div></div>
    <div class="panel" style="padding:18px 20px;margin-bottom:24px;display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap">
      <label style="flex:1;min-width:280px"><span class="na" style="display:block;margin-bottom:6px">Anything specific? (optional)</span>
        <textarea class="instr" id="orgInstr" rows="2" placeholder="Keep school files in Documents/School. Put Godot projects in Projects/Godot.">${esc(orgInstr)}</textarea></label>
      <button class="btn primary" id="orgScan" ${scanning ? "disabled" : ""}>${icon("folder")}Plan</button></div>
    ${body}${hist}`;
  $("#orgInstr").oninput = (e) => (orgInstr = e.target.value);
  $("#orgScan").onclick = () => { S.orgSel.clear(); S.busy = "organize"; run("/api/organize/scan", { instructions: orgInstr }, { openLog: false }); renderOrg(); };
  $("#orgAi").onclick = openSettings;
  $("#orgAll")?.addEventListener("click", () => { O.ops.filter((o) => !o.blocked).forEach((o) => S.orgSel.add(o.id)); renderOrg(); renderSelbar(); });
  $$("[data-o]", box).forEach((c) => (c.onchange = () => { c.checked ? S.orgSel.add(+c.dataset.o) : S.orgSel.delete(+c.dataset.o); renderSelbar(); }));
  $$("[data-undo]", box).forEach((b) => (b.onclick = () => { if (confirm("Put everything from this run back where it was?")) run("/api/organize/undo", { id: b.dataset.undo }); }));
}

/* ───────────────────────── settings ───────────────────────── */
async function openSettings() {
  const s = await api("/api/settings").catch(() => ({}));
  const f = $("#settingsForm");
  f.ai.value = s.ai || (s.has_key ? "claude" : "none");
  f.anthropic_key.value = ""; f.anthropic_key.placeholder = s.has_key ? "Saved. Leave blank to keep it." : "sk-ant-…";
  f.ollama_model.value = s.ai === "ollama" ? s.model || "" : ""; f.ollama_host.value = s.ollama_host || "";
  f.min_size.value = s.min_size || 50; f.large.value = s.large || 1024; f.old_days.value = s.old_days || 60;
  f.match_cosmic.checked = !!s.match_cosmic; f.match_cosmic.disabled = !s.cosmic_accent;
  $("#cosmicSwatch").style.background = s.cosmic_accent || "transparent";
  $("#cosmicNote").textContent = s.cosmic_accent ? "Uses the accent you picked in COSMIC Settings." : "PopUtil couldn't find a COSMIC accent color.";
  S.settings = { ...S.settings, ...s };
  $("#settings").showModal();
}
$("#settingsBtn").onclick = openSettings;
$("#settings").addEventListener("close", async () => {
  if ($("#settings").returnValue !== "save") return;
  const f = $("#settingsForm");
  const b = { ai: f.ai.value, ollama_host: f.ollama_host.value.trim() || "http://localhost:11434", min_size: +f.min_size.value || 50,
              large: +f.large.value || 1024, old_days: +f.old_days.value || 60, model: f.ai.value === "ollama" ? f.ollama_model.value.trim() || null : null,
              match_cosmic: f.match_cosmic.checked };
  if (f.anthropic_key.value.trim()) b.anthropic_key = f.anthropic_key.value.trim();
  try { await api("/api/settings", b); toast("Settings saved"); Object.assign(S.settings, b); applyTheme(); (loaders[S.view] || (() => {}))(); }
  catch (e) { toast(e.message, "err"); }
});

/* ───────────────────────── activity log & job polling ───────────────────────── */
let seq = 0, firstPoll = true;
const seen = new Set();
function openConsole(open) {
  const c = $("#console");
  c.classList.toggle("open", open ?? !c.classList.contains("open"));
  $("#consoleHead").setAttribute("aria-expanded", c.classList.contains("open"));
  document.body.classList.toggle("log-open", c.classList.contains("open"));
}
$("#consoleHead").addEventListener("click", (e) => { if (!e.target.closest("#logClear")) openConsole(); });
$("#consoleHead").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openConsole(); } });
$("#logClear").onclick = () => ($("#log").innerHTML = "");
$("#activityPill").onclick = () => openConsole(true);

const refreshAfter = { apps: loadApps, removed: loadApps, catalog: loadCatalog, tweaks: loadTweaks, clean: loadClean, organize: loadOrg,
                       tasks: () => loadSys().catch(() => {}) };
async function poll() {
  try {
    const j = await api("/api/log?after=" + seq);
    if (j.lines.length) {
      const log = $("#log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      const frag = document.createDocumentFragment();
      for (const l of j.lines) { const d = document.createElement("div"); d.className = l.k; d.textContent = l.s; frag.append(d); seq = l.n; if (l.k === "out") S.lastLine = l.s.trim(); }
      log.append(frag);
      while (log.childElementCount > 3000) log.firstChild.remove();
      if (atEnd) log.scrollTop = log.scrollHeight;
      const pl = $(`#${S.view} .progress-line`); if (pl && S.busy) pl.textContent = S.lastLine;
    }
    const cur = j.jobs.current, q = j.jobs.queued.length;
    $("#activityPill").classList.toggle("busy", !!cur);
    $("#activityText").textContent = cur ? cur.title : "Nothing running";
    $("#consoleCur").textContent = cur ? `${cur.title}${q ? ` (${q} more waiting)` : ""}` : "Nothing running";
    const busyTag = cur && ["apps", "clean", "organize"].includes(cur.tag) && /^(Scanning|Looking|Planning)/.test(cur.title) ? cur.tag : null;
    if (busyTag !== S.busy) { const was = S.busy; S.busy = busyTag; if (was && !busyTag) S.lastLine = ""; }
    for (const h of j.jobs.history) {
      if (seen.has(h.id)) continue;
      seen.add(h.id);
      if (firstPoll) continue;
      toast(h.status === "done" ? h.done_title : `${h.title} didn't finish. Activity has the details.`, h.status === "done" ? "ok" : "err");
      (refreshAfter[h.tag] || (() => {}))();
    }
    firstPoll = false;
    setTimeout(poll, cur || q ? 400 : 1500);
  } catch { setTimeout(poll, 3000); }
}

/* ───────────────────────── start ───────────────────────── */
if (!TOKEN) {
  document.body.innerHTML = `<div class="empty" style="grid-column:1/-1;margin:18vh auto;max-width:440px"><b>Open PopUtil from its launcher</b>This page needs the private link PopUtil opens for you.</div>`;
} else {
  api("/api/settings").then((s) => { S.settings = s; applyTheme(); }).catch(applyTheme);
  applyTheme();
  go("overview");
  poll();
}
