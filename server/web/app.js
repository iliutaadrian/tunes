// Tunes web app. Vanilla JS, no build step. Hash routes, state lives on the server.
(() => {
  "use strict";
  const view = document.getElementById("view");
  const statusBar = document.getElementById("statusBar");
  const TOKEN_KEY = "tunes.token";
  let prefs = {};          // settings kept on the server (data/settings.json), the same on every device
  let player = null;       // active YouTube player, if any
  let renderId = 0;        // guards against a slow page landing after a newer one
  let listen = null;       // state of the Listen page: { mood, tracks, at, folder }
  let lib = null;          // state of the Library page: { tracks, order, pos, shuffle, errors }
  let pollTimer = null;

  // ---------- helpers ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = { get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };
  const art = (id) => `/art/${id}?token=${encodeURIComponent(store.get(TOKEN_KEY))}`;  // cover of a library track
  const ICON = {
    play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h4v14H7zm6 0h4v14h-4z"/></svg>',
    prev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg>',
    next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg>',
    save: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 4h2v8.2l3.3-3.3 1.4 1.4L12 16l-5.7-5.7 1.4-1.4L11 12.2zM5 18h14v2H5z"/></svg>',
    shuffle: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.6 9.2 5.4 4 4 5.4l5.2 5.2zM14.5 4l2 2L4 18.6 5.4 20 18 7.5l2 2V4zm.3 9.4-1.4 1.4 3.1 3.1-2 2.1H20v-5.5l-2 2z"/></svg>',
  };
  const thumb = (vid) => `https://i.ytimg.com/vi/${esc(vid)}/mqdefault.jpg`;

  async function api(path, opts = {}) {
    const res = await fetch("/api/" + path, {
      method: opts.method || "GET",
      headers: { Authorization: "Bearer " + store.get(TOKEN_KEY), ...(opts.body ? { "Content-Type": "application/json" } : {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) { askToken(); throw new Error("Token required"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Request failed (" + res.status + ")");
    return data;
  }

  async function loadPrefs() { prefs = await api("settings"); }
  function setPref(key, value) {
    if (prefs[key] === value) return;
    prefs[key] = value;
    api("settings", { method: "POST", body: { [key]: value } }).catch(() => {});  // a lost save only costs a preference
  }

  function askToken() {
    stopPlayer();
    view.innerHTML = `<h1>Token</h1><p class="sub">Paste the app token from tunes.json. It is stored only in this browser.</p>
      <form class="inline" id="tokenForm"><input type="password" id="tokenInput" placeholder="App token" autocomplete="off" aria-label="App token">
      <button class="btn">Save</button></form>`;
    document.getElementById("tokenForm").addEventListener("submit", (e) => {
      e.preventDefault();
      store.set(TOKEN_KEY, document.getElementById("tokenInput").value.trim());
      route();
    });
  }

  function clock(sec) {
    if (!sec) return "";
    sec = Math.round(sec);
    return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
  }

  function ago(ts) {
    if (!ts) return "";
    const d = (Date.now() / 1000 - ts) / 86400;
    if (d < 1 / 24) { const m = Math.round(d * 1440); return m < 1 ? "just now" : m + " min ago"; }
    if (d < 1) return Math.round(d * 24) + " h ago";
    return Math.floor(d) + (Math.floor(d) === 1 ? " day ago" : " days ago");
  }

  function fail(err) {
    if (err.message === "Token required") return;
    view.querySelector(".error.flash")?.remove();
    view.insertAdjacentHTML("afterbegin", `<p class="error flash" role="alert">${esc(err.message)}</p>`);
  }

  const options = (list, chosen) => list.map((f) => `<option${f === chosen ? " selected" : ""}>${esc(f)}</option>`).join("");

  // ---------- header status: downloads and the library import ----------
  async function refreshStatus() {
    let s;
    try { s = await api("status"); } catch { return null; }
    const bits = [];
    if (s.downloading) bits.push(`Downloading ${s.downloading}…`);
    if (s.failed.length) bits.push(`<span class="error" title="${esc(s.failed.map((f) => (f.title || f.vid) + ": " + f.error).join("\n"))}">${s.failed.length} failed</span>`);
    if (s.sync.running) bits.push("Importing into the library…");
    else if (s.sync.enabled && s.sync.waiting && !s.downloading) bits.push(`<button class="btn small primary" id="syncBtn">Send ${s.sync.waiting} to Plex</button>`);
    else if (s.sync.ok === false) bits.push(`<span class="error" title="${esc(s.sync.tail)}">Import failed</span>`);
    statusBar.innerHTML = bits.join(" ");
    document.getElementById("syncBtn")?.addEventListener("click", () => api("sync", { method: "POST" }).then(refreshStatus).catch(fail));
    clearTimeout(pollTimer);
    if (s.downloading || s.sync.running) pollTimer = setTimeout(refreshStatus, 5000);
    return s;
  }

  // ---------- Listen ----------
  async function listenPage(moodArg) {
    const mine = renderId;
    const [{ moods }, status] = await Promise.all([api("moods"), refreshStatus()]);
    if (mine !== renderId) return;
    if (!moods.length) { view.innerHTML = `<p class="empty">No moods yet. <a href="#/moods"><u>Add one</u></a> to get recommendations.</p>`; return; }
    const wanted = moodArg || prefs.mood;
    const mood = moods.find((m) => String(m.id) === wanted) || moods.find((m) => m.waiting) || moods[0];
    setPref("mood", String(mood.id));
    const { tracks } = await api("queue?mood=" + mood.id);
    if (mine !== renderId) return;
    const folders = status ? status.folders : [mood.folder];
    listen = { mood, tracks, at: 0, errors: 0 };

    view.innerHTML = `<div class="chips">${moods.map((m) =>
      `<a class="chip${m.id === mood.id ? " on" : ""}" href="#/listen/${m.id}">${esc(m.name)}<b>${m.waiting || ""}</b></a>`).join("")}</div>
      <div class="listen">
        <section class="now" aria-label="Now playing">
          <div class="player"><div id="frame"></div></div>
          <h1 id="nowTitle"></h1><div class="meta" id="nowMeta"></div>
          <div class="actions">
            <button class="btn primary" id="keepBtn">Keep</button>
            <select id="folder" aria-label="Playlist folder to keep into">${options(folders, mood.folder)}</select>
            <button class="btn" id="skipBtn">Skip</button>
            <button class="btn" id="nextBtn">Later</button>
          </div>
          <div class="meta" id="note" role="status"></div>
          <div class="box" id="whyBox"><div class="label">Because you have</div><ul class="why" id="why"></ul></div>
          <div class="keys"><kbd>K</kbd> keep · <kbd>S</kbd> skip · <kbd>L</kbd> later</div>
        </section>
        <section aria-label="Queue">
          <div class="queue-head"><h2 id="queueTitle"></h2>
            <button class="btn small" id="roundBtn">New round</button></div>
          <p class="meta" id="roundNote"></p>
          <div id="queue"></div>
        </section>
      </div>`;
    document.getElementById("keepBtn").addEventListener("click", () => judge("keep"));
    document.getElementById("skipBtn").addEventListener("click", () => judge("skip"));
    document.getElementById("nextBtn").addEventListener("click", () => play(listen.at + 1, true));
    document.getElementById("roundBtn").addEventListener("click", () =>
      api(`moods/${mood.id}/generate`, { method: "POST" }).then(() => watchRound(mine)).catch(fail));
    document.getElementById("queue").addEventListener("click", (e) => {
      const row = e.target.closest(".track");
      if (row) play(listen.tracks.findIndex((t) => t.vid === row.dataset.vid), true);
    });
    drawQueue();
    // A mood that has never had a round starts its first one by itself.
    if (!tracks.length && !mood.round && !mood.generated_at && !mood.error) {
      try { await api(`moods/${mood.id}/generate`, { method: "POST" }); mood.round = { done: 0, total: 0 }; } catch (err) { fail(err); }
    }
    showRound(mood);
    if (mood.round) watchRound(mine);
    if (tracks.length) await startPlayer(mine);
  }

  function showRound(mood) {
    const btn = document.getElementById("roundBtn"), note = document.getElementById("roundNote");
    if (!btn) return;
    btn.disabled = !!mood.round;
    btn.textContent = mood.round ? "Listening to your seeds…" : "New round";
    note.innerHTML = mood.round
      ? (mood.round.total ? `Reading YouTube mixes: ${mood.round.done} of ${mood.round.total} seeds. New ones take a few seconds each.` : "Waiting for another round to finish.")
      : (mood.error ? `<span class="error">${esc(mood.error)}</span>` : mood.generated_at ? "Last round " + ago(mood.generated_at) + "." : "No round yet. Start one to fill the queue.");
  }

  // Follow a running round, then load its queue.
  async function watchRound(mine) {
    while (mine === renderId) {
      await new Promise((r) => setTimeout(r, 2500));
      if (mine !== renderId) return;
      let mood;
      try { mood = (await api("moods")).moods.find((m) => m.id === listen.mood.id); } catch { continue; }
      if (!mood) return;
      showRound(mood);
      if (!mood.round) { if (mine === renderId) route(); return; }
    }
  }

  function drawQueue() {
    const { tracks, at } = listen;
    document.getElementById("queueTitle").textContent = tracks.length ? tracks.length + " waiting" : "Queue";
    document.getElementById("queue").innerHTML = tracks.map((t, i) => `<button class="track${i === at ? " on" : ""}" data-vid="${esc(t.vid)}">
      <img loading="lazy" alt="" src="${thumb(t.vid)}">
      <span><strong>${esc(t.title)}</strong><span class="meta">${esc(t.uploader || "")}</span></span>
      <span class="meta">${clock(t.duration)}</span></button>`).join("") || `<p class="empty">Nothing waiting in this mood.</p>`;
    const t = tracks[at];
    document.querySelector(".now").style.display = t ? "" : "none";
    if (!t) return;
    document.getElementById("nowTitle").textContent = t.title;
    document.getElementById("nowMeta").innerHTML = `${esc(t.uploader || "")}${t.duration ? " · " + clock(t.duration) : ""} · <a href="https://www.youtube.com/watch?v=${esc(t.vid)}" target="_blank" rel="noopener"><u>YouTube</u></a>`;
    document.getElementById("whyBox").style.display = t.why.length ? "" : "none";
    document.getElementById("why").innerHTML = t.why.map((w) => `<li>${esc(w)}</li>`).join("");
  }

  function play(index, autoplay) {
    if (!listen || !listen.tracks.length) { drawQueue(); stopPlayer(); return; }
    listen.at = ((index % listen.tracks.length) + listen.tracks.length) % listen.tracks.length;
    drawQueue();
    const vid = listen.tracks[listen.at].vid;
    if (player && typeof player.loadVideoById === "function") autoplay ? player.loadVideoById(vid) : player.cueVideoById(vid);
  }

  async function judge(verdict) {
    const t = listen && listen.tracks[listen.at];
    if (!t) return;
    const note = document.getElementById("note"), folder = document.getElementById("folder").value;
    try {
      await api("verdict", { method: "POST", body: { vid: t.vid, verdict, mood: listen.mood.id, folder } });
    } catch (err) { note.innerHTML = `<span class="error">${esc(err.message)}</span>`; return; }
    note.innerHTML = verdict === "keep" ? `<span class="ok">Kept “${esc(t.title)}” in ${esc(folder)}.</span>` : `Skipped “${esc(t.title)}”.`;
    listen.tracks.splice(listen.at, 1);
    listen.errors = 0;
    document.querySelector(".chip.on b").textContent = listen.tracks.length || "";
    play(listen.at, true);
    refreshStatus();
  }

  // ---------- player ----------
  function loadYouTubeApi() {
    if (window.YT && window.YT.Player) return Promise.resolve();
    return new Promise((resolve) => {
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => { prev && prev(); resolve(); };
      if (!document.getElementById("ytapi")) {
        const s = document.createElement("script");
        s.id = "ytapi"; s.src = "https://www.youtube.com/iframe_api";
        document.head.appendChild(s);
      }
    });
  }

  function stopPlayer() {
    document.getElementById("audio")?.pause();  // an <audio> taken off the page would keep playing
    if (player) { try { player.destroy(); } catch { /* frame already gone */ } player = null; }
  }

  async function startPlayer(mine) {
    await loadYouTubeApi();
    if (mine !== renderId || !document.getElementById("frame") || !listen.tracks.length) return;
    player = new YT.Player("frame", {
      host: "https://www.youtube-nocookie.com", videoId: listen.tracks[listen.at].vid,
      playerVars: { rel: 0, playsinline: 1, origin: location.origin },
      events: {
        onStateChange: (e) => {
          if (e.data === YT.PlayerState.PLAYING) listen.errors = 0;
          if (e.data === YT.PlayerState.ENDED) play(listen.at + 1, true);  // unjudged tracks stay in the queue
        },
        onError: () => {  // some uploads refuse to play outside youtube.com
          const note = document.getElementById("note");
          if (note) note.textContent = "That one cannot be played here. Use the YouTube link to hear it.";
          if (++listen.errors < 3) play(listen.at + 1, true);
        },
      },
    });
  }

  // ---------- Library: your own music, played from the files ----------
  async function libraryPage(playlistArg) {
    const mine = renderId;
    refreshStatus();
    const wanted = playlistArg || prefs.playlist;
    const data = await api("library" + (wanted ? "?playlist=" + encodeURIComponent(wanted) : ""));
    if (mine !== renderId) return;
    if (!data.playlists.length) { view.innerHTML = `<p class="empty">No library found. Set beets_db in tunes.json.</p>`; return; }
    setPref("playlist", data.playlist);
    const tracks = data.tracks.filter((t) => t.playable);
    const total = data.playlists.reduce((n, p) => n + p.tracks, 0);
    const chips = `<div class="chips">${[{ name: "*", label: "All", tracks: total }, ...data.playlists].map((p) =>
      `<a class="chip${p.name === data.playlist ? " on" : ""}" href="#/library/${encodeURIComponent(p.name)}">${esc(p.label || p.name)}<b>${p.tracks}</b></a>`).join("")}</div>`;
    if (!tracks.length) {
      view.innerHTML = chips + `<p class="empty">${data.tracks.length} tracks here, but their files are not on this machine. Set library_dir in tunes.json.</p>`;
      return;
    }
    lib = { tracks, order: [], pos: 0, shuffle: prefs.shuffle !== false, errors: 0 };
    view.innerHTML = chips + `<div class="listen">
        <section class="now deck" aria-label="Now playing">
          <div class="glow" id="libGlow" aria-hidden="true"></div>
          <div class="cover"><img id="libCover" alt=""></div>
          <h1 id="libTitle"></h1><div class="meta" id="libArtist"></div>
          <div class="seek"><span id="curTime">0:00</span>
            <input type="range" id="seek" min="0" max="1000" value="0" aria-label="Position"><span id="durTime"></span></div>
          <div class="transport">
            <button class="round" id="shuffleBtn" title="Shuffle" aria-label="Shuffle">${ICON.shuffle}</button>
            <button class="round" id="prevBtn" title="Previous (P)" aria-label="Previous">${ICON.prev}</button>
            <button class="round big" id="playBtn" title="Play or pause (Space)" aria-label="Play">${ICON.play}</button>
            <button class="round" id="nextBtn" title="Next (N)" aria-label="Next">${ICON.next}</button>
            <a class="round" id="saveBtn" title="Save to this device" aria-label="Save to this device">${ICON.save}</a>
          </div>
          <div class="meta" id="note" role="status"></div>
          <audio id="audio" preload="metadata"></audio>
        </section>
        <section aria-label="Tracks">
          <div class="queue-head"><h2>${tracks.length} tracks</h2>
            <input class="filter" type="search" id="libFilter" placeholder="Filter" autocomplete="off" aria-label="Filter tracks"></div>
          <div id="libList">${tracks.map((t, i) => `<button class="track square" data-i="${i}">
            <img loading="lazy" alt="" src="${art(t.id)}">
            <span><strong>${esc(t.title)}</strong><span class="meta">${esc(t.artist || "")}</span></span>
            <span class="meta">${clock(t.length)}</span></button>`).join("")}</div>
        </section>
      </div>`;
    // A track without an embedded cover keeps an empty tile instead of a broken picture.
    view.addEventListener("error", (e) => { if (e.target.tagName === "IMG") e.target.style.visibility = "hidden"; }, true);
    const seek = document.getElementById("seek"), playBtn = document.getElementById("playBtn");
    let dragging = false;
    const showTime = (sec) => {
      const a = document.getElementById("audio");
      document.getElementById("curTime").textContent = clock(sec) || "0:00";
      const part = a.duration ? sec / a.duration : 0;
      seek.value = Math.round(part * 1000);
      seek.style.setProperty("--p", part * 100 + "%");
    };
    seek.addEventListener("input", () => { dragging = true; const a = document.getElementById("audio"); showTime((seek.value / 1000) * (a.duration || 0)); });
    seek.addEventListener("change", () => { const a = document.getElementById("audio"); if (a.duration) a.currentTime = (seek.value / 1000) * a.duration; dragging = false; });
    playBtn.addEventListener("click", libToggle);
    const audio = document.getElementById("audio");
    audio.addEventListener("playing", () => { lib.errors = 0; });
    audio.addEventListener("timeupdate", () => { if (!dragging) showTime(audio.currentTime); });
    audio.addEventListener("durationchange", () => { document.getElementById("durTime").textContent = clock(audio.duration); });
    for (const ev of ["play", "pause"]) audio.addEventListener(ev, () => {
      playBtn.innerHTML = audio.paused ? ICON.play : ICON.pause;
      playBtn.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
    });
    audio.addEventListener("ended", () => libPlay(lib.pos + 1, true));
    audio.addEventListener("error", () => {
      document.getElementById("note").innerHTML = `<span class="error">Could not play “${esc(libTrack().title)}”.</span>`;
      if (++lib.errors < 3) libPlay(lib.pos + 1, true);
    });
    document.getElementById("prevBtn").addEventListener("click", () => libPlay(lib.pos - 1, true));
    document.getElementById("nextBtn").addEventListener("click", () => libPlay(lib.pos + 1, true));
    document.getElementById("shuffleBtn").addEventListener("click", () => {
      lib.shuffle = !lib.shuffle;
      setPref("shuffle", lib.shuffle);
      libOrder(lib.order[lib.pos]);
    });
    document.getElementById("libList").addEventListener("click", (e) => {
      const row = e.target.closest(".track");
      if (row) libPlay(lib.order.indexOf(Number(row.dataset.i)), true);
    });
    document.getElementById("libFilter").addEventListener("input", (e) => {
      const needle = e.target.value.trim().toLowerCase();
      document.querySelectorAll("#libList .track").forEach((row) => {
        const t = tracks[row.dataset.i];
        row.hidden = !!needle && !(t.title + " " + (t.artist || "")).toLowerCase().includes(needle);
      });
    });
    if ("mediaSession" in navigator) {  // headphone and keyboard media keys
      navigator.mediaSession.setActionHandler("nexttrack", () => lib && libPlay(lib.pos + 1, true));
      navigator.mediaSession.setActionHandler("previoustrack", () => lib && libPlay(lib.pos - 1, true));
    }
    libOrder();
    libPlay(0, false);
  }

  const libTrack = () => lib.tracks[lib.order[lib.pos]];
  function libToggle() { const a = document.getElementById("audio"); a.paused ? a.play().catch(() => {}) : a.pause(); }

  // The play order: the list as shown, or a fresh shuffle. `current` (a track index) keeps its place as "now".
  function libOrder(current) {
    const order = lib.tracks.map((_, i) => i);
    if (lib.shuffle) for (let i = order.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [order[i], order[j]] = [order[j], order[i]];
    }
    lib.order = order;
    if (current != null) lib.pos = order.indexOf(current);
    const btn = document.getElementById("shuffleBtn");
    btn.classList.toggle("on", lib.shuffle);
    btn.setAttribute("aria-pressed", String(lib.shuffle));
  }

  function libPlay(pos, autoplay) {
    const n = lib.tracks.length;
    lib.pos = ((pos % n) + n) % n;
    const t = libTrack(), audio = document.getElementById("audio");
    audio.src = `/media/${t.id}?token=${encodeURIComponent(store.get(TOKEN_KEY))}`;
    document.getElementById("saveBtn").href = audio.src + "&save";
    if (autoplay) audio.play().catch(() => { /* the browser wants a click first */ });
    document.getElementById("libTitle").textContent = t.title;
    document.getElementById("libArtist").textContent = t.artist || "";
    document.getElementById("durTime").textContent = clock(t.length);
    document.getElementById("curTime").textContent = "0:00";
    const seek = document.getElementById("seek");
    seek.value = 0; seek.style.setProperty("--p", "0%");
    const cover = document.getElementById("libCover");
    cover.style.visibility = ""; cover.src = art(t.id);
    document.getElementById("libGlow").style.backgroundImage = `url("${art(t.id)}")`;
    document.querySelector("#libList .track.on")?.classList.remove("on");
    const row = document.querySelector(`#libList .track[data-i="${lib.order[lib.pos]}"]`);
    row.classList.add("on");
    if (autoplay && !row.hidden) row.scrollIntoView({ block: "nearest" });
    if ("mediaSession" in navigator) navigator.mediaSession.metadata = new MediaMetadata({ title: t.title, artist: t.artist || "", artwork: [{ src: art(t.id) }] });
  }

  // ---------- Moods ----------
  async function moodsPage() {
    const mine = renderId;
    const [{ moods }, status] = await Promise.all([api("moods"), refreshStatus()]);
    if (mine !== renderId) return;
    const folders = status ? status.folders : [];
    view.innerHTML = `<h1>Moods</h1><p class="sub">A mood starts from seeds: a library playlist, pasted YouTube links, and what you keep in it. A round asks YouTube what plays next to them.</p>
      <div class="box rows">${moods.map((m) => `<div class="row"><div class="grow"><a href="#/moods/${m.id}"><strong>${esc(m.name)}</strong></a>
        <div class="meta">${[m.playlist ? "playlist " + esc(m.playlist) : "", m.seeds ? m.seeds + " pasted seeds" : "", m.kept + " kept", m.waiting + " waiting",
          m.round ? "round running" : m.generated_at ? "last round " + ago(m.generated_at) : "no round yet"].filter(Boolean).join(" · ")}${
          m.error ? ` · <span class="error">${esc(m.error)}</span>` : ""}</div></div>
        <button class="btn small" data-round="${m.id}"${m.round ? " disabled" : ""}>New round</button>
        <a class="btn small" href="#/listen/${m.id}">Listen</a></div>`).join("") || '<p class="empty">No moods yet.</p>'}</div>
      <h2>New mood</h2>
      <form class="form box" id="addForm" style="margin-top:12px">
        <div class="line"><input id="moodName" placeholder="Name, e.g. sunday jazz" maxlength="40" autocomplete="off" aria-label="Mood name"></div>
        <div class="line"><label class="meta" for="moodPlaylist">Seed from playlist</label>
          <select id="moodPlaylist"><option value="">None</option>${options(folders)}</select>
          <label class="meta" for="moodFolder">Keep into</label><select id="moodFolder">${options(folders, "misc")}</select></div>
        <textarea id="moodSeeds" placeholder="YouTube links of tracks that define the mood, one per line (optional when a playlist is chosen)" aria-label="Seed links"></textarea>
        <div class="line"><button class="btn primary" id="addBtn">Add mood</button></div>
      </form>`;
    view.querySelectorAll("[data-round]").forEach((btn) => btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "Started";
      try { await api(`moods/${btn.dataset.round}/generate`, { method: "POST" }); } catch (err) { fail(err); }
    }));
    document.getElementById("addForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const made = await api("moods", { method: "POST", body: { name: document.getElementById("moodName").value,
          playlist: document.getElementById("moodPlaylist").value, folder: document.getElementById("moodFolder").value,
          seeds: document.getElementById("moodSeeds").value } });
        await api(`moods/${made.id}/generate`, { method: "POST" });
        location.hash = "#/listen/" + made.id;
      } catch (err) { fail(err); }
    });
  }

  async function moodPage(id) {
    const mine = renderId;
    const draw = (m) => {
      if (mine !== renderId) return;
      view.innerHTML = `<h1>${esc(m.name)}</h1><p class="sub">${m.playlist ? "Seeded from the playlist " + esc(m.playlist) + ", plus" : "Seeded from"} the links below and what you keep in this mood. Keeps go to ${esc(m.folder)}.</p>
        <form class="form" id="seedForm"><textarea id="seedText" placeholder="YouTube links or video ids, one per line" aria-label="Seed links"></textarea>
          <div class="line"><button class="btn primary">Add seeds</button><a class="btn" href="#/listen/${m.id}">Listen</a>
          <div class="grow"></div><button type="button" class="btn danger" id="removeMood">Remove mood</button></div></form>
        <div class="box rows">${m.seeds.map((s) => `<div class="row"><img loading="lazy" alt="" src="${thumb(s.vid)}">
          <div class="grow"><a href="https://www.youtube.com/watch?v=${esc(s.vid)}" target="_blank" rel="noopener"><strong>${esc(s.title || s.vid)}</strong></a></div>
          <button class="btn small danger" data-seed="${esc(s.vid)}">Remove</button></div>`).join("") || '<p class="empty">No pasted seeds.</p>'}</div>`;
      document.getElementById("seedForm").addEventListener("submit", (e) => {
        e.preventDefault();
        api(`moods/${m.id}/seeds`, { method: "POST", body: { seeds: document.getElementById("seedText").value } }).then(draw).catch(fail);
      });
      view.querySelectorAll("[data-seed]").forEach((btn) => btn.addEventListener("click", () =>
        api(`moods/${m.id}/seeds/${encodeURIComponent(btn.dataset.seed)}`, { method: "DELETE" }).then(draw).catch(fail)));
      document.getElementById("removeMood").addEventListener("click", async () => {
        if (!confirm("Remove the mood “" + m.name + "”? Tracks you kept stay in the library.")) return;
        try { await api("moods/" + m.id, { method: "DELETE" }); location.hash = "#/moods"; } catch (err) { fail(err); }
      });
    };
    draw(await api("moods/" + encodeURIComponent(id)));
  }

  // ---------- Kept and Skipped ----------
  async function historyPage(kind) {
    const mine = renderId;
    view.innerHTML = `<h1>${kind === "kept" ? "Kept" : "Skipped"}</h1><p class="sub">${kind === "kept"
      ? "Tracks you sent to the library. They also seed their mood." : "Never offered again. A channel with only skips is ranked lower."}</p>
      <div class="box rows" id="rows"></div>`;
    const rows = document.getElementById("rows");
    const dl = { queued: "queued", running: "downloading", done: "downloaded", failed: "download failed" };
    async function next(offset) {
      const data = await api(`history?kind=${kind}&offset=${offset}`);
      if (mine !== renderId) return;
      view.querySelector(".more")?.remove();
      if (!data.tracks.length && !offset) { rows.innerHTML = `<p class="empty">Nothing here yet.</p>`; return; }
      rows.insertAdjacentHTML("beforeend", data.tracks.map((t) => `<div class="row"><img loading="lazy" alt="" src="${thumb(t.vid)}">
        <div class="grow"><a href="https://www.youtube.com/watch?v=${esc(t.vid)}" target="_blank" rel="noopener"><strong>${esc(t.title || t.vid)}</strong></a>
        <div class="meta">${[esc(t.uploader || ""), t.mood ? esc(t.mood) : "", kind === "kept" && t.folder ? "in " + esc(t.folder) : "",
          t.download ? `<span class="${t.download === "failed" ? "error" : ""}" title="${esc(t.download_error || "")}">${dl[t.download]}</span>` : ""].filter(Boolean).join(" · ")}</div></div>
        ${kind === "skipped" ? `<button class="btn small" data-restore="${esc(t.vid)}">Back to queue</button>` : ""}</div>`).join(""));
      if (data.next != null) {
        const btn = document.createElement("button");
        btn.className = "btn more"; btn.textContent = "Show more";
        btn.addEventListener("click", () => { btn.disabled = true; next(data.next).catch(fail); });
        view.appendChild(btn);
      }
    }
    rows.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-restore]");
      if (!btn) return;
      try { await api("verdict", { method: "POST", body: { vid: btn.dataset.restore, verdict: "restore" } }); btn.closest(".row").remove(); } catch (err) { fail(err); }
    });
    await next(0);
  }

  // ---------- routing ----------
  async function route() {
    const mine = ++renderId;
    stopPlayer();
    listen = null;
    lib = null;
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
    const section = parts[0] || "listen";
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === section));
    view.innerHTML = `<p class="empty">Loading…</p>`;
    window.scrollTo(0, 0);
    try {
      await loadPrefs();
      if (mine !== renderId) return;
      if (section === "listen") await listenPage(parts[1]);
      else if (section === "library") await libraryPage(parts[1]);
      else if (section === "moods" && parts[1]) await moodPage(parts[1]);
      else if (section === "moods") await moodsPage();
      else if (section === "kept" || section === "skipped") await historyPage(section);
      else view.innerHTML = `<p class="empty">Page not found.</p>`;
    } catch (err) {
      if (mine === renderId && err.message !== "Token required") {
        const offline = err instanceof TypeError;  // fetch could not reach the server
        view.innerHTML = `<p class="error" role="alert">${esc(offline ? "Cannot reach Tunes. It only answers on the home network." : err.message)}</p>
          <p class="empty"><button class="btn" id="retry">Try again</button></p>`;
        document.getElementById("retry").addEventListener("click", route);
      }
    }
  }

  document.addEventListener("keydown", (e) => {
    if ((!listen && !lib) || e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT|AUDIO)$/.test(document.activeElement.tagName)) return;
    const key = e.key.toLowerCase();
    if (lib) {
      if (key === "n") libPlay(lib.pos + 1, true);
      else if (key === "p") libPlay(lib.pos - 1, true);
      else if (key === " " && document.activeElement.tagName !== "BUTTON") { e.preventDefault(); libToggle(); }
      return;
    }
    if (key === "k") judge("keep");
    else if (key === "s") judge("skip");
    else if (key === "l") play(listen.at + 1, true);
  });
  window.addEventListener("hashchange", route);
  route();
  if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("sw.js").catch(() => {});
})();
