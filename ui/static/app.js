"use strict";
const TOKEN = document.querySelector('meta[name="token"]').content;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const gb = (b) => (b / 1e9).toFixed(b < 1e9 ? 2 : 1) + " GB";
const mb = (b) => (b / 1e6).toFixed(1) + " MB";
const fmtTaken = (t) => t ? t.replace(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d).*/, "$1-$2-$3 $4:$5") : "never";

async function api(path, body) {
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json", "X-Token": TOKEN },
                        body: JSON.stringify(body) } : {};
  const r = await fetch(path, opts);
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
  ({ build: loadBuild, flash: loadFlash, jobs: loadJobs, fleet: () => FLEET ? renderFleet() : loadFleet(false) }[name] || (() => {}))();
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
    body.innerHTML = controllers.map((c) => {
      const k = kept.get(c.host) || {};
      return `
      <tr class="${c.online ? "" : "offline"}" data-host="${esc(c.host)}">
        <td class="pick">${c.online ? `<input type="checkbox" data-f="pick" ${k.pick ? "checked" : ""}>` : ""}</td>
        <td><strong>${esc(c.host)}</strong>${c.mode ? `<span class="sub">${esc(c.mode)}</span>` : ""}</td>
        <td>${esc(c.variant || "")}${c.cape ? `<span class="sub">cape ${esc(c.cape)}</span>` : ""}</td>
        <td>${c.online ? esc(c.version) : '<span class="bad">offline</span>'}</td>
        <td>${esc(c.ip)}${c.addresses.length > 1 ? `<span class="sub">${esc(c.addresses.filter((a) => a !== c.ip).join(", "))}</span>` : ""}</td>
        <td>${fmtTaken(c.last_snapshot)}${c.snapshots ? `<span class="sub">${c.snapshots} total</span>` : ""}</td>
        <td class="pick">${c.online ? `<input type="checkbox" data-f="media" title="Include media for ${esc(c.host)}" ${k.media ? "checked" : ""}>` : ""}</td>
      </tr>`;
    }).join("");
    body.querySelectorAll("input").forEach((i) => (i.onchange = syncBackupButtons));
  } catch (e) {
    $("#player").textContent = "player unreachable";
    body.innerHTML = `<tr><td colspan="7" class="bad">${esc(e.message)}</td></tr>`;
  }
  syncBackupButtons();
}

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
    if (TITLES[id] && TITLES[id].startsWith("Back up")) loadControllers();
  });
}

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
  $("#fleet-units").innerHTML = `<thead><tr><th>Controller</th><th>FPP</th><th>Hardware</th><th>Address</th><th>Clock</th><th>Uptime</th><th>Storage</th></tr></thead><tbody>` +
    units.map((u) => !u.online ? `<tr class="offline"><td><span class="dot off"></span><strong>${esc(u.host)}</strong></td><td colspan="6">offline · last known ${esc(u.ip)}</td></tr>` : `
      <tr><td><span class="dot ${u.status === "playing" ? "play" : "on"}"></span><strong>${esc(u.host)}</strong><span class="sub">${esc(u.role)}${u.status ? " · " + esc(u.status) : ""}</span></td>
        <td class="${u.version_ok ? "" : "bad"}">${esc(u.version)}<span class="sub">${esc(u.os_version || "")}</span></td>
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
    <tr><td class="pick">${r.drift ? `<input type="checkbox" data-pick="${esc(r.name)}">` : ""}</td><td><strong>${esc(r.name)}</strong><span class="why">${esc(r.why || r.description)}${r.needs ? ` · needs player ${r.needs}` : ""}</span></td>
      <td>${esc(r.expected === "@master" ? "the player" : shown(r.expected))}${r.player !== null && r.player !== undefined
        ? `<span class="sub">player: ${r.player === "*" ? "not checked" : esc(shown(r.player))}</span>` : ""}</td>` +
    hosts.map((h) => {
      const c = r.cells[h] || { status: "n/a" };
      const cls = (c.status === "n/a" ? "na" : c.status) + (isPlayer(h) ? " player" : "");
      const text = c.status === "ok" ? "✓" : c.status === "n/a" ? "n/a" : c.status === "exempt" ? "—" : val(c.value);
      return `<td class="cell ${cls}" title="${esc(h)}: ${esc(shown(c.value))}${c.expected !== undefined && c.expected !== null ? " (expected " + esc(shown(c.expected)) + ")" : ""}"
        ${c.status === "drift" ? `data-expect="${esc(r.name)}" data-value="${esc(c.value)}"` : ""}>${text}</td>`;
    }).join("") +
    `<td>${r.drift ? `<button class="small primary" data-apply="${esc(r.name)}" ${PREVIEW ? "disabled title=\"Save the policy first\"" : ""}>Apply to ${r.drift}</button>` : ""}</td></tr>`).join("") + verRow + "</tbody>";

  document.querySelectorAll("[data-apply]").forEach((b) => (b.onclick = () => runFleetUpdate([b.dataset.apply])));
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
  const needs = rows.filter((r) => r.needs).map((r) => r.name);
  const restartLine = needs.length ? (restart ? `\nAfterwards, restart the players on the affected units (${needs.join(", ")} need it); units that are playing are skipped.`
                                              : `\n${needs.join(", ")} will need a player restart to take effect.`) : "";
  if (!confirm(`Update ${rows.length} setting(s)? This writes to the controllers.\n\n${lines}\n${restartLine}\n\nEach setting runs in turn; the fleet is rescanned at the end.`)) return;
  if (!restart && needs.length) {
    rows.filter((r) => r.needs).forEach((r) => Object.entries(r.cells).filter(([, c]) => c.status === "drift").forEach(([h]) => pendingRestart.hosts.add(h)));
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

$("#policy-toggle").onclick = () => (POLICY_OPEN ? closePolicyEditor() : openPolicyEditor());

async function openPolicyEditor(keepDraft) {
  const d = await api("/api/fleet/policy");
  CATALOG = d.catalog || {};
  const hadChanges = keepDraft && dirty();
  SAVED_POLICY = d.policy;
  if (!hadChanges) DRAFT = clone(d.policy);
  POLICY_OPEN = true;
  $("#policy-editor").hidden = false;
  $("#policy-toggle").textContent = "Close policy editor";
  renderPolicyEditor();
}

function closePolicyEditor() {
  if (dirty() && !confirm("Close the editor and discard unsaved policy changes?")) return;
  POLICY_OPEN = false;
  $("#policy-editor").hidden = true;
  $("#policy-toggle").textContent = "Edit policy";
  if (PREVIEW) loadFleet(false);
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
    renderFleet();
    const n = FLEET.match.filter((r) => r.drift).length;
    policyStatus(`${PREVIEW ? "Previewing unsaved changes" : "No changes"}: ${n} setting(s) would drift. ${PREVIEW ? "Save to keep them." : ""}`);
  } catch (e) { toast(e.message); }
};
$("#pol-save").onclick = async () => {
  if (!dirty()) { toast("No policy changes to save."); return; }
  try {
    FLEET = await api("/api/fleet/policy", { policy: DRAFT });
    PREVIEW = false;
    renderFleet();
    await openPolicyEditor(false);
    toast("Policy saved. Drifting settings can now be updated.");
  } catch (e) { toast(e.message); }
};
$("#pol-discard").onclick = async () => {
  DRAFT = clone(SAVED_POLICY);
  renderPolicyEditor();
  if (PREVIEW) await loadFleet(false);
};

setInterval(() => { if (!$("#tab-jobs").hidden || !$("#jobs-badge").hidden) loadJobs(); }, 3000);
loadControllers();
const startTab = location.hash.slice(1);
if (startTab && document.getElementById("tab-" + startTab)) showTab(startTab);
loadJobs();
