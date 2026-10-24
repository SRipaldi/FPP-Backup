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
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("main section").forEach((s) => (s.hidden = s.id !== "tab-" + name));
  ({ build: loadBuild, flash: loadFlash, jobs: loadJobs }[name] || (() => {}))();
}
document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ------------------------------------------------------------ controllers
async function loadControllers() {
  const body = $("#controllers");
  try {
    const { master, controllers } = await api("/api/controllers");
    $("#player").textContent = `player ${master}`;
    body.innerHTML = controllers.map((c) => `
      <tr class="${c.online ? "" : "offline"}">
        <td><strong>${esc(c.host)}</strong>${c.mode ? `<span class="sub">${esc(c.mode)}</span>` : ""}</td>
        <td>${esc(c.variant || "")}${c.cape ? `<span class="sub">cape ${esc(c.cape)}</span>` : ""}</td>
        <td>${c.online ? esc(c.version) : '<span class="bad">offline</span>'}</td>
        <td>${esc(c.ip)}${c.addresses.length > 1 ? `<span class="sub">${esc(c.addresses.filter((a) => a !== c.ip).join(", "))}</span>` : ""}</td>
        <td>${fmtTaken(c.last_snapshot)}${c.snapshots ? `<span class="sub">${c.snapshots} total</span>` : ""}</td>
        <td class="actions">${c.online ? `
          <button class="small" data-snap="${esc(c.ip)}" data-via="${esc(c.via || "")}" data-host="${esc(c.host)}">Snapshot</button>
          <button class="small" data-snap="${esc(c.ip)}" data-via="${esc(c.via || "")}" data-host="${esc(c.host)}" data-media="1">+ media</button>` : ""}
        </td>
      </tr>`).join("");
    body.querySelectorAll("[data-snap]").forEach((b) => (b.onclick = () =>
      act("/api/snapshot", { ip: b.dataset.snap, via: b.dataset.via || null, host: b.dataset.host, media: !!b.dataset.media })));
  } catch (e) {
    $("#player").textContent = "player unreachable";
    body.innerHTML = `<tr><td colspan="6" class="bad">${esc(e.message)}</td></tr>`;
  }
}
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
  });
}

setInterval(() => { if (!$("#tab-jobs").hidden || !$("#jobs-badge").hidden) loadJobs(); }, 3000);
loadControllers();
loadJobs();
