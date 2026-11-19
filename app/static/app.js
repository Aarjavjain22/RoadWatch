"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const CLASS_COLORS = { Longitudinal: "#f5c518", Transverse: "#fb923c", Alligator: "#f472b6", Pothole: "#f2545b" };
const ICONS = {
  upload: '<svg viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5M4 20h16"/></svg>',
  refresh: '<svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 0 0-14.9-3M4 13a8 8 0 0 0 14.9 3M4 4v4h4M20 20v-4h-4"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="m7 4 13 8-13 8V4Z"/></svg>',
};

let pageTimer = null;   // per-page polling interval, cleared on navigation
let pageCleanup = null; // per-page teardown (animation loops, listeners)
let modelInfo = null;

// ---------------------------------------------------------------- helpers

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* non-JSON error body */ }
    throw new Error(msg);
  }
  return res.json();
}
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 3500);
}
function fmtTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const num = (v, d = 0) => (v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));
function confColor(c) { return c >= 0.6 ? "var(--accent)" : c >= 0.4 ? "var(--amber)" : "var(--danger)"; }
function conditionChip(cond) {
  if (!cond) return '<span class="chip">untagged</span>';
  const cls = cond === "clear" ? "ok" : cond.includes("+") ? "bad" : "warn";
  return `<span class="chip ${cls}">${esc(cond)}</span>`;
}
function head(eyebrow, title, subtitle, actions = "") {
  return `<div class="page-head"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1>
    <p class="subtitle">${esc(subtitle)}</p></div><div class="actions">${actions}</div></div>`;
}
function offlineBanner(err) {
  return `<div class="banner">Pipeline database not reachable (${esc(err)}). Start the streaming stack with
    <code>docker compose up -d</code> — this page refreshes automatically once Postgres is up.</div>`;
}

function drawBox(ctx, box, label, color, scale, ox = 0, oy = 0) {
  const [x1, y1, x2, y2] = box;
  const x = ox + x1 * scale, y = oy + y1 * scale, w = (x2 - x1) * scale, h = (y2 - y1) * scale;
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = color;
  ctx.strokeRect(x, y, w, h);
  ctx.font = "600 12.5px Inter, Segoe UI, sans-serif";
  const tw = ctx.measureText(label).width + 12;
  const ty = Math.max(0, y - 22);
  ctx.fillStyle = color;
  ctx.fillRect(x - 1.25, ty, tw, 22);
  ctx.fillStyle = "#101418";
  ctx.fillText(label, x + 5, ty + 15);
}

function openLightbox(src, caption) {
  const lb = $("#lightbox");
  $("img", lb).src = src;
  $(".lightbox-caption", lb).textContent = caption || "";
  lb.classList.remove("hidden");
}
$("#lightbox").addEventListener("click", (e) => {
  if (e.target.tagName !== "IMG") $("#lightbox").classList.add("hidden");
});

// ---------------------------------------------------------------- sidebar model card

async function loadModels() {
  modelInfo = await api("/api/models");
  const sel = $("#model-select");
  sel.innerHTML = modelInfo.models.length
    ? modelInfo.models.map((m) => `<option ${m === modelInfo.active ? "selected" : ""}>${esc(m)}</option>`).join("")
    : "<option>no weights found</option>";
  const device = modelInfo.device === "cpu" ? "CPU" : "CUDA GPU";
  $("#model-sub").textContent = `${device} · weather classifier ${modelInfo.weather_classifier ? "on" : "off"}`;
}
$("#model-select").addEventListener("change", async (e) => {
  try {
    await api("/api/models/active", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: e.target.value }) });
    toast(`Active model: ${e.target.value}. New analyses will use it.`);
  } catch (err) { toast(err.message, true); }
});

// ---------------------------------------------------------------- Detection replay

const replayState = { videos: [], videoId: null, detections: [], hazards: [], activeHazard: null };

async function renderReplay() {
  view.innerHTML = `
    ${head("REPLAY. DETECT. KEEP THE EVIDENCE.", "Detection replay",
      "Replay road footage with YOLO bounding boxes and automatically save each hazard's evidence.",
      `<button class="btn primary" id="upload-btn">${ICONS.upload} Upload a video</button>
       <button class="btn" id="refresh-btn">${ICONS.refresh} Refresh videos</button>
       <input type="file" id="upload-input" accept="video/*" hidden />`)}
    <div class="replay-grid">
      <section class="card">
        <div class="picker">
          <div>
            <div class="field-label"><span>Find your video</span><span class="badge grey" id="video-count">0</span></div>
            <div class="search">${ICONS.search}<input class="input" id="video-search" placeholder="Search by file name" /></div>
          </div>
          <div>
            <div class="field-label"><span>Video to analyze</span></div>
            <select class="input" id="video-select"></select>
          </div>
        </div>
        <div class="meta-line" id="video-meta">&nbsp;</div>
        <div class="player" id="player">
          <div class="player-empty">Upload a road video, drop files into <code>data/videos/</code>, or press <b>Refresh videos</b> to load Buffalo GoPro clips from <code>external_datasets</code> (analysis starts when you select a video).</div>
        </div>
        <div class="status-line">
          <div class="left"><span class="dot" id="status-dot"></span><span id="status-text">No video selected</span></div>
          <div class="muted" id="frame-text"></div>
        </div>
        <div class="progress hidden" id="analysis-progress"><span style="width:0"></span></div>
      </section>
      <aside class="card hazard-panel">
        <div class="eyebrow">AUTOMATICALLY LOGGED</div>
        <div class="card-head"><h2>Hazards in this replay</h2><span class="badge" id="hazard-count">0</span></div>
        <div class="hazard-stats">
          <div class="stat"><div class="v" id="unique-hazards">0</div><div class="k">unique hazards</div></div>
          <div class="stat"><div class="v" id="saved-photos">0</div><div class="k">saved photos</div></div>
        </div>
        <p class="note">Up to 10 annotated frames per hazard, stored in SQLite. Click a finding to jump to its video timestamp.</p>
        <div class="hazard-list" id="hazard-list"><div class="empty">Hazards appear here once the video is analyzed.</div></div>
      </aside>
    </div>`;

  $("#upload-btn").onclick = () => $("#upload-input").click();
  $("#upload-input").onchange = uploadVideo;
  $("#refresh-btn").onclick = async () => {
    try {
      const { added } = await api("/api/videos/refresh", { method: "POST" });
      toast(added ? `Added ${added} new video(s) to the library` : "Library is up to date");
      await loadVideoList();
    } catch (err) { toast(err.message, true); }
  };
  $("#video-search").oninput = () => fillVideoSelect();
  $("#video-select").onchange = (e) => selectVideo(Number(e.target.value));

  await loadVideoList();
}

async function uploadVideo(e) {
  const file = e.target.files[0];
  if (!file) return;
  const btn = $("#upload-btn");
  btn.disabled = true;
  btn.innerHTML = `${ICONS.upload} Uploading…`;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const v = await api("/api/videos", { method: "POST", body: fd });
    toast(`${file.name} uploaded — analysis queued`);
    replayState.videoId = v.id;
    await loadVideoList();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `${ICONS.upload} Upload a video`;
    e.target.value = "";
  }
}

async function loadVideoList() {
  replayState.videos = await api("/api/videos");
  if (!replayState.videos.some((v) => v.id === replayState.videoId)) {
    replayState.videoId = replayState.videos[0]?.id ?? null;
  }
  fillVideoSelect();
  if (replayState.videoId != null) await selectVideo(replayState.videoId);
}

function fillVideoSelect() {
  const q = $("#video-search").value.trim().toLowerCase();
  const list = replayState.videos.filter((v) => (v.original_name || v.filename).toLowerCase().includes(q));
  $("#video-count").textContent = `${list.length} video${list.length === 1 ? "" : "s"}`;
  $("#video-select").innerHTML = list.length
    ? list.map((v) => `<option value="${v.id}" ${v.id === replayState.videoId ? "selected" : ""}>${esc(v.original_name || v.filename)}</option>`).join("")
    : "<option>No videos match</option>";
}

async function selectVideo(id) {
  if (pageCleanup) pageCleanup();
  clearInterval(pageTimer);
  replayState.videoId = id;
  replayState.activeHazard = null;
  let v = await api(`/api/videos/${id}`);
  renderVideoMeta(v);
  if (v.status === "pending") {
    await api(`/api/videos/${id}/analyze`, { method: "POST" });
    v = await api(`/api/videos/${id}`);
    renderVideoMeta(v);
    pollAnalysis(id);
  }
  mountPlayer(v);
  if (v.status === "done") {
    await loadAnalysis(id);
  } else {
    replayState.detections = [];
    replayState.hazards = [];
    renderHazards();
    if (v.status === "queued" || v.status === "analyzing") pollAnalysis(id);
  }
}

function renderVideoMeta(v) {
  const yolo = v.status === "done" ? `<b>YOLO enabled</b> · ${esc(v.model || "")}` : `YOLO ${esc(v.status)}`;
  $("#video-meta").innerHTML = `${v.width} x ${v.height} · ${num(v.fps, 1)} source fps · ${fmtTime(v.duration)} · ${yolo}`;
  const prog = $("#analysis-progress");
  const busy = v.status === "queued" || v.status === "analyzing";
  prog.classList.toggle("hidden", !busy);
  $("span", prog).style.width = `${Math.round((v.progress || 0) * 100)}%`;
  $("#status-dot").className = "dot" + (v.status === "failed" ? " off" : "");
  if (busy) {
    $("#status-text").textContent = v.status === "queued" ? "Queued for analysis…" : `Analyzing with YOLO · ${Math.round(v.progress * 100)}%`;
  } else if (v.status === "pending") {
    $("#status-text").textContent = "Registered — select to analyze";
  } else if (v.status === "failed") {
    $("#status-text").innerHTML = `Analysis failed: ${esc(v.error)} <button class="btn small" id="retry-btn">Retry</button>`;
    $("#retry-btn").onclick = () => reanalyze(v.id);
  }
}

async function reanalyze(id) {
  await api(`/api/videos/${id}/analyze`, { method: "POST" });
  toast("Re-analysis queued");
  await selectVideo(id);
}

function pollAnalysis(id) {
  pageTimer = setInterval(async () => {
    if (replayState.videoId !== id) return clearInterval(pageTimer);
    const v = await api(`/api/videos/${id}`);
    renderVideoMeta(v);
    if (v.status === "done") {
      clearInterval(pageTimer);
      toast(`Analysis finished: ${v.hazards} hazards, ${v.photos} photos saved`);
      await loadAnalysis(id);
      renderVideoMeta(v);
    } else if (v.status === "failed") {
      clearInterval(pageTimer);
    }
  }, 1500);
}

async function loadAnalysis(id) {
  [replayState.detections, replayState.hazards] = await Promise.all([
    api(`/api/videos/${id}/detections`), api(`/api/videos/${id}/hazards`),
  ]);
  renderHazards();
  updateStatus();
}

function mountPlayer(v) {
  const player = $("#player");
  player.innerHTML = `<video id="video" src="${esc(v.url)}" controls preload="metadata" playsinline></video><canvas id="overlay"></canvas>`;
  const video = $("#video"), canvas = $("#overlay");
  const ctx = canvas.getContext("2d");
  const sampleWindow = 1.5 / (modelInfo?.sample_fps || 5);
  let raf = 0;

  function frameAt(t) {
    const d = replayState.detections;
    let lo = 0, hi = d.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (d[mid].t <= t + 1e-3) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return best >= 0 && t - d[best].t <= sampleWindow ? d[best] : null;
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const cw = video.clientWidth, ch = video.clientHeight;
    if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
      canvas.width = Math.round(cw * dpr); canvas.height = Math.round(ch * dpr);
      canvas.style.width = `${cw}px`; canvas.style.height = `${ch}px`;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    if (video.videoWidth) {
      const scale = Math.min(cw / video.videoWidth, ch / video.videoHeight);
      const ox = (cw - video.videoWidth * scale) / 2, oy = (ch - video.videoHeight * scale) / 2;
      const f = frameAt(video.currentTime);
      if (f) {
        for (const b of f.boxes) {
          let label = `${b.damage_type} #${b.track_id} ${pct(b.confidence)}`;
          if (b.distance_m != null) label += ` | ${b.distance_m} m`;
          const color = b.track_id === replayState.activeHazard ? "#4a90c4" : CLASS_COLORS[b.damage_type] || "#f5c518";
          drawBox(ctx, b.box, label, color, scale, ox, oy);
        }
      }
    }
    updateStatus();
    raf = requestAnimationFrame(draw);
  }
  raf = requestAnimationFrame(draw);
  pageCleanup = () => { cancelAnimationFrame(raf); video.pause(); };
}

function updateStatus() {
  const video = $("#video");
  const v = replayState.videos.find((x) => x.id === replayState.videoId);
  if (!video || !v) return;
  const fps = v.fps || 25;
  $("#frame-text").textContent = `Frame ${Math.round(video.currentTime * fps)} · ${fmtTime(video.currentTime)} · ${replayState.detections.length ? "saved analysis" : "no detections yet"}`;
  const st = $("#status-text");
  if ($("#analysis-progress").classList.contains("hidden") && !$("#retry-btn")) {
    st.textContent = `${video.paused ? "Paused" : "Playing"} · detections saved`;
  }
}

function renderHazards() {
  const hz = replayState.hazards;
  const photos = hz.reduce((n, h) => n + h.evidence.length, 0);
  $("#hazard-count").textContent = hz.length;
  $("#unique-hazards").textContent = hz.length;
  $("#saved-photos").textContent = photos;
  const list = $("#hazard-list");
  if (!hz.length) {
    list.innerHTML = '<div class="empty">No hazards logged for this video yet.</div>';
    return;
  }
  list.innerHTML = hz.map((h) => {
    const thumb = h.evidence[0]?.url || "";
    const dist = h.distance_m != null ? `${h.distance_m} m <span class="muted">(provisional)</span>` : "distance n/a";
    const weather = h.ml_weather ? ` · ${esc(h.ml_weather)} ${pct(h.ml_weather_conf)}` : "";
    return `<div class="hazard" data-id="${h.id}" data-track="${h.track_id}" data-t="${h.first_t}">
      <img src="${esc(thumb)}" alt="" loading="lazy" />
      <div>
        <div class="hazard-top"><span class="hazard-title">${esc(h.damage_type)} #${h.track_id}</span>
          <span class="conf" style="color:${confColor(h.best_conf)}">${pct(h.best_conf)}</span></div>
        <div class="hazard-sub">${fmtTime(h.first_t)} · visible ${(h.last_t - h.first_t).toFixed(1)} s · ${dist}</div>
        <div class="hazard-sub">${conditionChip(h.visibility_condition)}${weather} · photos saved ${h.evidence.length}/10</div>
      </div>
      <div class="evidence-strip hidden">${h.evidence.map((e) =>
        `<img src="${esc(e.url)}" data-t="${e.t}" data-conf="${e.conf}" alt="" loading="lazy" />`).join("")}</div>
    </div>`;
  }).join("");

  list.querySelectorAll(".hazard").forEach((el) => {
    el.addEventListener("click", (e) => {
      const video = $("#video");
      if (e.target.closest(".evidence-strip")) {
        const img = e.target.closest("img");
        if (!img) return;
        video.currentTime = Number(img.dataset.t);
        openLightbox(img.src, `${el.querySelector(".hazard-title").textContent} · ${fmtTime(img.dataset.t)} · ${pct(Number(img.dataset.conf))}`);
        return;
      }
      list.querySelectorAll(".hazard").forEach((x) => {
        if (x !== el) { x.classList.remove("active"); x.querySelector(".evidence-strip").classList.add("hidden"); }
      });
      el.classList.add("active");
      el.querySelector(".evidence-strip").classList.remove("hidden");
      replayState.activeHazard = Number(el.dataset.track);
      video.pause();
      video.currentTime = Number(el.dataset.t);
    });
  });
}

// ---------------------------------------------------------------- Live monitor

let liveRefreshInFlight = false;

function paintLiveOverlay(img, boxes) {
  const canvas = $("#live-canvas");
  if (!canvas || !img?.naturalWidth) return;
  canvas.width = img.clientWidth;
  canvas.height = img.clientHeight;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const scale = img.clientWidth / img.naturalWidth;
  boxes.forEach((d) =>
    drawBox(ctx, d.bbox, `${d.damage_type} ${pct(d.confidence)}`, CLASS_COLORS[d.damage_type] || "#f5c518", scale));
}

function liveFrameSrc(latest) {
  if (!latest?.source_image) return "";
  const q = encodeURIComponent(latest.event_id || latest.frame_id || latest.event_time || Date.now());
  return `/media/frames/${encodeURIComponent(latest.source_image)}?e=${q}`;
}

async function renderLive() {
  view.innerHTML = `${head("STREAMING PIPELINE", "Live monitor",
    "Detections from the Kafka pipeline joined with the weather agent's verdict for each frame. Refreshes every 5 seconds.")}
    <div id="live-body"><div class="empty">Loading…</div></div>`;
  await refreshLive();
  pageTimer = setInterval(refreshLive, 5000);
}

async function refreshLive() {
  const body = $("#live-body");
  if (!body) return;
  if (liveRefreshInFlight) return;
  liveRefreshInFlight = true;
  let data;
  try {
    data = await api("/api/live");
  } finally {
    liveRefreshInFlight = false;
  }
  if (!data.online) { body.innerHTML = offlineBanner(data.error); return; }
  const s = data.summary, w = data.weather;
  const latest = data.recent[0];
  body.innerHTML = `
    <div class="stats">
      <div class="stat"><div class="v">${num(s.total_detections)}</div><div class="k">detections stored</div></div>
      <div class="stat"><div class="v">${num(s.recent_detections)}</div><div class="k">in the last 10 minutes</div></div>
      <div class="stat"><div class="v">${num(s.frames_tagged)}</div><div class="k">frames weather-tagged</div></div>
      <div class="stat"><div class="v">${s.escalated_pct == null ? "—" : s.escalated_pct + "%"}</div><div class="k">frames escalated (1 h)</div></div>
    </div>
    <div class="two-col">
      <section class="card">
        <div class="card-head"><h2>Latest frame</h2><span class="muted">${latest ? esc(new Date(latest.event_time).toLocaleTimeString()) : ""}</span></div>
        ${latest ? `<div class="frame-view"><img id="live-img" src="${esc(liveFrameSrc(latest))}" alt="" /><canvas id="live-canvas"></canvas></div>
          <div class="status-line"><span>${esc(latest.road_segment)} · ${esc(latest.source_image)}</span>${conditionChip(latest.visibility_condition)}</div>`
          : '<div class="empty">No detections yet — is the producer running?</div>'}
      </section>
      <section class="card">
        <div class="card-head"><h2>Current conditions</h2>${w ? conditionChip(w.visibility_condition) : ""}</div>
        ${w && w.weather_description ? `<p class="note" style="margin:0 0 10px">Aligned with latest detection <code>${esc(w.frame_id || "—")}</code> · Buffalo Open-Meteo cache ~15 min</p>
          <dl class="kv">
          <dt>Open-Meteo</dt><dd>${esc(w.weather_description)}</dd>
          <dt>Temperature</dt><dd>${num(w.weather_temperature_c, 1)} °C</dd>
          <dt>Precipitation</dt><dd>${num(w.weather_precipitation_mm, 1)} mm</dd>
          <dt>Visibility</dt><dd>${w.weather_visibility_m == null ? "—" : num(w.weather_visibility_m / 1000, 1) + " km"}</dd>
          <dt>Weather classifier</dt><dd>${w.ml_weather_class ? `${esc(w.ml_weather_class)} · ${pct(w.ml_weather_confidence)}` : "not loaded"}</dd>
          <dt>As of detection</dt><dd>${esc(new Date(w.context_as_of).toLocaleString())}</dd></dl>`
          : w ? `<div class="empty">Waiting for weather context on <code>${esc(w.frame_id)}</code> — the weather agent is catching up to the live frame stream.</div>`
          : '<div class="empty">The weather agent has not tagged any frames yet.</div>'}
      </section>
    </div>
    <section class="card" style="margin-top:18px">
      <div class="card-head"><h2>Recent detections</h2><span class="badge grey">${data.recent.length}</span></div>
      <table><thead><tr><th>Time</th><th>Segment</th><th>Damage</th><th>Confidence</th><th>Weather-adjusted</th><th>Condition</th><th>Severity</th></tr></thead>
      <tbody>${data.recent.map((d) => `<tr>
        <td>${esc(new Date(d.event_time).toLocaleTimeString())}</td><td>${esc(d.road_segment)}</td>
        <td><span style="color:${CLASS_COLORS[d.damage_type] || "inherit"}">●</span> ${esc(d.damage_type)}</td>
        <td>${pct(d.confidence)}</td><td>${d.adjusted_confidence == null ? "—" : pct(d.adjusted_confidence)}${d.escalation_flag ? ' <span class="chip bad">escalated</span>' : ""}</td>
        <td>${conditionChip(d.visibility_condition)}</td><td>${esc(d.severity || "—")}</td></tr>`).join("")}</tbody></table>
    </section>`;

  const img = $("#live-img");
  if (img && latest) {
    const boxes = data.recent.filter((d) => d.frame_id === latest.frame_id && d.bbox);
    const paint = () => paintLiveOverlay(img, boxes);
    img.onload = paint;
    if (img.complete) paint();
    else img.decode?.().then(paint).catch(() => {});
  }
}

// ---------------------------------------------------------------- Report a hazard

async function renderReport() {
  view.innerHTML = `${head("CROWD-SOURCED FINDINGS", "Report a hazard",
    "Log a road defect you spotted in person. Reports are stored locally next to the detections.")}
    <div class="two-col">
      <form class="card" id="report-form">
        <div class="form-grid">
          <div><div class="field-label">Damage type</div>
            <select class="input" name="damage_type">${["Pothole", "Longitudinal", "Transverse", "Alligator", "Other"].map((d) => `<option>${d}</option>`).join("")}</select></div>
          <div><div class="field-label">Severity</div>
            <div class="segmented" id="severity">${["low", "medium", "high"].map((s) => `<button type="button" data-v="${s}" class="${s === "medium" ? "on" : ""}">${s}</button>`).join("")}</div></div>
          <div class="full"><div class="field-label">Location</div><input class="input" name="location" placeholder="e.g. Main St & Delaware Ave, Buffalo" /></div>
          <div><div class="field-label">Latitude</div><input class="input" name="lat" type="number" step="any" /></div>
          <div><div class="field-label"><span>Longitude</span><a href="#" id="geo-btn" style="color:var(--accent)">Use my location</a></div><input class="input" name="lon" type="number" step="any" /></div>
          <div class="full"><div class="field-label">Notes</div><textarea class="input" name="notes" rows="3" placeholder="Lane, size, anything a crew should know"></textarea></div>
          <div class="full"><div class="field-label">Photo (optional)</div>
            <label class="photo-drop" id="photo-drop"><span>Click to attach a photo</span><input type="file" name="photo" accept="image/*" hidden /></label></div>
          <div class="full"><button class="btn primary" type="submit">Submit report</button></div>
        </div>
      </form>
      <section class="card"><div class="card-head"><h2>Submitted reports</h2><span class="badge grey" id="report-count">0</span></div>
        <div id="report-list"></div></section>
    </div>`;

  const form = $("#report-form");
  let severity = "medium";
  form.querySelectorAll("#severity button").forEach((b) => b.addEventListener("click", () => {
    severity = b.dataset.v;
    form.querySelectorAll("#severity button").forEach((x) => x.classList.toggle("on", x === b));
  }));
  form.photo.addEventListener("change", () => {
    const f = form.photo.files[0];
    $("#photo-drop span").innerHTML = f ? `<img src="${URL.createObjectURL(f)}" alt="" />${esc(f.name)}` : "Click to attach a photo";
  });
  $("#geo-btn").addEventListener("click", (e) => {
    e.preventDefault();
    if (!navigator.geolocation) return toast("Geolocation not available in this browser", true);
    navigator.geolocation.getCurrentPosition(
      (p) => { form.lat.value = p.coords.latitude.toFixed(6); form.lon.value = p.coords.longitude.toFixed(6); },
      (err) => toast(`Location unavailable: ${err.message}`, true));
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    fd.append("severity", severity);
    ["lat", "lon"].forEach((k) => { if (!fd.get(k)) fd.delete(k); });
    if (!form.photo.files.length) fd.delete("photo");
    try {
      await api("/api/reports", { method: "POST", body: fd });
      toast("Report saved");
      form.reset();
      $("#photo-drop span").textContent = "Click to attach a photo";
      await loadReports();
    } catch (err) { toast(err.message, true); }
  });
  await loadReports();
}

async function loadReports() {
  const reports = await api("/api/reports");
  $("#report-count").textContent = reports.length;
  $("#report-list").innerHTML = reports.length ? reports.map((r) => `
    <div class="report-item">
      ${r.photo_url ? `<img class="report-thumb" src="${esc(r.photo_url)}" alt="" />` : '<div class="report-thumb">no photo</div>'}
      <div><div class="hazard-top"><b>${esc(r.damage_type)}</b>
        <span class="chip ${r.severity === "high" ? "bad" : r.severity === "medium" ? "warn" : "ok"}">${esc(r.severity)}</span></div>
        <div class="hazard-sub">${esc(r.location || "no location")}${r.lat != null ? ` · ${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}` : ""}</div>
        <div class="hazard-sub">${esc(r.notes || "")}</div>
        <div class="hazard-sub">${esc(new Date(r.created_at + "Z").toLocaleString())}</div></div>
    </div>`).join("") : '<div class="empty">No reports yet.</div>';
}

// ---------------------------------------------------------------- Weather insights

async function renderWeather() {
  view.innerHTML = `${head("DETECTION × WEATHER", "Weather insights",
    "How visibility conditions change what the detector reports — the join between detection events and the weather agent's per-frame context.")}
    <div id="weather-body"><div class="empty">Loading…</div></div>`;
  await refreshWeather();
  pageTimer = setInterval(refreshWeather, 15000);
}

async function refreshWeather() {
  const body = $("#weather-body");
  if (!body) return;
  const d = await api("/api/weather-insights");
  const replayTable = d.replay_by_condition.length ? `<table><thead><tr><th>Condition</th><th>Hazards</th><th>Avg confidence</th></tr></thead><tbody>
    ${d.replay_by_condition.map((r) => `<tr><td>${conditionChip(r.condition)}</td><td>${r.hazards}</td><td>${pct(r.avg_confidence)}</td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">Analyze a video on the Detection replay page to populate this.</div>';
  const replayCard = `<section class="card"><div class="card-head"><h2>Replay hazards by condition</h2><span class="muted">local videos</span></div>${replayTable}</section>`;

  if (!d.online) { body.innerHTML = offlineBanner(d.error) + replayCard; return; }
  const maxN = Math.max(1, ...d.by_condition.map((r) => r.detections));
  const maxCls = Math.max(1, ...d.ml_classes.map((r) => r.frames));
  body.innerHTML = `
    <section class="card" style="margin-bottom:18px">
      <div class="card-head"><h2>Detections by visibility condition</h2><span class="muted">confidence vs weather-adjusted confidence</span></div>
      ${d.by_condition.length ? `<table><thead><tr><th>Condition</th><th>Detections</th><th></th><th>Avg confidence</th><th>Avg adjusted</th><th>Escalated</th></tr></thead><tbody>
      ${d.by_condition.map((r) => `<tr><td>${conditionChip(r.condition)}</td><td>${num(r.detections)}</td>
        <td style="width:22%"><div class="bar"><span style="width:${(100 * r.detections) / maxN}%"></span></div></td>
        <td>${pct(r.avg_confidence)}</td><td>${pct(r.avg_adjusted)}</td><td>${r.escalated_pct}%</td></tr>`).join("")}</tbody></table>`
        : '<div class="empty">No detections stored yet.</div>'}
    </section>
    <div class="grid-3">
      <section class="card"><div class="card-head"><h2>Clear vs degraded, by damage type</h2></div>
        ${d.by_type.length ? `<table><thead><tr><th>Damage</th><th>Clear</th><th>Degraded</th></tr></thead><tbody>
        ${d.by_type.map((r) => `<tr><td>${esc(r.damage_type)}</td><td>${pct(r.clear_conf)} <span class="muted">(${r.clear_n})</span></td>
          <td>${pct(r.degraded_conf)} <span class="muted">(${r.degraded_n})</span></td></tr>`).join("")}</tbody></table>` : '<div class="empty">No data yet.</div>'}
      </section>
      <section class="card"><div class="card-head"><h2>Weather classifier verdicts</h2></div>
        ${d.ml_classes.length ? `<table><tbody>${d.ml_classes.map((r) => `<tr><td>${esc(r.weather_class)}</td>
          <td style="width:45%"><div class="bar amber"><span style="width:${(100 * r.frames) / maxCls}%"></span></div></td>
          <td>${num(r.frames)}</td><td class="muted">${pct(r.avg_confidence)}</td></tr>`).join("")}</tbody></table>`
          : '<div class="empty">No classifier output yet. Place <code>weather_cls.pt</code> in model/weights/ and rebuild the weather agent.</div>'}
      </section>
      ${replayCard}
    </div>
    <section class="card" style="margin-top:18px"><div class="card-head"><h2>NOAA ground-truth validation</h2><span class="muted">station USW00014733 · Buffalo</span></div>
      ${d.validation.length ? `<table><thead><tr><th>Date</th><th>NOAA reading</th><th>Precip / snow</th><th>NOAA says</th><th>Agent adverse share</th><th>Agreement</th></tr></thead><tbody>
      ${d.validation.map((r) => `<tr><td>${esc(r.observed_date)}</td><td>${esc(r.noaa_observed_date || "—")}${r.is_exact_date_match ? "" : ' <span class="chip">lagged</span>'}</td>
        <td>${num(r.noaa_precip_mm, 1)} / ${num(r.noaa_snow_mm, 1)} mm</td><td>${esc(r.noaa_condition || "—")}</td>
        <td>${pct(r.agent_adverse_pct)}</td><td>${r.agreement ? '<span class="chip ok">agree</span>' : '<span class="chip bad">disagree</span>'}</td></tr>`).join("")}</tbody></table>`
        : '<div class="empty">The ground-truth validator has not run yet.</div>'}
    </section>`;
}

// ---------------------------------------------------------------- Dataset & records

async function renderDatasets(refresh = false) {
  view.innerHTML = `${head("DATA LINEAGE", "Dataset & records",
    "What the models were trained and evaluated on, and everything this workstation has logged.",
    `<button class="btn" id="rescan-btn">${ICONS.refresh} Rescan datasets</button>`)}
    <div id="ds-body"><div class="empty">Scanning dataset folders… (first scan of a large dataset can take a few seconds)</div></div>`;
  $("#rescan-btn").onclick = () => renderDatasets(true);
  const d = await api(`/api/datasets${refresh ? "?refresh=true" : ""}`);
  const r = d.records, res = d.results, inv = d.inventory;

  const evalCards = ["norway_baseline", "norway_comparison"].filter((k) => res[k]).map((k) => `
    <section class="card"><div class="card-head"><h2>${k === "norway_baseline" ? "Norway baseline (before fine-tune)" : "Norway: baseline vs fine-tuned"}</h2></div>
      <table><thead><tr><th>Weights</th><th>mAP50</th><th>mAP50-95</th><th>Precision</th><th>Recall</th></tr></thead><tbody>
      ${res[k].results.map((m) => `<tr><td>${esc(m.weights.split("/").pop())}</td><td><b>${pct(m.map50)}</b></td><td>${pct(m.map50_95)}</td><td>${pct(m.precision)}</td><td>${pct(m.recall)}</td></tr>`).join("")}
      </tbody></table>
      <table style="margin-top:12px"><thead><tr><th>Per class mAP50</th>${res[k].results.map((m) => `<th>${esc(m.weights.split("/").pop())}</th>`).join("")}</tr></thead><tbody>
      ${Object.keys(res[k].results[0].per_class_map50).map((c) => `<tr><td>${esc(c)}</td>${res[k].results.map((m) => `<td>${pct(m.per_class_map50[c])}</td>`).join("")}</tr>`).join("")}
      </tbody></table></section>`).join("");

  const wc = res.weather_classifier;
  const clsCard = `<section class="card"><div class="card-head"><h2>Weather classifier</h2></div>${wc
    ? `<div class="stats" style="margin:0"><div class="stat"><div class="v">${pct(wc.top1_accuracy)}</div><div class="k">top-1 accuracy (val)</div></div>
       <div class="stat"><div class="v">${wc.best_epoch}</div><div class="k">best of ${wc.epochs_run} epochs</div></div></div>`
    : '<div class="empty">Not trained yet.</div>'}</section>`;

  const wcls = inv.prepared.weather_cls;
  const classes = Object.keys(wcls.train || {});
  const maxC = Math.max(1, ...classes.map((c) => wcls.train[c] || 0));

  $("#ds-body").innerHTML = `
    <div class="stats">
      <div class="stat"><div class="v">${r.videos}</div><div class="k">videos in library</div></div>
      <div class="stat"><div class="v">${r.videos_analyzed}</div><div class="k">videos analyzed</div></div>
      <div class="stat"><div class="v">${r.hazards}</div><div class="k">unique hazards logged</div></div>
      <div class="stat"><div class="v">${r.evidence_photos}</div><div class="k">evidence photos</div></div>
      <div class="stat"><div class="v">${r.reports}</div><div class="k">hazard reports</div></div>
    </div>
    <div class="grid-3" style="margin-bottom:18px">${evalCards}${clsCard}</div>
    <div class="two-col">
      <section class="card"><div class="card-head"><h2>Downloaded datasets</h2><span class="muted">data/external_datasets</span></div>
        ${inv.external.length ? `<table><thead><tr><th>Dataset</th><th>Images</th><th>Size</th></tr></thead><tbody>
        ${inv.external.map((x) => `<tr><td>${esc(x.name)}</td><td>${num(x.images)}</td><td>${x.size_mb >= 1000 ? num(x.size_mb / 1000, 1) + " GB" : num(x.size_mb) + " MB"}</td></tr>`).join("")}</tbody></table>`
          : '<div class="empty">No datasets found.</div>'}
      </section>
      <section class="card"><div class="card-head"><h2>Prepared training sets</h2></div>
        <dl class="kv"><dt>RDD2022 Norway (YOLO)</dt><dd>${num(inv.prepared.rdd2022_norway.train)} train · ${num(inv.prepared.rdd2022_norway.val)} val</dd></dl>
        <div class="field-label" style="margin-top:16px">Weather classifier — images per class (train / val)</div>
        ${classes.length ? `<table><tbody>${classes.map((c) => `<tr><td>${esc(c)}</td>
          <td style="width:50%"><div class="bar"><span style="width:${(100 * wcls.train[c]) / maxC}%"></span></div></td>
          <td>${num(wcls.train[c])} / ${num((wcls.val || {})[c])}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">Run model/prepare_weather_cls.py first.</div>'}
      </section>
    </div>`;
}

// ---------------------------------------------------------------- router

const ROUTES = { live: renderLive, report: renderReport, replay: renderReplay, weather: renderWeather, datasets: renderDatasets };

async function route() {
  clearInterval(pageTimer);
  if (pageCleanup) { pageCleanup(); pageCleanup = null; }
  const name = (location.hash.replace("#/", "") || "replay").split("?")[0];
  const render = ROUTES[name] || renderReplay;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === name));
  try {
    await render();
  } catch (err) {
    view.innerHTML = `<div class="banner">Something went wrong loading this page: ${esc(err.message)}</div>`;
  }
}

window.addEventListener("hashchange", route);
loadModels().catch((err) => toast(`Could not load models: ${err.message}`, true)).finally(route);
