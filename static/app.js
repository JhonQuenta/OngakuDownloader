const state = {
  songs: [], filtered: [], playlistName: "Playlist", playlistUploader: "",
  view: localStorage.getItem("music-view") || "grid", page: 1, pageSize: 100,
  coverSize: 500, jobId: null, pollTimer: null, downloadPath: "", failed: []
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.add("hidden"), 3000);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})}
  });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(data.error || `Error ${response.status}`);
  return data;
}

function durationText(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const value = Math.max(0, Math.round(Number(seconds)));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

function normalizeTitle(title) {
  return String(title || "")
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/\b(official|oficial|video|audio|lyric|lyrics|letra|hd|hq|remaster(?:ed|izado)?|feat|ft)\b/g, " ")
    .replace(/[\[\](){}|·•_\-–—]/g, " ").replace(/\s+/g, " ").trim();
}

function duplicateGroups() {
  const groups = new Map();
  for (const song of state.songs) {
    const key = normalizeTitle(song.title);
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(song);
  }
  return [...groups.values()].filter(group => group.length > 1);
}

function updateCounts() {
  $("selectedCount").textContent = state.songs.filter(song => song.selected).length;
  $("resultCount").textContent = `${state.filtered.length} resultados`;
  const groups = duplicateGroups();
  const count = groups.reduce((sum, group) => sum + group.length, 0);
  $("duplicateCount").textContent = count;
  $("duplicateCount").classList.toggle("hidden", count === 0);
}

function applyFilter(resetPage = true) {
  const query = $("searchInput").value.trim().toLocaleLowerCase();
  state.filtered = query ? state.songs.filter(song => `${song.title} ${song.artist}`.toLocaleLowerCase().includes(query)) : [...state.songs];
  if (resetPage) state.page = 1;
  renderSongs();
}

function renderSongs() {
  const start = (state.page - 1) * state.pageSize;
  const pageSongs = state.filtered.slice(start, start + state.pageSize);
  const container = $("songs");
  container.className = `songs ${state.view}-view`;
  container.innerHTML = pageSongs.map(song => `
    <article class="song-card ${song.selected ? "selected" : ""}" data-uid="${escapeHtml(song.uid)}">
      <input class="song-check" type="checkbox" ${song.selected ? "checked" : ""} aria-label="Seleccionar ${escapeHtml(song.title)}">
      <div class="cover">
        ${song.thumbnail ? `<img src="${escapeHtml(song.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
        <span class="song-number">#${song.index}</span>
      </div>
      <div class="song-body">
        <h3 class="song-title" title="${escapeHtml(song.title)}">${escapeHtml(song.title)}</h3>
        <p class="song-artist" title="${escapeHtml(song.artist)}">${escapeHtml(song.artist)}</p>
        <div class="song-footer">
          <span class="duration">${durationText(song.duration)}</span>
          <button class="lyrics-btn" data-lyrics="${escapeHtml(song.uid)}">Ver letra</button>
        </div>
      </div>
    </article>`).join("");
  $("emptyState").classList.toggle("hidden", state.filtered.length > 0);
  bindSongEvents();
  renderPagination();
  updateCounts();
  const visible = pageSongs.length > 0 && pageSongs.every(song => song.selected);
  $("selectVisible").checked = visible;
  $("selectVisible").indeterminate = pageSongs.some(song => song.selected) && !visible;
}

function bindSongEvents() {
  document.querySelectorAll(".song-card").forEach(card => {
    const song = state.songs.find(item => item.uid === card.dataset.uid);
    card.querySelector(".song-check").addEventListener("change", event => {
      song.selected = event.target.checked;
      card.classList.toggle("selected", song.selected);
      updateCounts();
    });
  });
  document.querySelectorAll("[data-lyrics]").forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    showLyrics(state.songs.find(song => song.uid === button.dataset.lyrics));
  }));
}

function renderPagination() {
  const pages = Math.ceil(state.filtered.length / state.pageSize);
  const nav = $("pagination");
  if (pages <= 1) { nav.innerHTML = ""; return; }
  const candidates = new Set([1, pages, state.page - 2, state.page - 1, state.page, state.page + 1, state.page + 2]);
  const numbers = [...candidates].filter(n => n >= 1 && n <= pages).sort((a, b) => a - b);
  let previous = 0;
  nav.innerHTML = numbers.map(n => {
    const gap = n - previous > 1 ? `<span class="muted">…</span>` : "";
    previous = n;
    return `${gap}<button data-page="${n}" class="${n === state.page ? "active" : ""}">${n}</button>`;
  }).join("");
  nav.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
    state.page = Number(button.dataset.page);
    renderSongs();
    $("songs").scrollIntoView({behavior: "smooth", block: "start"});
  }));
}

async function loadPlaylist() {
  const url = $("playlistUrl").value.trim();
  if (!url) return toast("Pega el enlace de una playlist.");
  $("loadBtn").disabled = true;
  $("loadStatus").className = "inline-status";
  $("loadStatus").textContent = "Leyendo el índice de la playlist… En listas grandes puede tardar un momento.";
  try {
    const data = await api("/api/playlist", {method: "POST", body: JSON.stringify({url, auth_browser: $("authBrowserSelect").value})});
    state.playlistName = data.title;
    state.playlistUploader = data.uploader;
    state.songs = data.songs.map((song, position) => ({...song, id: song.id || `track-${position}`, uid: `${song.id || "track"}-${position}`, selected: true}));
    state.filtered = [...state.songs];
    state.page = 1;
    $("playlistTitle").textContent = data.title;
    $("playlistMeta").textContent = `${data.count} canciones${data.uploader ? ` · ${data.uploader}` : ""}`;
    $("workspace").classList.remove("hidden");
    $("loadStatus").classList.add("hidden");
    renderSongs();
    setView(state.view);
    $("workspace").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) {
    $("loadStatus").className = "inline-status error";
    $("loadStatus").textContent = error.message;
  } finally { $("loadBtn").disabled = false; }
}

function setView(view) {
  state.view = view;
  localStorage.setItem("music-view", view);
  $("gridViewBtn").classList.toggle("active", view === "grid");
  $("listViewBtn").classList.toggle("active", view === "list");
  renderSongs();
}

function showDuplicates() {
  const groups = duplicateGroups();
  if (!groups.length) return toast("No se encontraron canciones repetidas.");
  $("duplicateGroups").innerHTML = groups.map((group, groupIndex) => `
    <section class="duplicate-group">
      <h3>Grupo ${groupIndex + 1} · ${escapeHtml(group[0].title)}</h3>
      ${group.map(song => `
        <label class="duplicate-track">
          <input type="checkbox" data-dup-id="${escapeHtml(song.uid)}" ${song.selected ? "checked" : ""}>
          ${song.thumbnail ? `<img src="${escapeHtml(song.thumbnail)}" alt="" loading="lazy" decoding="async" width="48" height="48" referrerpolicy="no-referrer">` : "<span></span>"}
          <span><strong>${escapeHtml(song.title)}</strong><small>#${song.index} · ${escapeHtml(song.artist)}</small></span>
        </label>`).join("")}
    </section>`).join("");
  $("duplicatesModal").classList.remove("hidden");
}

$("duplicateGroups").addEventListener("change", event => {
  const input = event.target.closest("[data-dup-id]");
  if (!input) return;
  const song = state.songs.find(item => item.uid === input.dataset.dupId);
  if (song) song.selected = input.checked;
  updateCounts();
});

async function showLyrics(song) {
  if (!song) return;
  $("lyricsTitle").textContent = song.title;
  $("lyricsText").textContent = "Buscando letra o subtítulos…";
  $("lyricsModal").classList.remove("hidden");
  try {
    const data = await api("/api/track-details", {method: "POST", body: JSON.stringify({url: song.url, include_lyrics: true})});
    $("lyricsText").textContent = data.lyrics_available && data.lyrics ? data.lyrics : "No hay letra ni subtítulos disponibles para esta canción.";
  } catch (error) { $("lyricsText").textContent = error.message; }
}

function downloadPayload(songs) {
  return {
    songs, playlist_name: state.playlistName,
    format: $("formatSelect").value,
    cover: $("coverToggle").checked,
    cover_size: state.coverSize,
    lyrics: $("lyricsToggle").checked,
    concurrency: Number($("concurrencySelect").value),
    timeout_minutes: Number($("timeoutSelect").value),
    auth_browser: $("authBrowserSelect").value,
    download_root: $("downloadRoot").value.trim()
  };
}

async function startDownload(songs, retryLabel = "") {
  if (!songs.length) return toast("Selecciona al menos una canción.");
  if (state.jobId) return toast("Ya hay una descarga activa.");
  $("errorsPanel").classList.add("hidden");
  $("downloadModal").classList.remove("hidden");
  $("downloadSummary").textContent = retryLabel || "Probando acceso con una canción antes de crear la cola…";
  $("progressFill").style.width = "0%";
  $("progressPercent").textContent = "0%";
  $("progressNumbers").textContent = `0 de ${songs.length}`;
  $("activeTracks").innerHTML = "";
  $("cancelBtn").classList.remove("hidden");
  $("closeDownloadBtn").classList.add("hidden");
  $("openFolderBtn").disabled = true;
  try {
    const data = await api("/api/downloads", {method: "POST", body: JSON.stringify(downloadPayload(songs))});
    state.jobId = data.job_id;
    state.downloadPath = data.download_path;
    $("destinationPath").textContent = data.download_path;
    $("openFolderBtn").disabled = false;
    pollJob();
  } catch (error) {
    $("downloadSummary").textContent = error.message;
    $("cancelBtn").classList.add("hidden");
    $("closeDownloadBtn").classList.remove("hidden");
  }
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const job = await api(`/api/downloads/${state.jobId}`);
    const percent = job.total ? Math.round(job.finished / job.total * 100) : 0;
    $("progressFill").style.width = `${percent}%`;
    $("progressPercent").textContent = `${percent}%`;
    $("progressNumbers").textContent = `${job.finished} de ${job.total}`;
    $("downloadSummary").textContent = job.status === "cancelling"
      ? "Cancelando procesos activos…"
      : job.status === "adding_lyrics"
        ? `Audio completado · incrustando letras (${job.succeeded} canciones)…`
      : `${job.succeeded} completadas · ${job.failed} con error${job.cancelled_count ? ` · ${job.cancelled_count} canceladas` : ""}`;
    $("activeTracks").innerHTML = job.active.slice(0, 8).map(track => `<div class="active-track">↓ ${escapeHtml(track.title)}</div>`).join("");
    if (["completed", "completed_with_errors", "cancelled", "blocked"].includes(job.status)) {
      state.failed = job.errors || [];
      state.jobId = null;
      $("cancelBtn").classList.add("hidden");
      $("closeDownloadBtn").classList.remove("hidden");
      $("downloadTitle").textContent = job.status === "blocked" ? "YouTube bloqueó la descarga" : (job.status === "cancelled" ? "Descarga cancelada" : (job.failed ? "Descarga terminada con avisos" : "Descarga completada"));
      if (job.fatal_error) $("downloadSummary").textContent = job.fatal_error;
      if (state.failed.length) showErrors();
      return;
    }
  } catch (error) { $("downloadSummary").textContent = error.message; }
  state.pollTimer = setTimeout(pollJob, 1000);
}

function showErrors() {
  $("errorCount").textContent = state.failed.length;
  $("errorsList").innerHTML = state.failed.map((item, index) => `
    <article class="error-item">
      <strong>${escapeHtml(item.song.title)}</strong>
      <pre>${escapeHtml(item.error)}</pre>
      <div class="error-actions"><button class="secondary-btn" data-retry-index="${index}">Reintentar esta canción</button></div>
    </article>`).join("");
  $("errorsList").querySelectorAll("[data-retry-index]").forEach(button => button.addEventListener("click", () => {
    const item = state.failed[Number(button.dataset.retryIndex)];
    startDownload([item.song], `Reintentando: ${item.song.title}`);
  }));
  $("errorsPanel").classList.remove("hidden");
}

async function cancelDownload() {
  if (!state.jobId) return;
  $("cancelBtn").disabled = true;
  try {
    await api(`/api/downloads/${state.jobId}/cancel`, {method: "POST", body: "{}"});
    $("downloadSummary").textContent = "Cancelando procesos activos…";
  } catch (error) { toast(error.message); }
  finally { $("cancelBtn").disabled = false; }
}

async function init() {
  try {
    const health = await api("/api/health");
    $("serverBadge").textContent = health.ffmpeg ? `Listo · yt-dlp ${health.yt_dlp}` : "Falta FFmpeg";
    $("serverBadge").className = `server-badge ${health.ffmpeg ? "ok" : "bad"}`;
    $("downloadRoot").value = health.default_download_path;
    $("downloadRoot").title = health.default_download_path;
  } catch { $("serverBadge").textContent = "Servidor no disponible"; $("serverBadge").className = "server-badge bad"; }
}

$("loadBtn").addEventListener("click", loadPlaylist);
$("playlistUrl").addEventListener("keydown", event => { if (event.key === "Enter") loadPlaylist(); });
$("authBrowserSelect").addEventListener("change", event => {
  const browser = event.target.value;
  $("authHint").textContent = browser === "none"
    ? "Recomendado para playlists públicas; no accede a ninguna sesión."
    : `Para leer sus cookies, ${browser === "edge" ? "Edge" : browser === "firefox" ? "Firefox" : "Chrome"} debe estar completamente cerrado. Abre esta página en un navegador diferente.`;
});
$("searchInput").addEventListener("input", () => applyFilter(true));
$("gridViewBtn").addEventListener("click", () => setView("grid"));
$("listViewBtn").addEventListener("click", () => setView("list"));
$("duplicatesBtn").addEventListener("click", showDuplicates);
$("selectAllBtn").addEventListener("click", () => { state.songs.forEach(song => song.selected = true); renderSongs(); });
$("clearAllBtn").addEventListener("click", () => { state.songs.forEach(song => song.selected = false); renderSongs(); });
$("chooseFolderBtn").addEventListener("click", async () => {
  const button = $("chooseFolderBtn");
  button.disabled = true;
  try {
    const data = await api("/api/select-folder", {
      method: "POST",
      body: JSON.stringify({initial: $("downloadRoot").value})
    });
    if (!data.cancelled && data.path) {
      $("downloadRoot").value = data.path;
      $("downloadRoot").title = data.path;
    }
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("selectVisible").addEventListener("change", event => {
  const start = (state.page - 1) * state.pageSize;
  state.filtered.slice(start, start + state.pageSize).forEach(song => song.selected = event.target.checked);
  renderSongs();
});
$("coverSizes").querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
  state.coverSize = Number(button.dataset.size);
  $("coverSizes").querySelectorAll("button").forEach(item => item.classList.toggle("active", item === button));
}));
$("downloadBtn").addEventListener("click", () => startDownload(state.songs.filter(song => song.selected)));
$("cancelBtn").addEventListener("click", cancelDownload);
$("closeDownloadBtn").addEventListener("click", () => { $("downloadModal").classList.add("hidden"); $("downloadTitle").textContent = "Descargando playlist"; });
$("openFolderBtn").addEventListener("click", async () => {
  try { await api("/api/open-folder", {method: "POST", body: JSON.stringify({path: state.downloadPath})}); }
  catch (error) { toast(error.message); }
});
$("retryAllBtn").addEventListener("click", () => startDownload(state.failed.map(item => item.song), "Reintentando canciones fallidas…"));
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => {
  $(button.dataset.close).classList.add("hidden");
  if (button.dataset.close === "duplicatesModal") renderSongs();
}));
document.querySelectorAll(".modal").forEach(modal => modal.addEventListener("click", event => {
  if (event.target === modal && modal.id !== "downloadModal") { modal.classList.add("hidden"); if (modal.id === "duplicatesModal") renderSongs(); }
}));

init();



(() => {
  const icon = document.getElementById("appFavicon");
  if (!icon) return;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let timer = null;
  let frame = 0;
  const draw = () => {
    const bars = [16, 32, 22].map((height, index) => {
      const h = Math.round(height * (0.75 + 0.25 * Math.sin(frame * 0.7 + index * 1.4)));
      return '<rect x="' + (18 + index * 11) + '" y="' + (49 - h) + '" width="7" height="' + h + '" rx="3.5"/>';
    }).join("");
    icon.href = "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="red" x2="1" y2="1"><stop stop-color="#ff3d58"/><stop offset="1" stop-color="#ff6b4a"/></linearGradient></defs><rect width="64" height="64" rx="18" fill="url(#red)"/><g fill="white">' + bars + '</g></svg>');
    frame += 1;
  };
  const update = () => {
    if (timer) clearInterval(timer);
    timer = null;
    if (reducedMotion.matches) {
      icon.href = "/static/favicon.svg?v=1";
    } else if (!document.hidden) {
      draw();
      timer = setInterval(draw, 250);
    }
  };
  document.addEventListener("visibilitychange", update);
  reducedMotion.addEventListener("change", update);
  window.addEventListener("pagehide", () => clearInterval(timer));
  update();
})();
