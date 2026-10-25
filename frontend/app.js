/* NITW Vehicle Console - frontend.
 *
 * This file only ever talks to the backend through:
 *   - REST  (/api/...)         for commands
 *   - WS    (/ws/telemetry)    for live vehicle state
 * It has no idea whether the vehicle is simulated or real hardware, so
 * nothing here changes when you plug in the physical vehicle.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    token: null, ws: null, reconnectTimer: null,
    map: null, vehicle: null, routeLine: null, terminals: [],
    idleSeconds: 300, idleLimitMs: 285000, lastActivity: 0, lastKeepalive: 0, idleTimer: null, idleShown: false,
    mode: "autonomous", modeLockUntil: 0, follow: true, termMarkers: {}, destId: null, prevLock: null, last: null, tween: null,
    stick: { x: 0, y: 0, timer: null, active: false },
  };

  /* ---------------- helpers ---------------- */
  async function api(path, body, method) {
    const opts = { method: method || (body !== undefined ? "POST" : "GET"), headers: {} };
    if (state.token) opts.headers["Authorization"] = "Bearer " + state.token;
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (res.status === 401 && path !== "/api/login") { signOut("Session expired. Please sign in again.", false); throw new Error("Session expired. Please sign in again."); }
    if (!res.ok) throw new Error((data && data.detail) || ("Request failed (" + res.status + ")"));
    return data;
  }

  let toastTimer;
  function toast(msg, kind) {
    const el = $("toast");
    el.textContent = msg;
    el.style.borderLeftColor = kind === "ok" ? "var(--green)" : "var(--red)";
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3500);
  }

  const fmtEta = (s) => {
    if (s == null) return "--:--";
    s = Math.max(0, Math.round(s));
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  };
  const compass = (d) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(d / 45) % 8];

  /* ---------------- sign in / out ---------------- */
  async function loadDemoHint() {
    try {
      const d = await api("/api/demo-info");
      $("loginUser").value = d.username; $("loginPass").value = d.password;
      const h = $("demoHint"); h.hidden = false;
      h.textContent = "Demo mode: credentials are pre-filled.";
    } catch (_) { /* not in demo mode */ }
  }

  $("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("loginError").textContent = "";
    try {
      const r = await api("/api/login", { username: $("loginUser").value, password: $("loginPass").value });
      state.token = r.token;
      state.idleSeconds = r.idle_timeout_s;
      state.idleLimitMs = Math.max(15, r.idle_timeout_s - 15) * 1000;  // sign out client-side a little before the server does
      startApp();
    } catch (err) { $("loginError").textContent = err.message; }
  });

  function signOut(message, tellServer) {
    if (tellServer !== false && state.token) {
      fetch("/api/logout", { method: "POST", headers: { Authorization: "Bearer " + state.token } }).catch(() => {});
    }
    state.token = null;
    if (state.ws) { state.ws.onclose = null; state.ws.close(); state.ws = null; }
    clearTimeout(state.reconnectTimer);
    clearInterval(state.idleTimer);
    clearInterval(state.stick.timer);
    hideIdleBanner();
    $("app").hidden = true; $("login").hidden = false;
    $("loginPass").value = "";
    $("loginError").textContent = message || "";
  }
  $("signOutBtn").addEventListener("click", () => signOut("Signed out."));

  /* ---------------- inactivity timeout ----------------
   * Any input (tap, click, key, scroll, map drag) counts as activity and is
   * reported to the server (throttled) so its own idle clock stays in step.
   * With no input for the limit, the page signs out and drops the live feed. */
  const IDLE_WARN_MS = 30000;
  function showIdleBanner(sec) {
    $("idleText").textContent = "No input detected. Signing out in " + sec + " s.";
    if (!state.idleShown) { $("idleBanner").hidden = false; state.idleShown = true; }
  }
  function hideIdleBanner() {
    if (state.idleShown) { $("idleBanner").hidden = true; state.idleShown = false; }
  }
  function markActive() {
    if (!state.token) return;
    const now = Date.now();
    state.lastActivity = now;
    hideIdleBanner();
    if (now - state.lastKeepalive > 10000) {
      state.lastKeepalive = now;
      api("/api/keepalive", {}).catch(() => {});
    }
  }
  ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"].forEach((ev) =>
    window.addEventListener(ev, markActive, { passive: true }));
  $("stayBtn").addEventListener("click", markActive);

  function startIdleWatch() {
    state.lastActivity = state.lastKeepalive = Date.now();
    clearInterval(state.idleTimer);
    state.idleTimer = setInterval(() => {
      if (!state.token) return;
      const left = state.idleLimitMs - (Date.now() - state.lastActivity);
      if (left <= 0) {
        const mins = Math.max(1, Math.round(state.idleSeconds / 60));
        signOut("Signed out after " + mins + " minute" + (mins > 1 ? "s" : "") + " without input.");
      } else if (left <= IDLE_WARN_MS) showIdleBanner(Math.ceil(left / 1000));
    }, 1000);
  }

  /* ---------------- map ---------------- */
  function vehicleIcon() {
    return L.divIcon({
      className: "vehicle-icon", iconSize: [34, 34], iconAnchor: [17, 17],
      html: '<div class="vehicle-arrow" id="vArrow"><svg viewBox="0 0 24 24"><path d="M12 2 L20 21 L12 17 L4 21 Z" fill="#3dd6c6" stroke="#04201d" stroke-width="1.2" stroke-linejoin="round"/></svg></div>',
    });
  }

  /* Free tile sources that need no API key. Streets/Satellite/Terrain cover the
   * "Google Maps style" views. (Google's own tiles can't be used directly:
   * that needs a Maps JavaScript API key with billing enabled.) */
  function buildBaseLayers() {
    const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 20, maxNativeZoom: 19, attribution: "&copy; OpenStreetMap contributors",
    });
    const satellite = L.layerGroup([
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 20, maxNativeZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
      }),
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 20, maxNativeZoom: 19,
      }),
    ]);
    const terrain = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
      subdomains: "abc", maxZoom: 20, maxNativeZoom: 17,
      attribution: "&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap (CC-BY-SA)",
    });
    return { Streets: streets, Satellite: satellite, Terrain: terrain };
  }

  const coarse = window.matchMedia && matchMedia("(pointer: coarse)").matches;
  const TERM_STYLE = { radius: coarse ? 9 : 6, color: "#0e1319", weight: 2.5, fillColor: "#ffffff", fillOpacity: 1 };
  const TERM_SELECTED = { radius: coarse ? 12 : 9, fillColor: "#3dd6c6" };

  function selectDest(id) {
    if (state.destId != null && state.termMarkers[state.destId]) state.termMarkers[state.destId].setStyle(TERM_STYLE);
    state.destId = id;
    const m = state.termMarkers[id];
    if (m) { m.setStyle(Object.assign({}, TERM_STYLE, TERM_SELECTED)); m.bringToFront(); }
    declutterLabels();
  }

  /* Landmark names are always on. Where two names would physically overlap at
   * the current zoom, the lower-priority one is faded out until you zoom in. */
  function declutterLabels() {
    requestAnimationFrame(() => {
      const placed = [];
      const ids = Object.keys(state.termMarkers).sort((a, b) => (+b === state.destId) - (+a === state.destId));
      ids.forEach((id) => {
        const tip = state.termMarkers[id].getTooltip();
        const el = tip && tip.getElement();
        if (!el) return;
        el.classList.remove("lm-hidden");
        const r = el.getBoundingClientRect();
        const hit = placed.some((p) => !(r.right < p.left - 2 || r.left > p.right + 2 || r.bottom < p.top - 1 || r.top > p.bottom + 1));
        if (hit) el.classList.add("lm-hidden"); else placed.push(r);
      });
    });
  }

  async function initMap() {
    if (state.map) return;
    state.map = L.map("map", { zoomControl: true, maxZoom: 20 }).setView([17.9840, 79.5310], 16);
    const bases = buildBaseLayers();
    const saved = localStorage.getItem("nitw.base");
    (bases[saved] || bases.Streets).addTo(state.map);
    L.control.layers(bases, null, { position: "topleft", collapsed: true }).addTo(state.map);
    state.map.on("baselayerchange", (e) => localStorage.setItem("nitw.base", e.name));
    state.map.on("dragstart", () => setFollow(false));
    state.map.on("zoomend moveend", declutterLabels);

    const net = await api("/api/network");
    const bounds = [];
    // White casing under a blue line keeps the paths readable on streets, satellite and terrain.
    net.edges.forEach(([a, b]) => {
      const pts = [net.nodes[a], net.nodes[b]];
      L.polyline(pts, { color: "#ffffff", weight: 8, opacity: 0.85, lineCap: "round" }).addTo(state.map);
      L.polyline(pts, { color: "#1f5fa8", weight: 4, opacity: 1, lineCap: "round" }).addTo(state.map);
    });
    net.nodes.forEach((n) => bounds.push(n));
    state.terminals = net.terminals;

    const sel = $("destSelect");
    sel.innerHTML = "";
    state.termMarkers = {};
    net.terminals.forEach((t) => {
      const o = document.createElement("option");
      o.value = t.id; o.textContent = t.name; sel.appendChild(o);
      state.termMarkers[t.id] = L.circleMarker([t.lat, t.lon], TERM_STYLE)
        .bindTooltip(t.name, { permanent: true, direction: "right", offset: [9, 0], className: "lm-label" })
        .on("click", () => { sel.value = t.id; selectDest(t.id); })
        .addTo(state.map);
    });
    sel.addEventListener("change", () => selectDest(parseInt(sel.value, 10)));
    selectDest(parseInt(sel.value, 10));
    state.map.fitBounds(bounds, { padding: [30, 30] });
  }

  function setFollow(on) {
    state.follow = on;
    const b = $("followBtn");
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.textContent = on ? "Following vehicle" : "Follow vehicle";
  }
  $("followBtn").addEventListener("click", () => {
    setFollow(!state.follow);
    if (state.follow && state.last) state.map.panTo([state.last.lat, state.last.lon]);
  });

  function drawRoute(route) {
    clearRoute();
    const casing = L.polyline(route, { color: "#04201d", weight: 10, opacity: 0.6, lineCap: "round" });
    const dashes = L.polyline(route, { color: "#3dd6c6", weight: 5, opacity: 1, dashArray: "1 9", lineCap: "round" });
    state.routeLine = L.layerGroup([casing, dashes]).addTo(state.map);
  }
  function clearRoute() {
    if (state.routeLine) { state.map.removeLayer(state.routeLine); state.routeLine = null; }
    $("routeInfo").textContent = "";
  }

  // Smoothly slide the marker between telemetry fixes instead of jumping.
  function moveVehicle(lat, lon, heading) {
    if (!state.vehicle) {
      state.vehicle = L.marker([lat, lon], { icon: vehicleIcon(), zIndexOffset: 1000 }).addTo(state.map);
      state.map.setView([lat, lon], 17);
    }
    const arrow = document.getElementById("vArrow");
    if (arrow) arrow.style.transform = "rotate(" + heading + "deg)";
    const from = state.vehicle.getLatLng(), t0 = performance.now(), dur = 450;
    cancelAnimationFrame(state.tween);
    const step = (now) => {
      const k = Math.min(1, (now - t0) / dur);
      const p = [from.lat + (lat - from.lat) * k, from.lng + (lon - from.lng) * k];
      state.vehicle.setLatLng(p);
      if (state.follow) state.map.panTo(p, { animate: false });
      if (k < 1) state.tween = requestAnimationFrame(step);
    };
    state.tween = requestAnimationFrame(step);
  }

  /* ---------------- telemetry ---------------- */
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    const ws = new WebSocket(proto + location.host + "/ws/telemetry?token=" + encodeURIComponent(state.token));
    state.ws = ws;
    ws.onopen = () => setLink("up", "Live");
    ws.onmessage = (e) => onTelemetry(JSON.parse(e.data));
    ws.onclose = (e) => {
      if (e.code === 4401) { signOut("Signed out after inactivity.", false); return; }
      setLink("down", "Disconnected, retrying");
      if (state.token) state.reconnectTimer = setTimeout(connectWS, 2000);
    };
  }
  function setLink(cls, text) {
    $("linkStatus").className = "link " + cls;
    $("linkText").textContent = text;
  }

  function onTelemetry(t) {
    const prev = state.last;
    state.last = t;
    moveVehicle(t.lat, t.lon, t.heading_deg);

    $("tSpeed").textContent = t.speed_kmph.toFixed(1);
    $("tEta").textContent = fmtEta(t.eta_seconds);
    $("tDist").textContent = t.distance_remaining_m != null ? Math.round(t.distance_remaining_m) + " m left" : "";
    $("tHeading").textContent = Math.round(t.heading_deg);
    $("tCompass").textContent = compass(t.heading_deg);
    $("tBatt").textContent = Math.round(t.battery_pct);
    const bar = $("battBar");
    bar.style.width = t.battery_pct + "%";
    bar.className = t.battery_pct < 20 ? "low" : t.battery_pct < 45 ? "mid" : "";
    $("tPos").textContent = t.lat.toFixed(5) + ", " + t.lon.toFixed(5);
    $("tLink").textContent = "#" + t.seq + (t.link_latency_ms != null ? "  " + Math.round(t.link_latency_ms) + " ms" : "");

    if (t.mode !== state.mode && Date.now() > state.modeLockUntil) applyMode(t.mode);   // stay in sync if another client changed it
    updateLock(t);

    if (t.arrived_event) toast("Arrived at " + (t.destination_name || "destination"), "ok");
    if (prev && prev.destination_name && !t.destination_name) clearRoute();
  }

  /* ---------------- modes ---------------- */
  const MODE_NOTES = {
    autonomous: "The vehicle drives itself along the campus paths.",
    calibration: "Automatic driving is disabled. Use the joystick to check steering and throttle response.",
    manual: "Automatic driving is disabled. You are driving the vehicle directly.",
  };

  function applyMode(mode) {
    state.mode = mode;
    document.querySelectorAll(".seg").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    const auto = mode === "autonomous";
    $("autoBlock").hidden = !auto;
    $("manualBlock").hidden = auto;
    $("manualTitle").textContent = mode === "calibration" ? "Calibration joystick" : "Manual joystick";
    $("modeNote").textContent = MODE_NOTES[mode];
    $("navBtn").disabled = !auto;
    if (!auto) clearRoute();
    resetStick();
  }

  document.querySelectorAll(".seg").forEach((b) => b.addEventListener("click", async () => {
    try {
      await api("/api/mode", { mode: b.dataset.mode });
      state.modeLockUntil = Date.now() + 1500;  // ignore in-flight packets from before the switch
      applyMode(b.dataset.mode);
    } catch (err) { toast(err.message); }
  }));

  /* ---------------- navigate / e-stop ---------------- */
  $("navBtn").addEventListener("click", async () => {
    const id = parseInt($("destSelect").value, 10);
    $("navBtn").disabled = true;
    try {
      const r = await api("/api/navigate", { destination_id: id });
      drawRoute(r.route);
      $("routeInfo").textContent = r.destination_name + ": " + Math.round(r.distance_m) + " m, about " + fmtEta(r.eta_seconds) + " at cruising speed.";
      setFollow(true);
    } catch (err) { toast(err.message); }
    finally { $("navBtn").disabled = state.mode !== "autonomous"; }
  });

  $("estopBtn").addEventListener("click", async () => {
    try { await api("/api/estop", {}); clearRoute(); toast("Emergency stop sent", "ok"); }
    catch (err) { toast(err.message); }
  });

  /* ---------------- joystick ---------------- */
  const STICK_R = 47; // px the knob may travel from centre
  const stickEl = $("stick"), knob = $("knob");

  function setKnob(dx, dy) {
    knob.style.transform = "translate(" + dx + "px," + dy + "px)";
    state.stick.x = +(dx / STICK_R).toFixed(2);
    state.stick.y = +(-dy / STICK_R).toFixed(2);
    $("stickX").textContent = state.stick.x.toFixed(2);
    $("stickY").textContent = state.stick.y.toFixed(2);
  }
  function resetStick() { setKnob(0, 0); }

  async function sendStick() {
    if (state.mode === "autonomous") return;
    try { await api("/api/joystick", { x: state.stick.x, y: state.stick.y }); }
    catch (err) { toast(err.message); }
  }

  function dragTo(ev) {
    const r = stickEl.getBoundingClientRect();
    let dx = ev.clientX - (r.left + r.width / 2), dy = ev.clientY - (r.top + r.height / 2);
    const m = Math.hypot(dx, dy);
    if (m > STICK_R) { dx = dx / m * STICK_R; dy = dy / m * STICK_R; }
    setKnob(dx, dy);
  }
  stickEl.addEventListener("pointerdown", (ev) => {
    stickEl.setPointerCapture(ev.pointerId);
    state.stick.active = true;
    dragTo(ev);
    clearInterval(state.stick.timer);
    state.stick.timer = setInterval(sendStick, 150);
    sendStick();
  });
  stickEl.addEventListener("pointermove", (ev) => { if (state.stick.active) dragTo(ev); });
  const release = () => {
    if (!state.stick.active) return;
    state.stick.active = false;
    clearInterval(state.stick.timer);
    resetStick();
    sendStick(); // always send a neutral stick so the vehicle stops
  };
  stickEl.addEventListener("pointerup", release);
  stickEl.addEventListener("pointercancel", release);

  /* ---------------- lock / OTP ---------------- */
  const LOCK_TEXT = { locked: "Locked", awaiting_otp: "Awaiting code", unlocked: "Unlocked" };
  function updateLock(t) {
    const ls = $("lockState");
    ls.dataset.state = t.lock_state;
    ls.textContent = LOCK_TEXT[t.lock_state];
    const waiting = t.lock_state === "awaiting_otp";
    if (waiting && state.prevLock !== "awaiting_otp") $("lockBlock").scrollIntoView({ behavior: "smooth", block: "nearest" });
    state.prevLock = t.lock_state;
    $("otpBox").hidden = !waiting;
    $("smsBox").hidden = !(waiting && t.otp_debug);
    if (waiting && t.otp_debug) $("smsText").textContent = "Your NITW vehicle unlock code is " + t.otp_debug + ".";
    if (!waiting) { $("otpMsg").textContent = ""; $("otpMsg").className = "small"; }
  }

  $("otpForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("otpMsg");
    try {
      const r = await api("/api/otp/verify", { code: $("otpInput").value });
      msg.textContent = r.unlocked ? "Code accepted. Unlocking." : r.message;
      msg.className = "small " + (r.unlocked ? "ok" : "bad");
      if (r.unlocked) { $("otpInput").value = ""; toast("Lock opened", "ok"); }
    } catch (err) { toast(err.message); }
  });

  $("otpResend").addEventListener("click", async () => {
    try { await api("/api/otp/request", {}); toast("New code sent", "ok"); }
    catch (err) { toast(err.message); }
  });

  /* ---------------- boot ---------------- */
  async function startApp() {
    $("login").hidden = true; $("app").hidden = false;
    try { await initMap(); } catch (err) { toast(err.message); return; }
    state.map.invalidateSize();
    applyMode("autonomous");
    connectWS();
    startIdleWatch();
  }

  loadDemoHint();
})();
