"use strict";
const TOKEN = document.querySelector('meta[name="token"]').content;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const gb = (b) => (b / 1e9).toFixed(b < 1e9 ? 2 : 1) + " GB";
const mb = (b) => (b / 1e6).toFixed(1) + " MB";
// Status dot used on every page: green online/idle, blue playing, red offline.
const statusDot = (u) => {
  const [cls, tip] = !u.online ? ["off", u.needs_password ? "Answers, but needs its UI password" : "Offline: not answering"] : u.status === "playing" ? ["play", "Online, playing"]
    : ["on", `Online${u.status ? ", " + u.status : ""}`];
  return `<span class="dot ${cls}" title="${tip}" aria-label="${tip}"></span>`;
};
const fmtTaken = (t) => t ? t.replace(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d).*/, "$1-$2-$3 $4:$5") : "never";

async function api(path, body) {
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json", "X-Token": TOKEN },
                        body: JSON.stringify(body) } : { headers: { "X-Token": TOKEN } };
  const r = await fetch(path, opts);
  // Each server run issues a new token, so a tab left open across a restart is refused.
  if (r.status === 403) throw new Error("This page is from before the app was restarted: reload the page (⌘R) and try again.");
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 5000);
}

async function act(path, body) {
  try {
    const job = await api(path, body);
    showTab("jobs"); await loadJobs(); openJob(job.id);
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------ tabs
function showTab(name) {
  const btn = document.querySelector(`nav button[data-tab="${name}"]`);
  if (btn && btn.closest(".nav-group")) btn.closest(".nav-group").open = true;
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("main section").forEach((s) => (s.hidden = s.id !== "tab-" + name));
  // name comes from the URL hash: only the page loaders below, never inherited properties
  const loaders = { controllers: loadControllers, build: loadBuild, flash: loadFlash, jobs: loadJobs, fleet: () => FLEET ? renderFleet() : loadFleet(false),
     dashboard: () => DASH ? renderDashboard() : loadDashboard(false),
     sequences: () => FLEET ? renderSequences() : loadFleet(false).then(renderSequences),
     show: startLive, settings: loadSettings };
  if (Object.prototype.hasOwnProperty.call(loaders, name)) loaders[name]();
  if (name !== "show") stopLive();
}
document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
// Sidebar groups remember whether they are open (a convenience; works without storage too).
document.querySelectorAll(".nav-group").forEach((g) => {
  try { const v = localStorage.getItem("nav:" + g.dataset.group); if (v !== null) g.open = v === "1"; } catch {}
  g.addEventListener("toggle", () => { try { localStorage.setItem("nav:" + g.dataset.group, g.open ? "1" : "0"); } catch {} });
});

// ------------------------------------------------------------ controllers
let CONTROLLERS = [];

async function loadControllers() {
  const body = $("#controllers");
  // keep selections across a refresh
  const kept = new Map([...document.querySelectorAll("#controllers tr[data-host]")].map((tr) =>
    [tr.dataset.host, { pick: tr.querySelector("[data-f=pick]")?.checked, media: tr.querySelector("[data-f=media]")?.checked }]));
  try {
    const { master, controllers } = await api("/api/controllers");
    CONTROLLERS = controllers;
    $("#player").textContent = `player ${master}`;
    body.innerHTML = controllers.map((c) => ctlRow(c, kept.get(c.host) || {})).join("");
    body.querySelectorAll("input").forEach((i) => (i.onchange = syncBackupButtons));
  } catch (e) {
    $("#player").textContent = "player unreachable";
    body.innerHTML = `<tr><td colspan="7" class="bad">${esc(e.message)}</td></tr>`;
  }
  syncBackupButtons();
}

function ctlRow(c, k) {
  return `
      <tr class="${c.online ? "" : "offline"}" data-host="${esc(c.host)}">
        <td class="pick">${c.online ? `<input type="checkbox" data-f="pick" ${k.pick ? "checked" : ""}>` : ""}</td>
        <td>${statusDot(c)}<span class="nowrap"><strong>${esc(c.host)}</strong>${rescanBtn(c.host, "controllers")}</span><span class="sub">${esc(c.mode || "")}${c.status ? " · " + esc(c.status) : c.online ? "" : c.needs_password ? "needs password" : "offline"}</span></td>
        <td>${esc(c.variant || "")}${c.cape ? `<span class="sub">cape ${esc(c.cape)}</span>` : ""}</td>
        <td>${c.online ? esc(c.version) : '<span class="bad">offline</span>'}</td>
        <td>${esc(c.ip)}${c.addresses.length > 1 ? `<span class="sub">${esc(c.addresses.filter((a) => a !== c.ip).join(", "))}</span>` : ""}</td>
        <td>${fmtTaken(c.last_snapshot)}${c.snapshots ? `<span class="sub">${c.snapshots} total</span>` : ""}</td>
        <td class="pick">${c.online ? `<input type="checkbox" data-f="media" title="Include media for ${esc(c.host)}" ${k.media ? "checked" : ""}>` : ""}</td>
      </tr>`;
}

// ↻ next to a controller: rescan just that one (a few seconds instead of the whole fleet).
const rescanBtn = (host, page) =>
  `<button class="rescan-one" data-rescan="${esc(host)}" data-page="${page}" title="Rescan ${esc(host)} only" aria-label="Rescan ${esc(host)} only">↻</button>`;
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-rescan]");
  if (!b || b.disabled) return;
  const host = b.dataset.rescan, page = b.dataset.page;
  b.disabled = true; b.classList.add("busy");
  try {
    const r = await api("/api/fleet/rescan", { host, page });
    if (page === "controllers") {
      const tr = document.querySelector(`#controllers tr[data-host="${CSS.escape(host)}"]`);
      const k = tr ? { pick: tr.querySelector("[data-f=pick]")?.checked, media: tr.querySelector("[data-f=media]")?.checked } : {};
      CONTROLLERS = CONTROLLERS.map((c) => (c.host === host ? r.controller : c));
      if (tr) { tr.outerHTML = ctlRow(r.controller, k); document.querySelectorAll(`#controllers tr[data-host="${CSS.escape(host)}"] input`).forEach((i) => (i.onchange = syncBackupButtons)); }
      syncBackupButtons();
    } else {
      FLEET = r.report; PREVIEW = false;
      if (DASH) DASH.report = FLEET;
      if (page === "dashboard") renderDashboard(); else renderFleet();
    }
    toast(`${host} rescanned`);
  } catch (err) {
    toast(err.message);
    b.disabled = false; b.classList.remove("busy");
  }
});

const ctlRows = () => [...document.querySelectorAll("#controllers tr[data-host]")].filter((tr) => tr.querySelector("[data-f=pick]"));
function syncBackupButtons() {
  const rows = ctlRows(), picked = rows.filter((tr) => tr.querySelector("[data-f=pick]").checked);
  const btn = $("#backup-selected");
  btn.disabled = !picked.length;
  btn.textContent = picked.length ? `Back up selected (${picked.length})` : "Back up selected";
  $("#backup-all").disabled = !rows.length;
  const all = $("#ctl-pick-all");
  all.checked = picked.length && picked.length === rows.length;
  all.indeterminate = picked.length > 0 && picked.length < rows.length;
  const media = rows.filter((tr) => tr.querySelector("[data-f=media]").checked).length;
  const mAll = $("#ctl-media-all");
  mAll.checked = media && media === rows.length;
  mAll.indeterminate = media > 0 && media < rows.length;
}
$("#ctl-pick-all").onchange = (e) => { ctlRows().forEach((tr) => (tr.querySelector("[data-f=pick]").checked = e.target.checked)); syncBackupButtons(); };
$("#ctl-media-all").onchange = (e) => { ctlRows().forEach((tr) => (tr.querySelector("[data-f=media]").checked = e.target.checked)); syncBackupButtons(); };

async function backUp(rows) {
  const units = rows.map((tr) => {
    const c = CONTROLLERS.find((x) => x.host === tr.dataset.host);
    return { host: c.host, ip: c.ip, via: c.via || null, media: tr.querySelector("[data-f=media]").checked };
  });
  const withMedia = units.filter((u) => u.media).map((u) => u.host);
  if (units.length > 1 && !confirm(`Back up ${units.length} controllers${withMedia.length ? `, with media for ${withMedia.join(", ")}` : " (configuration only)"}?\n\nThis only reads from the controllers.`)) return;
  await act("/api/backup", { units });
}
$("#backup-selected").onclick = () => backUp(ctlRows().filter((tr) => tr.querySelector("[data-f=pick]").checked));
$("#backup-all").onclick = () => backUp(ctlRows());
$("#refresh-controllers").onclick = loadControllers;

// ------------------------------------------------------------ build
let SNAPS = [], RELEASES = [], STOCK = [];
const family = (p) => /pocketbeagle\s*2|beaglebone 64|beagley/i.test(p || "") ? "BB64"
  : /beaglebone/i.test(p || "") ? "BBB" : /raspberry/i.test(p || "") ? "Pi64" : null;

async function loadBuild() {
  [SNAPS, RELEASES, { stock: STOCK }] = await Promise.all([api("/api/snapshots"), api("/api/releases"), api("/api/images")]);
  const sel = $("#build-snapshot");
  const prev = sel.value;
  sel.innerHTML = SNAPS.slice().reverse().map((s) =>
    `<option value="${esc(s.path)}">${esc(s.host)} · ${fmtTaken(s.taken)} · FPP ${esc(s.version)} · ${s.media ? "with media" : "config only"}</option>`).join("");
  if (prev && SNAPS.some((s) => s.path === prev)) sel.value = prev;
  $("#build-release").innerHTML = RELEASES.map((r) =>
    `<option value="${esc(r.tag)}">${esc(r.tag)}${r.prerelease ? " (pre-release)" : ""} · ${r.published.slice(0, 10)}</option>`).join("");
  const firstStable = RELEASES.find((r) => r.tag !== "nightly" && !r.prerelease);
  if (firstStable) $("#build-release").value = firstStable.tag;
  updateBuild();
}

function updateBuild() {
  const snap = SNAPS.find((s) => s.path === $("#build-snapshot").value);
  const rel = RELEASES.find((r) => r.tag === $("#build-release").value);
  const assetSel = $("#build-asset");
  if (rel) {
    const want = snap && family(`${snap.platform} ${snap.variant}`);
    const keep = assetSel.dataset.tag === rel.tag && assetSel.dataset.snap === snap?.path ? assetSel.value : null;
    assetSel.innerHTML = rel.assets.map((a) => `<option value="${esc(a.name)}">${esc(a.name)} (${mb(a.size)})</option>`).join("");
    assetSel.dataset.tag = rel.tag; assetSel.dataset.snap = snap?.path || "";
    const auto = rel.assets.find((a) => a.name.includes(`-${want}.img`));
    assetSel.value = keep || (auto && auto.name) || assetSel.value;
  }
  $("#build-snapshot-note").innerHTML = snap ? `${esc(snap.platform)} (${esc(snap.variant)}), ${snap.files} files, ${mb(snap.bytes)}` +
    (snap.media ? "" : ' · <span class="warn">no media in this snapshot; sequences will be missing</span>') +
    (snap.errors ? ` · <span class="bad">${snap.errors} errors in snapshot</span>` : "") : "No snapshots yet: take one on the Controllers tab.";
  const imgName = assetSel.value && assetSel.value.replace(/\.zip$/, "");
  const have = STOCK.find((i) => i.name === imgName);
  $("#download-image").hidden = !!have || !assetSel.value;
  $("#build-image-note").innerHTML = !assetSel.value ? "" : have
    ? `<span class="good">Stock image downloaded</span> (${gb(have.bytes)})`
    : "Stock image not downloaded yet.";
  const fam = snap && family(`${snap.platform} ${snap.variant}`);
  if (fam && assetSel.value && !assetSel.value.includes(`-${fam}.img`))
    $("#build-image-note").innerHTML += ` · <span class="warn">this image family does not match the snapshot's hardware (${fam})</span>`;
  $("#build").disabled = !(snap && have);
}
$("#build-snapshot").onchange = updateBuild;
$("#build-release").onchange = updateBuild;
$("#build-asset").onchange = updateBuild;
$("#download-image").onclick = () => act("/api/download", { tag: $("#build-release").value, asset: $("#build-asset").value });
$("#build").onclick = () => {
  const img = STOCK.find((i) => i.name === $("#build-asset").value.replace(/\.zip$/, ""));
  act("/api/build", { snapshot: $("#build-snapshot").value, image: img.path });
};

// ------------------------------------------------------------ flash
async function loadFlash() {
  const { built } = await api("/api/images");
  $("#flash-images").innerHTML = built.length ? built.map((i, n) => `
    <label><input type="radio" name="img" value="${esc(i.path)}" ${n ? "" : "checked"}>
      <span>${esc(i.name)} <span class="muted">· ${gb(i.bytes)} · built ${new Date(i.mtime * 1000).toLocaleString()}</span></span></label>`).join("")
    : '<p class="muted">No built images yet: use the Build card tab.</p>';
  await loadCards();
}

async function loadCards() {
  const box = $("#flash-cards");
  box.innerHTML = '<p class="muted">Looking for SD cards…</p>';
  try {
    const cards = await api("/api/cards");
    box.innerHTML = cards.length ? cards.map((c, n) => `
      <label><input type="radio" name="card" value="${esc(c.disk)}" ${n ? "" : "checked"}>
        <span><strong>${esc(c.disk)}</strong> <span class="muted">· ${esc(c.size)} · ${esc(c.name)}</span></span></label>`).join("")
      : '<p class="muted">No SD card found. Insert one and press Refresh.</p>';
  } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
  box.querySelectorAll("input").forEach((i) => (i.onchange = updateFlash));
  document.querySelectorAll('#flash-images input').forEach((i) => (i.onchange = updateFlash));
  updateFlash();
}

function updateFlash() {
  const card = document.querySelector('input[name="card"]:checked');
  const img = document.querySelector('input[name="img"]:checked');
  $("#flash-confirm").placeholder = card ? `type ${card.value} to confirm` : "no card selected";
  $("#flash").disabled = !(card && img && $("#flash-confirm").value.trim() === card.value);
}
$("#flash-confirm").oninput = updateFlash;
$("#refresh-cards").onclick = loadCards;
$("#flash").onclick = () => {
  const card = document.querySelector('input[name="card"]:checked').value;
  act("/api/flash", { image: document.querySelector('input[name="img"]:checked').value, disk: card,
                      confirm: $("#flash-confirm").value.trim() });
  $("#flash-confirm").value = ""; updateFlash();
};

// ------------------------------------------------------------ jobs
let openId = null, source = null;
const TITLES = {};

async function loadJobs() {
  const jobs = await api("/api/jobs");
  jobs.forEach((j) => (TITLES[j.id] = j.title));
  const running = jobs.filter((j) => j.status === "running").length;
  const badge = $("#jobs-badge");
  badge.hidden = !running; badge.textContent = running;
  $("#jobs-list").innerHTML = jobs.length ? jobs.map((j) => `
    <li data-id="${j.id}" class="${j.id === openId ? "selected" : ""}">
      <span class="pill ${j.status}">${j.status}</span> ${esc(j.title)}
      <span class="last">${esc(j.last)}</span></li>`).join("") : '<li class="muted">No jobs yet</li>';
  document.querySelectorAll("#jobs-list li[data-id]").forEach((li) => (li.onclick = () => openJob(li.dataset.id)));
}

function openJob(id) {
  if (source) source.close();
  openId = id;
  const log = $("#log"), prog = $("#log-progress");
  log.textContent = ""; prog.textContent = "";
  document.querySelectorAll("#jobs-list li").forEach((li) => li.classList.toggle("selected", li.dataset.id === id));
  $("#log-title").textContent = TITLES[id] || "Job";
  source = new EventSource(`/api/jobs/${id}/stream`);
  source.addEventListener("line", (e) => {
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    log.textContent += JSON.parse(e.data) + "\n";
    if (atEnd) log.scrollTop = log.scrollHeight;
  });
  source.addEventListener("progress", (e) => (prog.textContent = JSON.parse(e.data)));
  source.addEventListener("status", (e) => {
    const s = JSON.parse(e.data);
    prog.innerHTML = `<span class="${s === "done" ? "good" : "bad"}">${s === "done" ? "Finished" : "Failed"}</span>`;
    source.close(); source = null; loadJobs();
    if (TITLES[id] && TITLES[id].startsWith("Back up")) { loadControllers(); DASH = null; }  // backup ages changed
  });
}

// ------------------------------------------------------------ dashboard
let DASH = null;
// true when version a is newer than b, comparing numbers (10.10 > 10.3)
const vNewer = (a, b) => {
  const pa = String(a).match(/\d+/g) || [], pb = String(b).match(/\d+/g) || [];
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) { const d = (+pa[i] || 0) - (+pb[i] || 0); if (d) return d > 0; }
  return false;
};

function updateTag(u) {
  const up = u.update;
  if (!u.online) return "";
  if (!up) return '<span class="tag" title="Update check unavailable">?</span>';
  const tags = [];
  if (up.release_newer) tags.push(`<span class="tag warn" title="Newest FPP release">${esc(up.latest_release)} available</span>`);
  if (up.patch_commits > 0) tags.push(`<span class="tag info" title="Newer commits on ${esc(u.branch)} than this unit runs">${up.patch_commits} patch${up.patch_commits > 1 ? "es" : ""}</span>`);
  return tags.join(" ") || '<span class="tag ok">current</span>';
}

const takenMs = (t) => t ? Date.parse(t.replace(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)$/, "$1-$2-$3T$4:$5:$6")) : null;
function ago(ms) {
  if (!ms) return "never";
  const h = (Date.now() - ms) / 3.6e6;
  return h < 1 ? "under an hour ago" : h < 24 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} day${Math.round(h / 24) === 1 ? "" : "s"} ago`;
}

async function loadDashboard(refresh) {
  $("#dash-when").textContent = "Scanning every controller (about 20 seconds)…";
  $("#dash-refresh").disabled = true;
  try {
    DASH = await api("/api/dashboard" + (refresh ? "?refresh=1" : ""));
    FLEET = DASH.report; PREVIEW = false;
    renderDashboard();
  } catch (e) {
    $("#dash-when").innerHTML = `<span class="bad">${esc(e.message)}</span>`;
  } finally { $("#dash-refresh").disabled = false; }
}
$("#dash-refresh").onclick = () => loadDashboard(true);

function renderDashboard() {
  // One scan for the whole app: the latest report any page fetched (a policy
  // preview on the Fleet page is not the real state, so it is not shown here).
  if (!PREVIEW && FLEET) DASH.report = FLEET;
  const R = DASH.report, units = R.units, B = DASH.backups;
  const target = R.policy.fpp_version, maxAge = R.policy.backup_max_age_days || 7;
  const online = units.filter((u) => u.online);
  const expected = units.filter((u) => u.expected !== false);
  const offline = expected.filter((u) => !u.online);
  const unknown = online.filter((u) => u.expected === false);
  const driftRows = R.match.filter((r) => r.drift);
  const driftUnits = online.filter((u) => u.drift_count);
  const behind = online.filter((u) => !u.version_ok);
  const withUpdate = online.filter((u) => u.update && (u.update.release_newer || u.update.patch_commits > 0));
  const lastMs = (h) => takenMs(B[h]?.last);
  const never = expected.filter((u) => !B[u.host]);
  const stale = expected.filter((u) => B[u.host] && Date.now() - lastMs(u.host) > maxAge * 864e5);
  const clockBad = online.filter((u) => u.offset_s !== null && u.offset_s !== undefined && Math.abs(u.offset_s) > 2);
  const lowDisk = online.filter((u) => u.disk_total && u.disk_free / u.disk_total < 0.15);
  const covGaps = (R.coverage?.units || []).filter((u) => u.missing?.length);
  const seqMissing = covGaps.reduce((n, u) => n + u.missing.length, 0);
  $("#seq-badge").hidden = !seqMissing; $("#seq-badge").textContent = seqMissing;
  $("#dash-when").textContent = `Scanned ${new Date(R.scanned * 1000).toLocaleTimeString()}`;

  const tile = (go, cls, k, n, sub) => `<button class="tile ${cls}" data-go="${go}"><span class="k">${k}</span><span class="n">${n}</span><span class="s">${sub}</span></button>`;
  $("#dash-tiles").innerHTML = [
    tile("controllers", offline.length ? "bad" : unknown.length ? "warn" : "", "Controllers online",
         `${expected.filter((u) => u.online).length} / ${expected.length}`, `${offline.length} offline · ${unknown.length} unknown${R.inventory ? "" : " · no inventory"}`),
    tile("fleet", driftRows.length ? "warn" : "", "Policy drift", driftRows.length ? `${driftRows.length} settings` : "none",
         driftRows.length ? `on ${driftUnits.length} controller${driftUnits.length === 1 ? "" : "s"}` : "every controller matches"),
    tile("build", behind.length ? "warn" : "", `On FPP ${esc(target)}`, `${online.length - behind.length} / ${online.length}`,
         behind.length ? `${behind.length} behind: ${behind.map((u) => esc(u.host)).join(", ")}` : "all on the target"),
    tile("fleet", withUpdate.length ? "warn" : "", "Updates available", withUpdate.length || "none",
         R.releases?.latest ? `latest release ${esc(R.releases.latest)}` : esc(R.update_error ? "update check failed" : "")),
    tile("controllers", never.length ? "bad" : stale.length ? "warn" : "", "Backups", never.length + stale.length ? `${never.length + stale.length} due` : "current",
         `${never.length} never · ${stale.length} older than ${maxAge} days`),
    tile("sequences", covGaps.length ? "bad" : R.coverage?.error ? "warn" : "", "Sequences", covGaps.length ? `${covGaps.reduce((n, u) => n + u.missing.length, 0)} missing` : R.coverage?.error ? "?" : "complete",
         covGaps.length ? `on ${covGaps.map((u) => esc(u.host)).join(", ")}` : R.coverage?.needed ? `${R.coverage.needed} sequences on every controller` : esc(R.coverage?.error || "")),
    tile("fleet", clockBad.length || lowDisk.length ? "warn" : "", "Health", clockBad.length + lowDisk.length || "ok",
         `${clockBad.length} clock · ${lowDisk.length} low storage`),
  ].join("");
  document.querySelectorAll(".tile[data-go]").forEach((t) => (t.onclick = () => showTab(t.dataset.go)));

  // needs attention
  const items = [];
  const add = (cls, html, go, label, extra) => items.push({ cls, html, go, label, extra });
  offline.forEach((u) => add("bad", u.needs_password
      ? `<strong>${esc(u.host)}</strong> answers but needs its UI password (or a different one).`
      : `<strong>${esc(u.host)}</strong> is offline (expected at ${esc(u.ip)}).`, u.needs_password ? "settings" : "controllers", u.needs_password ? "Passwords" : "Controllers",
    `<button class="small" data-inv-remove="${esc(u.host)}" title="Remove from fleet-inventory.json">Not expected</button>`));
  unknown.forEach((u) => add("warn", `<strong>${esc(u.host)}</strong> (${esc(u.ip)}) answers but isn't in the inventory.`, null, null,
    `<button class="small" data-inv-add="${esc(u.host)}">Add to inventory</button>`));
  covGaps.forEach((u) => add("bad", `<strong>${esc(u.host)}</strong> is missing ${u.missing.length} sequence${u.missing.length > 1 ? "s" : ""} the schedule plays (${u.missing.slice(0, 3).map(esc).join(", ")}${u.missing.length > 3 ? "…" : ""}); it will be dark for ${u.missing.length > 1 ? "them" : "it"}.`, "sequences", "Sequences"));
  if (never.length) add("bad", `Never backed up: ${never.map((u) => `<strong>${esc(u.host)}</strong>`).join(", ")}.`, "controllers", "Back up");
  if (stale.length) add("warn", `Last backup older than ${maxAge} days: ${stale.map((u) => `${esc(u.host)} (${ago(lastMs(u.host))})`).join(", ")}.`, "controllers", "Back up");
  if (behind.length) add("warn", `${behind.map((u) => `<strong>${esc(u.host)}</strong>`).join(", ")} run${behind.length === 1 ? "s" : ""} ${[...new Set(behind.map((u) => esc(u.version)))].join("/")}; the target is ${esc(target)}.`, "build", "Build card");
  if (R.releases?.latest && target && vNewer(R.releases.latest, target))
    add("warn", `FPP ${esc(R.releases.latest)} has been released; the policy targets ${esc(target)}.`, "fleet", "Fleet");
  const patched = online.filter((u) => u.update?.patch_commits > 0);
  if (patched.length) add("warn", `Newer commits on their branch: ${patched.map((u) => `${esc(u.host)} (${u.update.patch_commits} on ${esc(u.branch)})`).join(", ")}.`, "fleet", "Fleet");
  // A gateway that differs from the last backup: typically one an upgrade dropped.
  // Not flagged when the new value is what the policy expects (that change was a fix).
  const gwOf = (g) => g ? esc(g) : "none";
  const gwCells = (R.match.find((r) => r.kind === "network") || {}).cells || {};
  units.filter((u) => u.online && u.net && u.net.gateway !== null && B[u.host] && B[u.host].gateway !== null && B[u.host].gateway !== undefined
    && u.net.gateway !== B[u.host].gateway && gwCells[u.host]?.status !== "ok").forEach((u) =>
    add("warn", `<strong>${esc(u.host)}</strong>'s gateway changed since its last backup: ${gwOf(B[u.host].gateway)} → ${gwOf(u.net.gateway)}.`, "fleet", "Fleet"));
  if (driftRows.length) add("warn", `${driftRows.length} policy setting${driftRows.length === 1 ? "" : "s"} differ: ${driftRows.map((r) => r.kind === "network" ? "Gateway" : esc(r.name)).join(", ")}.`, "fleet", "Fleet");
  clockBad.forEach((u) => add("warn", `<strong>${esc(u.host)}</strong> clock is ${u.offset_s > 0 ? "+" : ""}${u.offset_s} s off.`, "fleet", "Fleet"));
  lowDisk.forEach((u) => add("warn", `<strong>${esc(u.host)}</strong> has ${Math.round(100 * u.disk_free / u.disk_total)}% storage free.`, null, null));
  if (R.update_error) add("warn", `Update check failed: ${esc(R.update_error)}`, null, null);
  if (!R.inventory) add("warn", "No fleet-inventory.json yet, so offline controllers can't be detected.", null, null);
  $("#dash-attention").innerHTML = items.length ? items.map((it) => `<li class="${it.cls}"><span>${it.html}</span><span class="row">${it.extra || ""}${it.go ? `<button class="small go" data-go="${it.go}">${it.label} →</button>` : ""}</span></li>`).join("")
    : '<li class="ok"><span>Nothing needs attention.</span></li>';
  $("#dash-attention").querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => showTab(b.dataset.go)));
  $("#dash-attention").querySelectorAll("[data-inv-add],[data-inv-remove]").forEach((b) => (b.onclick = () => inventory(b.dataset.invAdd || b.dataset.invRemove, b.dataset.invAdd ? "add" : "remove")));
  const badge = $("#dash-badge");
  const n = items.filter((i) => i.cls === "bad" || i.cls === "warn").length;
  badge.hidden = !n; badge.textContent = n;

  // show and releases
  const sh = R.show;
  $("#dash-show").innerHTML = !sh ? '<span class="bad">Player not reachable.</span>' :
    (sh.status === "playing" ? `<div class="big good">Playing ${esc(sh.playlist || "")}</div><div class="muted">${esc(sh.sequence || "")}</div>`
                             : `<div class="big">${esc(sh.status || "idle")}</div>`) +
    (sh.next?.playlist ? `<div class="muted" style="margin-top:6px">Next: <strong>${esc(sh.next.playlist)}</strong><br>${esc(sh.next.start_time || "")}</div>` : "");
  $("#dash-release").innerHTML = R.releases?.latest
    ? `<div class="big">FPP ${esc(R.releases.latest)}</div><div class="muted">latest release, ${new Date(R.releases.published).toLocaleDateString()}</div><div class="muted" style="margin-top:6px">Policy target: <strong>${esc(target)}</strong></div>`
    : `<span class="muted">${esc(R.update_error || "No release information.")}</span>`;

  // controllers
  $("#dash-units").innerHTML = `<thead><tr><th>Controller</th><th>Status</th><th>FPP</th><th>Update</th><th>Last backup</th><th>Drift</th><th>Clock</th><th>Storage</th><th>Uptime</th></tr></thead><tbody>` +
    units.map((u) => {
      const b = B[u.host], ms = lastMs(u.host);
      const backupCls = !b ? "bad" : Date.now() - ms > maxAge * 864e5 ? "warn" : "";
      const status = !u.online ? '<span class="tag bad">offline</span>' : u.expected === false ? '<span class="tag warn">unknown</span>'
        : u.status === "playing" ? '<span class="tag info">playing</span>' : `<span class="tag ok">${esc(u.status || "online")}</span>`;
      return `<tr class="${u.online ? "" : "offline"}">
        <td>${statusDot(u)}<span class="nowrap"><strong>${esc(u.host)}</strong>${rescanBtn(u.host, "dashboard")}</span><span class="sub">${esc(u.role || "")} · ${esc(u.ip)}</span></td>
        <td>${status}</td>
        <td class="${u.online && !u.version_ok ? "bad" : ""}">${esc(u.version || "—")}</td>
        <td>${updateTag(u)}</td>
        <td class="${backupCls === "bad" ? "bad" : backupCls === "warn" ? "warn" : ""}">${ago(ms)}${b?.last_media ? `<span class="sub">media ${ago(takenMs(b.last_media))}</span>` : b ? '<span class="sub">no media yet</span>' : ""}</td>
        <td>${!u.online ? "" : u.drift_count ? `<a href="#fleet" class="warn">${u.drift_count}</a>` : '<span class="good">0</span>'}</td>
        <td class="${u.offset_s !== null && u.offset_s !== undefined && Math.abs(u.offset_s) > 2 ? "bad" : ""}">${u.online && u.offset_s !== null && u.offset_s !== undefined ? (u.offset_s > 0 ? "+" : "") + u.offset_s + " s" : ""}</td>
        <td>${u.disk_total ? Math.round(100 * u.disk_free / u.disk_total) + "% free" : ""}</td>
        <td>${esc(u.uptime || "")}</td></tr>`;
    }).join("") + "</tbody>";
}

async function inventory(host, op) {
  const msg = op === "add" ? `Add ${host} to fleet-inventory.json as an expected controller?`
                           : `Remove ${host} from fleet-inventory.json? It will no longer be reported as offline.`;
  if (!confirm(msg + "\n\nThis edits the inventory file only.")) return;
  try {
    DASH.report = FLEET = await api("/api/inventory", { host, op });
    renderDashboard();
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------ sequences
async function loadSequences(refresh) {
  $("#seq-when").textContent = "Scanning (about 20 seconds)…";
  try { FLEET = await api("/api/fleet" + (refresh ? "?refresh=1" : "")); PREVIEW = false; renderSequences(); }
  catch (e) { $("#seq-when").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
}
$("#seq-refresh").onclick = () => { XL = null; VERS = null; loadSequences(true); };

let XL = null, VERS = null;
async function loadVersions() {
  try { VERS = await api("/api/sequences/versions"); } catch (e) { VERS = { units: {}, error: e.message }; }
  renderSequences();
}
async function loadXlights() {
  try { XL = await api("/api/xlights"); } catch (e) { XL = { ok: false, error: e.message }; }
  renderSequences();
}

// Where a sequence stands in the xLights show folder, as a tag next to it.
function xlTag(name) {
  if (!XL?.ok) return "";
  const l = XL.sequences?.[name];
  if (l === undefined) return "";
  if (!l) return ' <span class="tag bad" title="No .xsq with this name in the xLights show folder">not in show folder</span>';
  if (!l.rendered) return ' <span class="tag warn" title="The .xsq has never been rendered: tick Render first">not rendered</span>';
  if (l.stale) return ' <span class="tag warn" title="The .xsq was saved after its last render: tick Render first, or render it in xLights">render is old</span>';
  return "";
}

function renderSequences() {
  const C = FLEET.coverage || {};
  $("#seq-when").textContent = C.checked ? `Checked ${new Date(C.checked * 1000).toLocaleTimeString()}` : "";
  if (C.error) { $("#seq-summary").innerHTML = `<span class="chip bad">${esc(C.error)}</span>`; $("#seq-units").innerHTML = ""; return; }
  if (XL === null) { XL = false; loadXlights(); }
  if (VERS === null) { VERS = false; loadVersions(); }
  renderVersionPicker(C.units || []);
  const V = (VERS && VERS.units) || {};
  const older = (h) => V[h]?.outdated || [];
  const gaps = (C.units || []).filter((u) => u.missing?.length);
  const missing = gaps.reduce((n, u) => n + u.missing.length, 0);
  $("#seq-summary").innerHTML = `<span class="chip">Playlists checked: <strong>${(C.playlists || []).map(esc).join(", ") || "none"}</strong></span>
    <span class="chip"><strong>${C.needed || 0}</strong> sequences</span>
    <span class="chip ${missing ? "bad" : "ok"}"><strong>${missing}</strong> missing on ${gaps.length} controller${gaps.length === 1 ? "" : "s"}</span>` +
    (C.excluded?.length ? `<span class="chip">Excluded: ${C.excluded.map(esc).join(", ")}</span>` : "");
  const canPush = !!XL?.ok;
  $("#seq-xl").innerHTML = !XL ? "xLights: checking…" : XL.ok
    ? `xLights ${esc(XL.version)} · show folder <code>${esc(XL.show)}</code>${XL.open ? ` · open: ${esc(XL.open)}` : ""}` +
      ` · <span class="warn" title="xLights' automation port takes commands from any device on your network and from web pages in your browser, without a password. Turn it on (Preferences > xFade port: A) only while pushing.">automation port is open: turn it off when you're done</span>`
    : `<span class="warn">xLights not available: ${esc(XL.error)}</span> <button class="small" id="seq-xl-retry">Retry</button>`;
  if ($("#seq-xl-retry")) $("#seq-xl-retry").onclick = loadXlights;
  const nOlder = (C.units || []).reduce((n, u) => n + older(u.host).length, 0);
  $("#seq-render-wrap").hidden = $("#seq-push").hidden = !missing && !nOlder;
  const pick = (u, m, label, cls) => `<div><label class="inline ${cls}"><input type="checkbox" class="seq-pick" data-host="${esc(u.host)}" data-seq="${esc(m)}" ${canPush ? "" : "disabled"}> ${esc(m)}</label>${label}${xlTag(m)}</div>`;
  const verCell = (u) => {
    const r = V[u.host];
    if (!r) return '<td class="muted">not checked</td>';
    const when = `<span class="sub">checked ${new Date(r.checked * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span>`;
    const tip = VERS.source ? `Compared with the ${esc(VERS.source)}` : "Nothing to compare with";
    if (r.outdated.length) return `<td class="bad" title="${tip}">${r.outdated.length} older${when}</td>`;
    if (r.errors.length) return `<td class="warn" title="${esc(r.errors.slice(0, 5).join("\n"))}">${r.errors.length} unreadable${when}</td>`;
    return `<td class="good" title="${tip}">${r.current} current${when}</td>`;
  };
  $("#seq-units").innerHTML = `<thead><tr><th>Controller</th><th>Sequences</th><th title="Whether each copy is from the same render as the show folder's .fseq (Check versions)">Render</th><th>Missing or older render</th></tr></thead><tbody>` +
    (C.units || []).map((u) => `<tr><td><strong>${esc(u.host)}</strong><span class="sub">${esc(u.role || "")}</span></td>
      <td class="${u.error ? "bad" : u.missing?.length ? "bad" : "good"}">${u.error ? "error" : `${u.have} / ${C.needed}`}</td>
      ${verCell(u)}
      <td>${u.error ? esc(u.error) : u.missing.length || older(u.host).length
        ? u.missing.map((m) => pick(u, m, "", "bad")).join("") +
          older(u.host).map((o) => pick(u, o.seq, ' <span class="tag warn" title="Its render ID differs from the show folder\'s .fseq: it is from an older (or different) render">older render</span>', "")).join("")
        : '<span class="muted">none</span>'}</td></tr>`).join("") + "</tbody>";
  const b = $("#seq-badge"); b.hidden = !missing; b.textContent = missing;
  const sync = () => ($("#seq-push").disabled = !canPush || !document.querySelector(".seq-pick:checked"));
  document.querySelectorAll(".seq-pick").forEach((c) => (c.onchange = sync));
  sync();
}

// Which controllers Check versions reads: "All" or a pick, kept across redraws.
const verPicked = new Set();
let verJob = false;
function renderVersionPicker(units) {
  const all = $("#ver-all").checked;
  $("#ver-hosts").innerHTML = units.filter((u) => !u.error).map((u) =>
    `<label class="inline"><input type="checkbox" class="ver-host" value="${esc(u.host)}" ${all || verPicked.has(u.host) ? "checked" : ""} ${all ? "disabled" : ""}> ${esc(u.host)}</label>`).join("");
  document.querySelectorAll(".ver-host").forEach((c) => (c.onchange = () => {
    c.checked ? verPicked.add(c.value) : verPicked.delete(c.value); syncVersionPicker();
  }));
  syncVersionPicker();
}
function syncVersionPicker() {
  const all = $("#ver-all").checked, n = verPicked.size;
  $("#ver-pick-label").textContent = all ? "All controllers" : n === 1 ? [...verPicked][0] : n ? `${n} controllers` : "Choose controllers";
  $("#ver-run").disabled = verJob || (!all && !n);
}
$("#ver-all").onchange = () => renderVersionPicker(FLEET?.coverage?.units || []);
document.addEventListener("click", (e) => { if (!e.target.closest("#ver-pick")) $("#ver-pick").open = false; });

$("#ver-run").onclick = async () => {
  const hosts = $("#ver-all").checked ? "all" : [...verPicked];
  $("#ver-pick").open = false;
  let job;
  try { job = await api("/api/sequences/versions", { hosts }); } catch (e) { return toast(e.message); }
  verJob = true; syncVersionPicker();
  const state = $("#ver-state");
  state.textContent = "Starting…";
  const es = new EventSource(`/api/jobs/${job.id}/stream`);
  es.addEventListener("progress", (e) => (state.textContent = JSON.parse(e.data).trim()));
  let lastLine = "";
  es.addEventListener("line", (e) => { const l = JSON.parse(e.data); if (!l.startsWith("$ ") && !l.startsWith("[")) state.textContent = lastLine = l; });
  es.addEventListener("status", async (e) => {
    es.close(); verJob = false;
    const s = JSON.parse(e.data);
    // exit 1 means older copies were found (shown in the table) or the check could not run (say why)
    state.innerHTML = s === "done" ? "Versions checked: every copy is current."
      : /^(OUTD|FAIL|OK)/.test(lastLine) ? `<span class="warn">Checked: see the Render column</span> · <a href="#jobs">log</a>`
      : `<span class="bad">${esc(lastLine || "Check failed")}</span> · <a href="#jobs">log</a>`;
    await loadVersions();
  });
};

$("#seq-push").onclick = () => {
  const items = [...document.querySelectorAll(".seq-pick:checked")].map((c) => ({ seq: c.dataset.seq, host: c.dataset.host }));
  if (!items.length) return;
  const render = $("#seq-render").checked;
  const stale = items.filter((i) => XL.sequences?.[i.seq] && (XL.sequences[i.seq].stale || !XL.sequences[i.seq].rendered));
  if (stale.length && !render) return toast(`${stale[0].seq} needs a render first: tick Render first`);
  if (render && XL.open) return toast(`xLights has ${XL.open} open; close it first`);
  const lines = items.map((i) => `  ${i.seq} → ${i.host}`).join("\n");
  if (!confirm(`Upload ${items.length} sequence file(s) through xLights? Older copies are replaced.\n\n${lines}\n\n` +
    (render ? "Each sequence is rendered first; controllers that already have it keep the old render until they get the new one.\n" : "") +
    "This writes to the controllers. Not done while the show is playing. xLights is busy until it finishes; don't quit it or this app meanwhile.")) return;
  act("/api/xlights/push", { items, render });
};

// ------------------------------------------------------------ live show
let liveTimer = null;
// Polling can be paused (it reads every controller every 3 s); the choice is remembered in this browser.
const liveWanted = () => $("#live-on").checked;
try { $("#live-on").checked = localStorage.getItem("live:on") !== "0"; } catch {}
function startLive() {
  stopLive();
  $("#live-once").hidden = liveWanted();
  pollLive();
  if (liveWanted()) liveTimer = setInterval(pollLive, 3000);
}
function stopLive() { if (liveTimer) clearInterval(liveTimer); liveTimer = null; }
$("#live-on").onchange = () => {
  try { localStorage.setItem("live:on", liveWanted() ? "1" : "0"); } catch {}
  if (liveWanted()) startLive();
  else { stopLive(); $("#live-once").hidden = false; $("#live-when").textContent = "Paused"; }
};
$("#live-once").onclick = pollLive;
const fmtSec = (n) => n === undefined || n === null ? "" : `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;

async function pollLive() {
  let L;
  try { L = await api("/api/show/live"); } catch (e) { $("#live-when").innerHTML = `<span class="bad">${esc(e.message)}</span>`; return; }
  $("#live-when").textContent = `${liveWanted() ? "Updated" : "Paused · read"} ${new Date(L.t * 1000).toLocaleTimeString()}`;
  const P = L.player || {}, playing = P.status === "playing";
  $("#live-dot").hidden = !playing;
  $("#live-player").innerHTML = !L.player ? '<span class="bad">Player not reachable.</span>' : playing
    ? `<div><span class="k muted small">PLAYING</span><div class="big good">${esc(P.playlist || "")}</div></div>
       <div><span class="muted small">Sequence</span><div class="big">${esc(P.sequence)}</div></div>
       <div><span class="muted small">Position</span><div class="big">${fmtSec(P.seconds)} <span class="muted">/ −${fmtSec(P.remaining)}</span></div></div>`
    : `<div><span class="muted small">PLAYER</span><div class="big">${esc(P.status || "idle")}</div></div><div class="muted">Remotes are shown below; sync is only checked while a show plays.</div>`;
  const rows = L.units.filter((u) => u.role === "remote");
  const verdict = (u) => {
    if (u.error) return ["bad", "No answer"];
    if (u.mode && u.mode !== "remote") return ["", `not a remote (${u.mode})`];
    if (!playing) return ["", u.status || ""];
    if (u.status !== "playing") return ["bad", "DARK: not playing"];
    if (u.sequence !== P.sequence) return ["bad", "Wrong sequence"];
    const d = u.seconds - P.seconds;
    if (Math.abs(d) > 1) return ["warn", `${d > 0 ? "Ahead" : "Behind"} ${Math.abs(d)} s`];
    if (u.sync_rate === 0) return ["warn", "No sync packets"];
    return ["ok", "In sync"];
  };
  $("#live-units").innerHTML = `<thead><tr><th>Remote</th><th>Verdict</th><th>Status</th><th>Sequence</th><th>Position</th><th>Δ vs player</th><th>Sync pkts/s</th><th>Last sync</th></tr></thead><tbody>` +
    rows.map((u) => {
      const [cls, text] = verdict(u);
      const d = playing && u.status === "playing" ? u.seconds - P.seconds : null;
      return `<tr><td><strong>${esc(u.host)}</strong><span class="sub">${esc(u.ip)}</span></td>
        <td><span class="tag ${cls}">${esc(text)}</span></td>
        <td>${esc(u.status || "")}</td>
        <td class="${playing && u.sequence && u.sequence !== P.sequence ? "bad" : ""}">${esc(u.sequence || "—")}</td>
        <td>${u.status === "playing" ? fmtSec(u.seconds) : ""}</td>
        <td class="${d !== null && Math.abs(d) > 1 ? "warn" : ""}">${d === null ? "" : (d > 0 ? "+" : "") + d + " s"}</td>
        <td class="${playing && u.sync_rate === 0 ? "warn" : ""}">${u.sync_rate ?? ""}${u.sync_errors ? ` <span class="bad">(${u.sync_errors} err)</span>` : ""}</td>
        <td class="muted small">${esc((u.last_sync || "").replace(/^\d{4}-\d\d-\d\d /, ""))}</td></tr>`;
    }).join("") + "</tbody>";
}

// ------------------------------------------------------------ settings
let SETTINGS = null;

async function loadSettings() {
  try { SETTINGS = await api("/api/settings"); } catch (e) { toast(e.message); return; }
  const st = SETTINGS.settings;
  $("#set-player").value = st.player || "";
  $("#set-monitor").checked = st.monitor;
  $("#set-interval").value = st.monitor_interval;
  $("#set-updates").checked = st.update_check;
  $("#set-backup-days").value = st.backup_max_age_days;
  $("#set-cli").hidden = !SETTINGS.from_cli;
  $("#settings-first").hidden = !!SETTINGS.active_player;
  $("#settings-badge").hidden = !!SETTINGS.active_player;
  $("#set-inv").innerHTML = !SETTINGS.inventory ? '<span class="warn">No inventory yet.</span>'
    : `${SETTINGS.inventory_count} controller${SETTINGS.inventory_count === 1 ? "" : "s"} in <code>${esc(SETTINGS.files.inventory)}</code>.`;
  $("#set-inv-build").disabled = !SETTINGS.active_player;
  $("#policy-editor").hidden = !SETTINGS.policy || !SETTINGS.active_player;
  if (SETTINGS.policy && SETTINGS.active_player) openPolicyEditor(true);
  $("#set-pol").innerHTML = SETTINGS.policy
    ? `In <code>${esc(SETTINGS.files.policy)}</code>. Change what should match, <strong>Preview</strong> the effect on the fleet, then <strong>Save</strong>. Apply the result to the controllers from the Fleet page.`
    : "Not created yet. On the first scan it is created from <code>templates/fleet-policy.json</code>, with every value taken from your player's own settings, so your fleet starts out matching its player.";
  $("#set-files").innerHTML = `Settings, inventory and policy are saved in <code>config/</code>: this computer only, not in Git.`;
  $("#set-test-result").textContent = SETTINGS.active_player ? `Active player: ${SETTINGS.active_player}` : "";
  renderPasswords();
  $("#set-inv-list").innerHTML = (SETTINGS.controllers || []).map((c) =>
    `<span class="chip" title="${esc(c.ip)}${c.added === "manually" ? " · added manually" : ""}">${esc(c.host)} <span class="muted small">${esc(c.ip)}</span>` +
    `<button class="x" data-inv-del="${esc(c.host)}" title="Remove ${esc(c.host)} from the inventory">×</button></span>`).join("");
  document.querySelectorAll("[data-inv-del]").forEach((b) => (b.onclick = async () => {
    if (!confirm(`Remove ${b.dataset.invDel} from the inventory?\n\nIt stops being expected; nothing is sent to it.`)) return;
    try { await api("/api/inventory", { host: b.dataset.invDel, op: "remove", view: "settings" }); DASH = null; FLEET = null; loadSettings(); }
    catch (e) { toast(e.message); }
  }));
}

function renderPasswords() {
  const P = SETTINGS.passwords || {};
  const hosts = (SETTINGS.controllers || []).map((c) => c.host);
  if (!P.keychain) {
    $("#set-pw-status").innerHTML = '<span class="warn">The macOS Keychain is not available here, so passwords cannot be stored.</span>';
    return;
  }
  const stored = [P.default ? "one for every controller" : "", P.hosts?.length ? `own passwords for ${P.hosts.map(esc).join(", ")}` : ""].filter(Boolean);
  $("#set-pw-status").innerHTML = `Stored: <strong>${stored.length ? stored.join("; ") : "none"}</strong>`;
  $("#set-pw-default-remove").disabled = !P.default;
  const sel = $("#set-pw-host"), prev = sel.value;
  sel.innerHTML = hosts.length ? hosts.map((h) => `<option value="${esc(h)}">${esc(h)}${P.hosts?.includes(h) ? " (stored)" : ""}</option>`).join("")
    : '<option value="">no controllers in the inventory</option>';
  if (hosts.includes(prev)) sel.value = prev;
  $("#set-pw-one-remove").disabled = !P.hosts?.includes(sel.value);
  sel.onchange = () => ($("#set-pw-one-remove").disabled = !P.hosts?.includes(sel.value));
}

async function savePassword(scope, op, input) {
  const body = { scope, op, host: scope === "host" ? $("#set-pw-host").value : undefined };
  if (op === "set") {
    body.password = input.value;
    if (!body.password) return toast("Enter the password first");
  } else if (!confirm(`Remove the stored password for ${scope === "default" ? "every controller" : body.host}?`)) return;
  try {
    SETTINGS = await api("/api/passwords", body);
    if (input) input.value = "";
    toast(op === "set" ? "Password stored in the Keychain" : "Password removed");
    renderPasswords();
  } catch (e) { toast(e.message); }
}
$("#set-pw-default-save").onclick = () => savePassword("default", "set", $("#set-pw-default"));
$("#set-pw-default-remove").onclick = () => savePassword("default", "remove");
$("#set-pw-one-save").onclick = () => savePassword("host", "set", $("#set-pw-one"));
$("#set-pw-one-remove").onclick = () => savePassword("host", "remove");

$("#set-add").onclick = async () => {
  const out = $("#set-add-result"), address = $("#set-add-addr").value.trim();
  if (!address) return toast("Enter the controller's address");
  out.textContent = "Checking…";
  try {
    SETTINGS = await api("/api/inventory/add", { address, password: $("#set-add-pw").value });
    const a = SETTINGS.added;
    out.innerHTML = `<span class="good">Added ${esc(a.host)}</span> (${esc(a.address)}), FPP ${esc(a.version)} on ${esc(a.platform)}, mode ${esc(a.mode)}.` +
      ($("#set-add-pw").value ? " Its password is stored in the Keychain." : "") + " It shows up in the next scan.";
    $("#set-add-addr").value = $("#set-add-pw").value = "";
    DASH = null; FLEET = null;
    loadSettings();
  } catch (e) { out.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
};

$("#set-test").onclick = async () => {
  const out = $("#set-test-result");
  out.textContent = "Testing…";
  try {
    const r = await api("/api/settings/test", { player: $("#set-player").value.trim(), password: $("#set-test-pw").value });
    out.innerHTML = `<span class="good">Found ${esc(r.host)}</span>, FPP ${esc(r.version)} on ${esc(r.platform)}, mode <strong>${esc(r.mode)}</strong>. ` +
      (r.controllers.length ? `It knows ${r.controllers.length} other controller${r.controllers.length === 1 ? "" : "s"}: ${r.controllers.map(esc).join(", ")}.` : "It doesn't list any other controllers (is MultiSync on?).") +
      (r.mode !== "player" ? ' <span class="warn">This unit is not in player mode; use the controller that runs the show.</span>' : "") +
      ($("#set-test-pw").value ? ' The password worked: save it under <strong>Controller passwords</strong> below so the app can use it.' : "");
  } catch (e) { out.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
};

$("#set-save").onclick = async () => {
  const body = { player: $("#set-player").value.trim(), monitor: $("#set-monitor").checked,
                 monitor_interval: +$("#set-interval").value, update_check: $("#set-updates").checked,
                 backup_max_age_days: +$("#set-backup-days").value };
  const changedPlayer = SETTINGS && body.player !== (SETTINGS.settings.player || "");
  try {
    SETTINGS = await api("/api/settings", body);
    toast("Settings saved.");
    if (changedPlayer) { DASH = null; FLEET = null; CONTROLLERS = []; loadControllers(); }
    loadSettings();
  } catch (e) { toast(e.message); }
};

$("#set-inv-build").onclick = async () => {
  if (!confirm(`Replace the controller inventory with every controller ${SETTINGS.active_player} can reach right now?`)) return;
  try {
    await api("/api/inventory/rebuild", {});
    toast("Inventory rebuilt.");
    DASH = null; loadSettings();
  } catch (e) { toast(e.message); }
};

// ------------------------------------------------------------ notifications
async function loadNotes() {
  try {
    const N = await api("/api/notifications");
    const c = $("#bell-count"); c.hidden = !N.unread; c.textContent = N.unread > 99 ? "99+" : N.unread;
    $("#bell-list").innerHTML = N.items.length ? N.items.map((n) => `<li class="${esc(n.level)}${n.read ? "" : " unread"}" data-id="${n.id}" data-link="${esc(n.link || "")}">
      ${esc(n.text)}<span class="when">${new Date(n.t * 1000).toLocaleString()}</span></li>`).join("")
      : '<li class="muted">No notifications.</li>';
    $("#bell-list").querySelectorAll("li[data-id]").forEach((li) => (li.onclick = async () => {
      await api("/api/notifications/read", { ids: [li.dataset.id] });
      if (li.dataset.link) { $("#bell-panel").hidden = true; showTab(li.dataset.link); }
      loadNotes();
    }));
  } catch { /* server restarting */ }
}
$("#bell").onclick = (e) => { e.stopPropagation(); $("#bell-panel").hidden = !$("#bell-panel").hidden; if (!$("#bell-panel").hidden) loadNotes(); };
$("#bell-panel").onclick = (e) => e.stopPropagation();
document.addEventListener("click", () => ($("#bell-panel").hidden = true));
$("#bell-read").onclick = async () => { await api("/api/notifications/read", {}); loadNotes(); };
$("#bell-clear").onclick = async () => { if (confirm("Clear all notifications?")) { await api("/api/notifications/clear", {}); loadNotes(); } };
loadNotes();
setInterval(loadNotes, 15000);

// ------------------------------------------------------------ fleet
let FLEET = null;
let PREVIEW = false;      // FLEET is a preview of an unsaved policy
let POLICY_OPEN = false;
const pendingRestart = { hosts: new Set(), names: new Set() };
const shortHost = (h) => h.replace(/^FPP-/, "");
const shown = (v) => v === null || v === undefined ? "—" : v === "" ? "(blank)" : v;

async function loadFleet(refresh) {
  $("#fleet-when").textContent = "Scanning every controller (about 20 seconds)…";
  $("#fleet-refresh").disabled = true;
  try {
    FLEET = await api("/api/fleet" + (refresh ? "?refresh=1" : ""));
    PREVIEW = false;  // a fetched report is always against the saved policy
    renderFleet();
    if (POLICY_OPEN) await openPolicyEditor(true);
  } catch (e) {
    $("#fleet-when").innerHTML = `<span class="bad">${esc(e.message)}</span>`;
  } finally { $("#fleet-refresh").disabled = false; }
}
$("#fleet-refresh").onclick = () => loadFleet(true);

function renderFleet() {
  const F = FLEET, units = F.units, on = units.filter((u) => u.online);
  const master = on.find((u) => u.is_master);
  const hosts = on.map((u) => u.host);
  const driftRows = F.match.filter((r) => r.drift);
  const oldVer = on.filter((u) => !u.version_ok);
  const disagree = F.compare.concat(F.other).filter((r) => r.remotes_differ);
  const offline = units.filter((u) => !u.online);
  const clockBad = on.filter((u) => u.offset_s !== null && Math.abs(u.offset_s) > 2);
  $("#fleet-when").innerHTML = `Scanned ${new Date(F.scanned * 1000).toLocaleTimeString()}. Compares every controller with <code>fleet-policy.json</code>; scanning only reads.`;
  const chip = (n, label, bad) => `<span class="chip ${bad ? "bad" : "ok"}"><strong>${n}</strong>${label}</span>`;
  $("#fleet-summary").innerHTML = [
    chip(on.length, `online${offline.length ? `, ${offline.length} offline` : ""}`, offline.length),
    chip(driftRows.length, "policy settings drifting", driftRows.length),
    chip(oldVer.length, `not on FPP ${esc(F.policy.fpp_version)}`, oldVer.length),
    chip(clockBad.length, "clocks off by more than 2 s", clockBad.length),
    chip(disagree.length, "settings remotes disagree on", disagree.length),
  ].join("");
  const badge = $("#fleet-badge");
  badge.hidden = !(driftRows.length + oldVer.length + clockBad.length);
  badge.textContent = driftRows.length + oldVer.length + clockBad.length;

  // controllers
  const pct = (u) => u.disk_total ? Math.round(100 * u.disk_free / u.disk_total) + "% free" : "";
  $("#fleet-units").innerHTML = `<thead><tr><th>Controller</th><th>FPP</th><th>Update</th><th>Hardware</th><th>Address</th><th>Clock</th><th>Uptime</th><th>Storage</th></tr></thead><tbody>` +
    units.map((u) => !u.online ? `<tr class="offline"><td>${statusDot(u)}<span class="nowrap"><strong>${esc(u.host)}</strong>${rescanBtn(u.host, "fleet")}</span></td><td colspan="7">${u.needs_password ? '<span class="warn">needs its UI password</span>: add it in Settings' : "offline"} · expected at ${esc(u.ip)}</td></tr>` : `
      <tr><td>${statusDot(u)}<span class="nowrap"><strong>${esc(u.host)}</strong>${rescanBtn(u.host, "fleet")}</span>${u.expected === false ? ' <span class="tag warn">unknown</span>' : ""}<span class="sub">${esc(u.role)}${u.status ? " · " + esc(u.status) : ""}</span></td>
        <td class="${u.version_ok ? "" : "bad"}">${esc(u.version)}<span class="sub">${esc(u.os_version || "")}</span></td>
        <td>${updateTag(u)}</td>
        <td>${esc(u.variant || u.platform || "")}${u.cape ? `<span class="sub">cape ${esc(u.cape)}</span>` : ""}</td>
        <td>${esc(u.ip)}${u.via ? `<span class="sub">via player</span>` : ""}</td>
        <td class="${u.offset_s !== null && Math.abs(u.offset_s) > 2 ? "bad" : ""}">${u.offset_s === null ? "?" : (u.offset_s > 0 ? "+" : "") + u.offset_s + " s"}<span class="sub">${esc((u.clock || "").replace(/^\w+ /, "").replace(/ \d{4}$/, ""))}</span></td>
        <td>${esc(u.uptime || "")}</td><td>${pct(u)}</td></tr>`).join("") + "</tbody>";

  const head = (first) => `<thead><tr>${first}${hosts.map((h) => `<th class="unit${h === master?.host ? " player" : ""}" title="${esc(h)}${h === master?.host ? " (player)" : ""}"><span class="vlabel">${esc(shortHost(h))}${h === master?.host ? " ★" : ""}</span></th>`).join("")}<th></th></tr></thead>`;
  const isPlayer = (h) => h === master?.host;
  const val = (v) => `<span class="v" title="${esc(shown(v))}">${esc(shown(v))}</span>`;
  $("#fleet-preview-banner").hidden = !PREVIEW;

  // policy settings
  const verRow = `<tr><td class="pick"></td><td><strong>FPP version</strong><span class="why">Upgrade by building a card</span></td><td>${esc(F.policy.fpp_version)}</td>` +
    on.map((u) => `<td class="cell ${u.version_ok ? "ok" : "odd"}${isPlayer(u.host) ? " player" : ""}" title="${esc(u.version)}">${u.version_ok ? "✓" : esc(u.version)}</td>`).join("") + "<td></td></tr>";
  $("#fleet-match").innerHTML = head(`<th class="pick"><input type="checkbox" id="fleet-pick-all" title="Select every drifting setting"></th><th>Setting</th><th>Expected</th>`) + "<tbody>" + F.match.map((r) => `
    <tr><td class="pick">${r.drift ? `<input type="checkbox" data-pick="${esc(r.name)}">` : ""}</td><td><strong>${r.kind === "network" ? "Gateway" : esc(r.name)}</strong><span class="why">${esc(r.why || r.description)}${r.kind === "network" ? " · applies at the next reboot" : r.needs ? ` · needs player ${r.needs}` : ""}</span></td>
      <td>${r.kind === "network" ? r.rules.map((x) => `<div class="nowrap">${esc(x.subnet)}.x → ${esc(x.gateway)}</div>`).join("") +
          (r.inferred && !PREVIEW ? '<button class="small" id="gw-keep" title="Save these rules in fleet-policy.json so they stop following whatever the fleet uses">Keep these rules</button>' : "")
        : esc(r.expected === "@master" ? "the player" : shown(r.expected))}${r.player !== null && r.player !== undefined
        ? `<span class="sub">player: ${r.player === "*" ? "not checked" : esc(shown(r.player))}</span>` : ""}</td>` +
    hosts.map((h) => {
      const c = r.cells[h] || { status: "n/a" };
      const cls = (c.status === "n/a" ? "na" : c.status) + (isPlayer(h) ? " player" : "");
      const text = c.status === "ok" ? "✓" : c.status === "n/a" ? "n/a" : c.status === "exempt" ? "—" : val(c.value);
      return `<td class="cell ${cls}" title="${esc(h)}: ${esc(shown(c.value))}${c.expected !== undefined && c.expected !== null ? " (expected " + esc(shown(c.expected)) + ")" : ""}"
        ${c.status === "drift" && r.kind !== "network" ? `data-expect="${esc(r.name)}" data-value="${esc(c.value)}"` : ""}>${text}</td>`;
    }).join("") +
    `<td>${r.drift ? `<button class="small primary" data-apply="${esc(r.name)}" ${PREVIEW ? "disabled title=\"Save the policy first\"" : ""}>Apply to ${r.drift}</button>` : ""}</td></tr>`).join("") + verRow + "</tbody>";

  document.querySelectorAll("[data-apply]").forEach((b) => (b.onclick = () => runFleetUpdate([b.dataset.apply])));
  if ($("#gw-keep")) $("#gw-keep").onclick = async () => {
    if (!confirm("Save these gateway rules in fleet-policy.json?\n\nThis edits the policy only; nothing is sent to the controllers.")) return;
    try { FLEET = await api("/api/fleet/gateway-rules", {}); PREVIEW = false; renderFleet(); toast("Gateway rules saved in the policy"); } catch (e) { toast(e.message); }
  };
  const picks = () => [...document.querySelectorAll("input[data-pick]")];
  const syncPicks = () => {
    const n = picks().filter((i) => i.checked).length;
    $("#fleet-update").disabled = !n || fleetJobRunning || PREVIEW;
    $("#fleet-update").textContent = n ? `Update checked (${n})` : "Update checked";
    const all = $("#fleet-pick-all");
    all.checked = n && n === picks().length; all.indeterminate = n > 0 && n < picks().length;
    all.disabled = !picks().length;
  };
  picks().forEach((i) => { if (precheck.has(i.dataset.pick)) i.checked = true; i.onchange = syncPicks; });
  precheck.clear();
  $("#fleet-pick-all").onchange = (e) => { picks().forEach((i) => (i.checked = e.target.checked)); syncPicks(); };
  $("#fleet-update").onclick = () => runFleetUpdate(picks().filter((i) => i.checked).map((i) => i.dataset.pick));
  syncPicks();
  document.querySelectorAll("td[data-expect]").forEach((td) => (td.onclick = () => makeExpectedMany([{ setting: td.dataset.expect, value: td.dataset.value }])));

  // remotes that disagree
  const valueRow = (r, withAction) => {
    const remoteVals = [...new Set(hosts.filter((h) => h !== master?.host).map((h) => r.cells[h]).filter((v) => v !== null && v !== undefined))];
    const counts = {};
    hosts.filter((h) => h !== master?.host).forEach((h) => (counts[r.cells[h]] = (counts[r.cells[h]] || 0) + 1));
    const common = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
    return `<tr>${withAction ? `<td class="pick"><input type="checkbox" data-expect-pick="${esc(r.name)}"></td>` : ""}<td><strong>${esc(r.name)}</strong><span class="why">${esc(r.description || "")}</span></td>` +
      hosts.map((h) => {
        const v = r.cells[h];
        const odd = withAction && h !== master?.host && v !== common;
        return `<td class="cell${isPlayer(h) ? " player" + (withAction ? " muted" : "") : odd ? " odd" : ""}" title="${esc(h)}${isPlayer(h) && withAction ? " (player, not compared)" : ""}">${val(v)}</td>`;
      }).join("") +
      (withAction ? `<td><select data-for="${esc(r.name)}">${remoteVals.map((v) => `<option value="${esc(v)}" ${v === common ? "selected" : ""}>${esc(shown(v))}</option>`).join("")}</select>
        <button class="small" data-promote="${esc(r.name)}">Make expected</button></td>` : "<td></td>") + "</tr>";
  };
  $("#fleet-compare").innerHTML = disagree.length
    ? head(`<th class="pick"><input type="checkbox" id="fleet-expect-all" title="Select every setting"></th><th>Setting</th>`) + "<tbody>" + disagree.map((r) => valueRow(r, true)).join("") + "</tbody>"
    : '<tbody><tr><td class="muted">All remotes agree on every compared setting.</td></tr></tbody>';
  const chosenValue = (name) => document.querySelector(`select[data-for="${name}"]`).value;
  document.querySelectorAll("[data-promote]").forEach((b) => (b.onclick = () =>
    makeExpectedMany([{ setting: b.dataset.promote, value: chosenValue(b.dataset.promote) }])));
  const ePicks = () => [...document.querySelectorAll("input[data-expect-pick]")];
  const syncEPicks = () => {
    const n = ePicks().filter((i) => i.checked).length;
    const btn = $("#fleet-expect-checked");
    btn.disabled = !n; btn.textContent = n ? `Make checked expected (${n})` : "Make checked expected";
    const all = $("#fleet-expect-all");
    if (all) { all.checked = n && n === ePicks().length; all.indeterminate = n > 0 && n < ePicks().length; }
  };
  ePicks().forEach((i) => (i.onchange = syncEPicks));
  if ($("#fleet-expect-all")) $("#fleet-expect-all").onchange = (e) => { ePicks().forEach((i) => (i.checked = e.target.checked)); syncEPicks(); };
  $("#fleet-expect-checked").onclick = () => makeExpectedMany(ePicks().filter((i) => i.checked)
    .map((i) => ({ setting: i.dataset.expectPick, value: chosenValue(i.dataset.expectPick) })));
  syncEPicks();
  $("#fleet-perunit").innerHTML = head("<th>Setting</th>") + "<tbody>" + F.per_unit.map((r) => valueRow(r, false)).join("") + "</tbody>";
  renderRestartNote();
}

let fleetJobRunning = false;

async function runFleetUpdate(names) {
  const rows = names.map((n) => FLEET.match.find((x) => x.name === n)).filter((r) => r && r.drift);
  if (!rows.length) return;
  const restart = $("#fleet-restart-after").checked;
  const lines = rows.map((r) => `${r.name}:\n` + Object.entries(r.cells).filter(([, c]) => c.status === "drift")
    .map(([h, c]) => `    ${h}: ${shown(c.value)} → ${shown(c.expected)}`).join("\n")).join("\n");
  const needs = rows.filter((r) => r.needs && r.kind !== "network").map((r) => r.name);
  const gwNote = rows.some((r) => r.kind === "network") ? "\nGateway: saved now; each controller uses it from its next reboot (nothing is restarted, so a running show is not disturbed)." : "";
  const restartLine = needs.length ? (restart ? `\nAfterwards, restart the players on the affected units (${needs.join(", ")} need it); units that are playing are skipped.`
                                              : `\n${needs.join(", ")} will need a player restart to take effect.`) : "";
  if (!confirm(`Update ${rows.length} setting(s)? This writes to the controllers.\n\n${lines}\n${restartLine}${gwNote}\n\nEach setting runs in turn; the fleet is rescanned at the end.`)) return;
  if (!restart && needs.length) {
    rows.filter((r) => r.needs && r.kind !== "network").forEach((r) => Object.entries(r.cells).filter(([, c]) => c.status === "drift").forEach(([h]) => pendingRestart.hosts.add(h)));
    needs.forEach((n) => pendingRestart.names.add(n));
  }
  let job;
  try { job = await api("/api/fleet/apply", { settings: rows.map((r) => r.name), restart }); }
  catch (e) { toast(e.message); return; }
  watchFleetJob(job);
}

function watchFleetJob(job) {
  fleetJobRunning = true;
  $("#fleet-update").disabled = true;
  const box = $("#fleet-progress"), log = $("#fleet-progress-log"), state = $("#fleet-progress-state");
  box.hidden = false; log.textContent = "";
  $("#fleet-progress-title").textContent = job.title;
  state.className = "pill running"; state.textContent = "running";
  const es = new EventSource(`/api/jobs/${job.id}/stream`);
  es.addEventListener("line", (e) => {
    const line = JSON.parse(e.data);
    if (line.startsWith("$ ")) return;  // the command itself; the tool's own output says what happened
    log.textContent += line + "\n"; log.scrollTop = log.scrollHeight;
  });
  es.addEventListener("status", async (e) => {
    const s = JSON.parse(e.data);
    es.close();
    fleetJobRunning = false;
    state.className = `pill ${s}`; state.textContent = s === "done" ? "done" : "finished with errors";
    await loadFleet(false);  // the job rescanned before reporting, so this is fresh
    loadJobs();
  });
}

const precheck = new Set();

async function makeExpectedMany(items) {
  if (!items.length) return;
  const list = items.map((i) => `  ${i.setting} = ${shown(i.value)}`).join("\n");
  if (!confirm(`Make these the expected values for the whole fleet?\n\n${list}\n\nThis only edits fleet-policy.json; nothing is sent to the controllers. After the rescan, the units that differ are checked under "Settings that should match", ready for Update checked.`)) return;
  try {
    FLEET = await api("/api/fleet/expect", { items });
  } catch (e) { toast(e.message); return; }
  items.forEach((i) => precheck.add(i.setting));
  PREVIEW = false;
  renderFleet();
  if (POLICY_OPEN) await openPolicyEditor();
  const ticked = [...document.querySelectorAll("input[data-pick]")].filter((i) => i.checked).length;
  toast(ticked ? `${ticked} setting(s) now differ from the policy and are checked. Review, then press Update checked.`
               : "Policy updated. Every unit already matches.");
  if (ticked) $("#fleet-match").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderRestartNote() {
  const box = $("#fleet-restart-note");
  if (!pendingRestart.hosts.size) { box.hidden = true; return; }
  const hosts = [...pendingRestart.hosts];
  box.hidden = false;
  box.innerHTML = `<strong>${esc([...pendingRestart.names].join(", "))}</strong> take effect after a player restart on ${esc(hosts.join(", "))}.
    <button class="small" id="fleet-do-restart">Restart those players</button> <span class="muted small">Units that are playing are skipped.</span>`;
  $("#fleet-do-restart").onclick = async () => {
    if (!confirm(`Restart the FPP player on ${hosts.join(", ")}?\n\nEach is down for a few seconds. Units that are playing are skipped.`)) return;
    pendingRestart.hosts.clear(); pendingRestart.names.clear();
    renderRestartNote();
    try { watchFleetJob(await api("/api/fleet/restart", { hosts })); } catch (e) { toast(e.message); }
  };
}

// ------------------------------------------------------------ policy editor
let SAVED_POLICY = null, DRAFT = null, CATALOG = {};
const clone = (o) => JSON.parse(JSON.stringify(o));
const dirty = () => DRAFT && JSON.stringify(DRAFT) !== JSON.stringify(SAVED_POLICY);
const byCount = (name) => Object.entries(CATALOG[name]?.values || {}).sort((a, b) => b[1] - a[1]).map(([v]) => v);

// The editor lives on the Settings page. It needs a scan for its suggestions
// (the values each setting has across the fleet), so it starts one if needed.
async function openPolicyEditor(keepDraft) {
  let d = await api("/api/fleet/policy");
  if (!Object.keys(d.catalog || {}).length && !FLEET) {
    policyStatus("Scanning the fleet for suggestions (about 30 seconds)…");
    try { FLEET = await api("/api/fleet"); d = await api("/api/fleet/policy"); } catch {}
  }
  CATALOG = d.catalog || {};
  const hadChanges = keepDraft && dirty();
  SAVED_POLICY = d.policy;
  if (!hadChanges) DRAFT = clone(d.policy);
  POLICY_OPEN = true;
  renderPolicyEditor();
}

function showPreviewResult(rep) {
  const box = $("#pol-preview-result");
  const rows = rep.match.filter((r) => r.drift);
  const ver = rep.units.filter((u) => u.online && !u.version_ok);
  box.hidden = false;
  box.innerHTML = `<strong>Preview:</strong> ${rows.length ? `${rows.length} setting${rows.length === 1 ? "" : "s"} would drift` : "no setting would drift"}` +
    (ver.length ? `, and ${ver.length} controller${ver.length === 1 ? " is" : "s are"} not on FPP ${esc(rep.policy.fpp_version)}` : "") + "." +
    (rows.length ? "<ul>" + rows.map((r) => `<li><strong>${esc(r.name)}</strong> on ${Object.entries(r.cells).filter(([, c]) => c.status === "drift").map(([h]) => esc(h)).join(", ")}</li>`).join("") + "</ul>" : "") +
    `<button class="small" id="pol-see-fleet">See it on the Fleet page →</button>`;
  $("#pol-see-fleet").onclick = () => showTab("fleet");
}

function policyStatus(text) {
  $("#pol-status").innerHTML = text || (dirty() ? '<span class="warn">Unsaved changes.</span> Preview to see the effect on the fleet.' : "Matches the saved policy.");
}

function renderPolicyEditor() {
  const ver = $("#pol-version");
  ver.value = DRAFT.fpp_version || "";
  ver.oninput = () => { DRAFT.fpp_version = ver.value.trim(); policyStatus(); };
  const options = (name) => byCount(name).map((v) => `<option value="${esc(v)}">${esc(shown(v))} (${CATALOG[name].values[v]} unit${CATALOG[name].values[v] > 1 ? "s" : ""})</option>`).join("");
  $("#pol-match").innerHTML = `<thead><tr><th>Setting</th><th>Expected</th><th>Player (${esc(FLEET?.units.find((u) => u.is_master)?.host || "master")})</th><th>Also accept</th><th>Note</th><th></th></tr></thead><tbody>` +
    Object.entries(DRAFT.match).map(([name, r]) => {
      const mode = r.player === undefined ? "same" : r.player === "*" ? "skip" : "value";
      return `<tr data-name="${esc(name)}">
        <td class="name"><strong>${esc(name)}</strong><span class="why">${esc(CATALOG[name]?.description || "")}</span></td>
        <td class="w-val"><input data-f="value" value="${esc(r.value)}" list="vals-${esc(name)}" title="${name === "ntpServer" ? "@master = the player's address on each unit's subnet" : ""}"><datalist id="vals-${esc(name)}">${options(name)}</datalist></td>
        <td class="w-player"><div class="row">
          <select data-f="mode">
            <option value="same" ${mode === "same" ? "selected" : ""}>Same as remotes</option>
            <option value="value" ${mode === "value" ? "selected" : ""}>Its own value</option>
            <option value="skip" ${mode === "skip" ? "selected" : ""}>Not checked</option>
          </select>
          <input data-f="player" value="${esc(mode === "value" ? r.player : "")}" list="vals-${esc(name)}" ${mode === "value" ? "" : "hidden"} placeholder="(blank)">
        </div></td>
        <td class="w-val"><input data-f="equivalent" value="${esc((r.equivalent || []).join(", "))}" placeholder="aliases, comma separated"></td>
        <td class="w-note"><input data-f="why" value="${esc(r.why || "")}"></td>
        <td><button class="small" data-f="remove" title="Remove from the policy">✕</button></td></tr>`;
    }).join("") + "</tbody>";

  const markRows = () => document.querySelectorAll("#pol-match tr[data-name]").forEach((tr) =>
    tr.classList.toggle("changed", JSON.stringify(DRAFT.match[tr.dataset.name]) !== JSON.stringify(SAVED_POLICY.match[tr.dataset.name])));
  document.querySelectorAll("#pol-match tr[data-name]").forEach((tr) => {
    const name = tr.dataset.name, r = DRAFT.match[name];
    const f = (k) => tr.querySelector(`[data-f="${k}"]`);
    const update = () => {
      r.value = f("value").value;
      const mode = f("mode").value;
      f("player").hidden = mode !== "value";
      if (mode === "same") delete r.player; else r.player = mode === "skip" ? "*" : f("player").value;
      const eq = f("equivalent").value.split(",").map((x) => x.trim()).filter(Boolean);
      if (eq.length) r.equivalent = eq; else delete r.equivalent;
      if (f("why").value) r.why = f("why").value; else delete r.why;
      markRows(); policyStatus();
    };
    ["value", "player", "equivalent", "why"].forEach((k) => (f(k).oninput = update));
    f("mode").onchange = update;
    f("remove").onclick = () => { delete DRAFT.match[name]; renderPolicyEditor(); };
  });
  markRows();

  const used = () => new Set([...Object.keys(DRAFT.match), ...DRAFT.compare, ...DRAFT.per_unit]);
  const addOptions = (exclude) => `<option value="">Choose a setting…</option>` + Object.keys(CATALOG).filter((n) => !exclude.has(n))
    .map((n) => `<option value="${esc(n)}">${esc(n)}${CATALOG[n].description ? " — " + esc(CATALOG[n].description) : ""}</option>`).join("");
  $("#pol-add-name").innerHTML = addOptions(new Set(Object.keys(DRAFT.match)));
  $("#pol-add").onclick = () => {
    const name = $("#pol-add-name").value;
    if (!name) return;
    DRAFT.match[name] = { value: byCount(name)[0] ?? "" };
    DRAFT.compare = DRAFT.compare.filter((n) => n !== name);
    DRAFT.per_unit = DRAFT.per_unit.filter((n) => n !== name);
    renderPolicyEditor();
  };
  const chipList = (key, box, sel, btn) => {
    $(box).innerHTML = DRAFT[key].map((n) => `<span class="chip" title="${esc(CATALOG[n]?.description || "")}">${esc(n)}<button class="x" data-rm="${esc(n)}" title="Remove">✕</button></span>`).join("") || '<span class="muted small">none</span>';
    $(box).querySelectorAll("[data-rm]").forEach((b) => (b.onclick = () => { DRAFT[key] = DRAFT[key].filter((n) => n !== b.dataset.rm); renderPolicyEditor(); }));
    $(sel).innerHTML = addOptions(used());
    $(btn).onclick = () => { const n = $(sel).value; if (n) { DRAFT[key].push(n); renderPolicyEditor(); } };
  };
  chipList("compare", "#pol-compare", "#pol-compare-add", "#pol-compare-btn");
  chipList("per_unit", "#pol-perunit", "#pol-perunit-add", "#pol-perunit-btn");
  policyStatus();
}

$("#pol-preview").onclick = async () => {
  try {
    FLEET = await api("/api/fleet/preview", { policy: DRAFT });
    PREVIEW = dirty();
    showPreviewResult(FLEET);
    const n = FLEET.match.filter((r) => r.drift).length;
    policyStatus(`${PREVIEW ? "Previewing unsaved changes" : "No changes"}: ${n} setting(s) would drift. ${PREVIEW ? "Save to keep them." : ""}`);
  } catch (e) { toast(e.message); }
};
$("#pol-save").onclick = async () => {
  if (!dirty()) { toast("No policy changes to save."); return; }
  try {
    FLEET = await api("/api/fleet/policy", { policy: DRAFT });
    PREVIEW = false;
    $("#pol-preview-result").hidden = true;
    await openPolicyEditor(false);
    toast("Policy saved. Drifting settings can now be updated.");
  } catch (e) { toast(e.message); }
};
$("#pol-discard").onclick = async () => {
  DRAFT = clone(SAVED_POLICY);
  renderPolicyEditor();
  $("#pol-preview-result").hidden = true;
  if (PREVIEW) { PREVIEW = false; FLEET = null; }
};

setInterval(() => { if (!$("#tab-jobs").hidden || !$("#jobs-badge").hidden) loadJobs(); }, 3000);
(async () => {
  let S = null;
  try { S = await api("/api/settings"); } catch {}
  if (S && !S.active_player) {   // first run: nothing works until the player is set
    $("#player").textContent = "no player set";
    $("#settings-badge").hidden = false;
    showTab("settings");
    return;
  }
  const hash = location.hash.slice(1);
  const startTab = hash && document.getElementById("tab-" + hash) ? hash : "dashboard";
  if (startTab !== "controllers") loadControllers();  // fills the header's player; showTab loads it otherwise
  showTab(startTab);
})();
loadJobs();
