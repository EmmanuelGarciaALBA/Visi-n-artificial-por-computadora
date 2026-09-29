// VisionServer - interfaz web
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

let cfg = null;
let me = null;
let selectedTool = null;
let selectedZone = null;
let camSize = null;
let safetyState = null;
const history = [];

// Parámetros editables por tipo de herramienta. `options` convierte el campo en lista desplegable.
const TOOL_PARAMS = {
  brightness: { label: "Brillo promedio", params: {} },
  color_area: { label: "Área de color (%)", params: { h_min: 0, h_max: 10, s_min: 100, s_max: 255, v_min: 70, v_max: 255 } },
  blob_count: { label: "Conteo de objetos", params: { threshold: 0, invert: false, min_area: 500, max_area: 200000 } },
  person_detect: { label: "Detección de personas (YOLO)", params: { conf: 0.5, model: "yolo11n.pt" }, min: 0, max: 0 },
  yolo_detect: { label: "Detección de objetos (YOLO)", params: { class: "", conf: 0.5, model: "yolo11n.pt" }, min: 1, max: 99 },
  code_reader: {
    label: "Lector QR / DataMatrix / barras",
    params: { symbology: "todos", expected: "" },
    options: { symbology: ["todos", "qr", "datamatrix", "barras"] },
    min: 1, max: 1,
  },
};
const PARAM_LABELS = {
  conf: "Confianza", model: "Modelo", class: "Clases (coma)", symbology: "Tipo de código",
  expected: "Texto esperado (regex)", threshold: "Umbral (0=auto)", invert: "Invertir",
  min_area: "Área mín", max_area: "Área máx",
};

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const isAdmin = () => me?.role === "admin";

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 3000);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) {
    location.href = "/login";
    throw new Error("sesión expirada");
  }
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).error || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

// ------------------------------------------------------------ pestañas
$$(".tab").forEach((b) =>
  b.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab-page").forEach((p) => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
    activeTab = b.dataset.tab;
    setStream();
  })
);

// ------------------------------------------------------------ video en vivo
// Cada pestaña muestra la cámara en vivo. Solo se transmite el video de la pestaña visible:
// cada video abierto consume CPU del servidor.
let activeTab = "monitor";
const zoneView = () => document.querySelector('input[name="zview"]:checked').value;

function setImgSrc(img, url) {
  if (!url) {
    if (img.getAttribute("src")) img.removeAttribute("src"); // cierra la conexión MJPEG
    return;
  }
  if (img.dataset.url === url && img.getAttribute("src")) return; // ya está conectado
  img.dataset.url = url;
  img.src = `${url}&t=${Date.now()}`;
}

function setStream() {
  const view = document.querySelector('input[name="view"]:checked').value;
  const zedit = zoneView() === "edit";
  setImgSrc($("#stream"), activeTab === "monitor" && `/stream.mjpg?view=${view}`);
  setImgSrc($("#snap"), activeTab === "tools" && "/stream.mjpg?view=raw");
  setImgSrc($("#safety-stream"), activeTab === "safety" && !zedit && "/stream.mjpg?view=safety");
  setImgSrc($("#zone-snap"), activeTab === "safety" && zedit && "/stream.mjpg?view=raw");
  setImgSrc($("#cam-stream"), activeTab === "camera" && "/stream.mjpg?view=raw");
}
$$('input[name="view"]').forEach((r) => r.addEventListener("change", setStream));
$$('input[name="zview"]').forEach((r) =>
  r.addEventListener("change", () => {
    const edit = zoneView() === "edit";
    $("#safety-stream").hidden = edit;
    $("#zone-editor").hidden = !edit;
    if (!edit && drawingZone) finishDrawing();
    renderZones();
    setStream();
  })
);

// Si la conexión de video se corta (reinicio del servidor, Wi-Fi), reconectar sola.
["#stream", "#snap", "#safety-stream", "#zone-snap", "#cam-stream"].forEach((sel) =>
  $(sel).addEventListener("error", () => {
    const img = $(sel);
    if (!img.getAttribute("src")) return;
    img.removeAttribute("src");
    setTimeout(setStream, 2000);
  })
);

$("#btn-trigger").addEventListener("click", async () => {
  try { await api("/api/trigger", { method: "POST" }); }
  catch (e) { toast("Error: " + e.message); }
});

// ------------------------------------------------------------ módulos (Ejecutar / Detener)
let modules = { inspection: false, safety: false };
const pending = {}; // módulo -> true mientras espera respuesta (evita que un doble clic lo anule)

function applyModules(m) {
  modules = { ...m };
  if (cfg) {
    cfg.inspection = { ...(cfg.inspection || {}), running: m.inspection };
    cfg.safety.enabled = m.safety;
  }
  $$(".run-btn").forEach((b) => {
    const name = b.dataset.module;
    if (pending[name]) return;
    const on = m[name];
    b.disabled = false;
    b.classList.toggle("running", on);
    b.textContent = on ? "■ Detener" : "▶ Ejecutar";
  });
  $$("[data-pill]").forEach((p) => {
    const on = m[p.dataset.pill];
    p.classList.toggle("on", on);
    p.textContent = on ? "En ejecución" : "Detenido";
  });
  $$("[data-dot]").forEach((d) => d.classList.toggle("on", m[d.dataset.dot]));
  $("#btn-trigger").disabled = !m.inspection;
  $("#btn-trigger").title = m.inspection ? "" : "Presiona Ejecutar para habilitar la inspección";
  if (!m.inspection) showStoppedInspection();
}

function showStoppedInspection() {
  const tile = $("#result-tile");
  tile.classList.remove("ok", "nok");
  $("#res-pass").textContent = "DETENIDO";
  $("#res-source").textContent = "";
}

$$(".run-btn").forEach((b) =>
  b.addEventListener("click", async () => {
    const name = b.dataset.module;
    if (pending[name]) return;
    const target = !modules[name]; // lo que muestra el botón es lo que se pide
    pending[name] = true;
    b.disabled = true;
    b.textContent = target ? "Iniciando…" : "Deteniendo…";
    try {
      const m = await api(`/api/modules/${name}`, { method: "POST", body: JSON.stringify({ running: target }) });
      // pequeña pausa: un doble clic no debe apagar lo que se acaba de encender
      await new Promise((r) => setTimeout(r, 600));
      pending[name] = false;
      applyModules(m);
      toast(`${name === "inspection" ? "Inspección" : "Zonas de seguridad"}: ${m[name] ? "en ejecución" : "detenido"}`);
    } catch (e) {
      pending[name] = false;
      applyModules(modules);
      toast("Error: " + e.message);
    }
  })
);

// ------------------------------------------------------------ recetas
const recipe = () => cfg.recipes.find((r) => r.id === cfg.active_recipe) || cfg.recipes[0];
const tools = () => recipe().tools;

function renderRecipeSelect() {
  $("#recipe-select").innerHTML = cfg.recipes
    .slice()
    .sort((a, b) => a.number - b.number)
    .map((r) => `<option value="${esc(r.id)}">${r.number} · ${esc(r.name)}</option>`)
    .join("");
  $("#recipe-select").value = cfg.active_recipe;
  $("#rcp-number").value = recipe().number;
  $("#rcp-name").value = recipe().name;
}

$("#recipe-select").addEventListener("change", async (e) => {
  try {
    const r = await api("/api/recipe/select", { method: "POST", body: JSON.stringify({ key: e.target.value }) });
    applyRecipeChange(r);
    toast(`Receta ${r.number}: ${r.name}`);
  } catch (err) {
    toast("Error: " + err.message);
    renderRecipeSelect();
  }
});

function applyRecipeChange(r) {
  if (!cfg || cfg.active_recipe === r.id) return;
  cfg.active_recipe = r.id;
  selectedTool = null;
  renderRecipeSelect();
  renderTools();
  drawRois();
}

$("#rcp-number").addEventListener("change", (e) => {
  const n = Math.max(1, Math.round(+e.target.value));
  if (cfg.recipes.some((r) => r !== recipe() && r.number === n)) {
    toast(`Ya existe una receta con el número ${n}`);
    e.target.value = recipe().number;
    return;
  }
  recipe().number = n;
  renderRecipeSelect();
});
$("#rcp-name").addEventListener("change", (e) => {
  recipe().name = e.target.value.trim() || recipe().name;
  renderRecipeSelect();
});

function nextRecipeNumber() {
  return Math.max(0, ...cfg.recipes.map((r) => r.number)) + 1;
}
function newRecipe(name, toolList) {
  const r = { id: "r" + Date.now().toString(36), number: nextRecipeNumber(), name, tools: toolList };
  cfg.recipes.push(r);
  cfg.active_recipe = r.id;
  selectedTool = null;
  renderRecipeSelect();
  renderTools();
  drawRois();
  saveConfig(`Receta ${r.number} creada`);
}
$("#btn-rcp-new").addEventListener("click", () => {
  const name = prompt("Nombre de la nueva receta:", `Receta ${nextRecipeNumber()}`);
  if (name) newRecipe(name.trim(), []);
});
$("#btn-rcp-dup").addEventListener("click", () => {
  const copy = structuredClone(recipe().tools).map((t, i) => ({ ...t, id: "t" + Date.now().toString(36) + i }));
  newRecipe(`${recipe().name} (copia)`, copy);
});
$("#btn-rcp-del").addEventListener("click", () => {
  if (cfg.recipes.length <= 1) return toast("Debe existir al menos una receta");
  const r = recipe();
  if (!confirm(`¿Eliminar la receta ${r.number} "${r.name}"?`)) return;
  cfg.recipes = cfg.recipes.filter((x) => x !== r);
  cfg.active_recipe = cfg.recipes[0].id;
  selectedTool = null;
  renderRecipeSelect();
  renderTools();
  drawRois();
  saveConfig("Receta eliminada");
});

// ------------------------------------------------------------ resultados en tiempo real
function showResult(r) {
  const tile = $("#result-tile");
  tile.classList.toggle("ok", r.pass);
  tile.classList.toggle("nok", !r.pass);
  $("#res-pass").textContent = r.error ? "ERROR" : r.pass ? "OK" : "NOK";
  $("#res-counter").textContent = r.counter;
  $("#res-cycle").textContent = r.cycle_ms.toFixed(1);
  $("#res-source").textContent = r.source;
  $("#res-recipe").textContent = r.recipe ? `Receta ${r.recipe.number}: ${r.recipe.name}` : "";

  const byId = Object.fromEntries((cfg ? tools() : []).map((t) => [t.id, t]));
  $("#res-tools tbody").innerHTML = r.tools
    .map((t) => {
      const c = byId[t.id] || {};
      let val = t.error ? `<span class="nok-text" title="${esc(t.error)}">error</span>` : t.value;
      if (t.type === "code_reader" && !t.error) val = t.text ? `<span class="mono">${esc(t.text)}</span>` : "—";
      return `<tr><td>${esc(t.name)}</td><td>${val}</td><td class="hint">${c.min ?? ""} – ${c.max ?? ""}</td>
              <td><span class="dot ${t.pass ? "ok" : "nok"}"></span></td></tr>`;
    })
    .join("");

  history.push(r.pass);
  if (history.length > 100) history.shift();
  $("#history").innerHTML = history.map((p) => `<span class="${p ? "ok" : "nok"}"></span>`).join("");
  const ok = history.filter(Boolean).length;
  $("#stats").textContent = `Últimas ${history.length}: ${ok} OK · ${history.length - ok} NOK · ${((100 * ok) / history.length).toFixed(1)}% rendimiento`;
}

function showSafety(s) {
  safetyState = s;
  const alarm = $("#alarm");
  if (!s.enabled) {
    alarm.hidden = true;
    $("#safety-counts").textContent = "";
    $("#safety-summary").innerHTML = modules.safety
      ? "Iniciando… (cargando el modelo de detección)"
      : "Detenido. Presiona Ejecutar para vigilar las zonas.";
  } else {
    // Única excepción al orden por pestañas: el PELIGRO se anuncia en todas las pantallas.
    alarm.hidden = !s.danger;
    alarm.className = "alarm danger";
    const occ = s.zones.filter((z) => z.occupied).map((z) => z.name).join(", ");
    alarm.textContent = s.error ? `⚠ FALLA DEL MONITOR DE SEGURIDAD: ${s.error}` : `⛔ PERSONA EN ZONA DE PELIGRO: ${occ}`;
    $("#safety-counts").textContent = s.error ? "" : `${s.persons} persona(s) · ${s.fps} fps`;
    $("#safety-summary").innerHTML =
      `<div class="stats">Personas detectadas: <b>${s.persons}</b> · ${s.fps} fps</div>` +
      (s.zones.length
        ? s.zones.map((z) => `<div class="zone-row"><span class="dot ${z.occupied ? (z.level === "peligro" ? "nok" : "warn") : "ok"}"></span>
            ${esc(z.name)} <span class="hint">(${z.level})</span> — ${z.occupied ? `<b>OCUPADA</b> (${z.count})` : "libre"}</div>`).join("")
        : `<span class="hint">No hay zonas definidas.</span>`);
  }
  renderZoneStatus();
}

function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => setChip("#chip-ws", true);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "result") showResult(msg.data);
    else if (msg.type === "safety") showSafety(msg.data);
    else if (msg.type === "recipe") applyRecipeChange(msg.data);
    else if (msg.type === "modules") applyModules(msg.data);
  };
  ws.onclose = (ev) => {
    if (ev.code === 4401) return (location.href = "/login");
    setChip("#chip-ws", false);
    setTimeout(connectWs, 2000);
  };
}

// ------------------------------------------------------------ estado
function setChip(sel, ok, title = "") {
  const c = $(sel);
  c.classList.toggle("ok", ok === true);
  c.classList.toggle("bad", ok === false);
  c.title = title;
}

async function pollStatus() {
  try {
    const s = await api("/api/status");
    camSize = s.camera.size;
    fitAllCanvases();
    $("#cam-info").textContent = s.camera.ok
      ? `${s.camera.size ? s.camera.size.join("×") : ""} · ${s.camera.fps} fps`
      : s.camera.error || "";
    if (JSON.stringify(s.modules) !== JSON.stringify(modules)) applyModules(s.modules);
    setChip("#chip-cam", s.camera.ok, s.camera.error || `${s.camera.fps} fps`);
    setChip("#chip-opc", s.opcua.enabled ? s.opcua.running : null, s.opcua.error || s.opcua.endpoints[0]);
    setChip("#chip-tcp", s.tcp.enabled ? s.tcp.running : null, s.tcp.error || `puerto ${s.tcp.port}`);
    $("#opc-endpoints").innerHTML = s.opcua.endpoints.map((e) => `<li>${esc(e)}</li>`).join("");
    $("#web-urls").innerHTML = s.web.map((u) => `<li><a href="${esc(u)}" style="color:inherit">${esc(u)}</a></li>`).join("");
    $("#tcp-clients").textContent = s.tcp.clients;
    if (!$("#new-tool-type").options.length) {
      $("#new-tool-type").innerHTML = s.tool_types
        .map((t) => `<option value="${t}">${TOOL_PARAMS[t]?.label || t}</option>`)
        .join("");
    }
  } catch {
    ["#chip-cam", "#chip-opc", "#chip-tcp"].forEach((c) => setChip(c, false, "sin conexión"));
  }
}

// ------------------------------------------------------------ configuración
async function loadConfig() {
  cfg = await api("/api/config");
  renderRecipeSelect();
  fillCameraForm();
  fillCommsForm();
  fillSafetyForm();
  renderTools();
  renderZones();
}

async function saveConfig(msg) {
  try {
    const r = await api("/api/config", { method: "PUT", body: JSON.stringify(cfg) });
    toast(r.restart_required ? "Guardado. Reinicia el servicio para aplicar el puerto web." : msg || "Guardado");
    setTimeout(setStream, 500);
  } catch (e) {
    toast("Error al guardar: " + e.message);
  }
}

function fillCameraForm() {
  $("#cam-type").value = cfg.camera.type;
  $("#cam-source").value = cfg.camera.source;
  $("#cam-user").value = cfg.camera.user || "";
  $("#cam-password").value = cfg.camera.password || "";
  $("#cam-width").value = cfg.camera.width;
  $("#cam-height").value = cfg.camera.height;
  $("#trg-mode").value = cfg.trigger.mode;
  $("#trg-interval").value = cfg.trigger.interval_ms;
}

$("#btn-save-camera").addEventListener("click", () => {
  cfg.camera = {
    type: $("#cam-type").value,
    source: $("#cam-source").value.trim(),
    user: $("#cam-user").value.trim(),
    password: $("#cam-password").value,
    width: +$("#cam-width").value,
    height: +$("#cam-height").value,
  };
  cfg.trigger = { mode: $("#trg-mode").value, interval_ms: Math.max(20, +$("#trg-interval").value) };
  saveConfig();
});

function fillCommsForm() {
  $("#opc-enabled").checked = cfg.opcua.enabled;
  $("#opc-port").value = cfg.opcua.port;
  $("#opc-ns").value = cfg.opcua.namespace;
  $("#tcp-enabled").checked = cfg.tcp.enabled;
  $("#tcp-port").value = cfg.tcp.port;
  $("#tcp-format").value = cfg.tcp.format;
  $("#tcp-sep").value = cfg.tcp.separator;
  $("#tcp-push").checked = cfg.tcp.push_results;
  $("#srv-port").value = cfg.server.port;
}

$("#btn-save-comms").addEventListener("click", () => {
  cfg.opcua = { enabled: $("#opc-enabled").checked, port: +$("#opc-port").value, namespace: $("#opc-ns").value.trim() };
  cfg.tcp = {
    enabled: $("#tcp-enabled").checked,
    port: +$("#tcp-port").value,
    format: $("#tcp-format").value,
    separator: $("#tcp-sep").value || ";",
    push_results: $("#tcp-push").checked,
  };
  cfg.server.port = +$("#srv-port").value;
  saveConfig();
});

// ------------------------------------------------------------ editor de herramientas
function paramInput(t, k, v) {
  const label = PARAM_LABELS[k] || k;
  const opts = TOOL_PARAMS[t.type]?.options?.[k];
  if (opts)
    return `<label>${label}<select data-p="${k}">${opts
      .map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`)
      .join("")}</select></label>`;
  if (typeof v === "boolean")
    return `<label class="check"><input type="checkbox" data-p="${k}" ${v ? "checked" : ""}> ${label}</label>`;
  const wide = k === "expected" || k === "class" ? ' class="wide"' : "";
  return `<label${wide}>${label}<input data-p="${k}" value="${esc(v)}" ${typeof v === "number" ? 'type="number" step="any"' : ""}></label>`;
}

function renderTools() {
  const list = $("#tool-list");
  list.innerHTML = tools().length ? "" : `<p class="hint">Esta receta no tiene herramientas.</p>`;
  tools().forEach((t, i) => {
    const div = document.createElement("div");
    div.className = "tool" + (t.id === selectedTool ? " selected" : "");
    const params = Object.entries(t.params || {}).map(([k, v]) => paramInput(t, k, v)).join("");
    div.innerHTML = `
      <div class="tool-head">
        <input type="checkbox" data-f="enabled" ${t.enabled ? "checked" : ""} title="Habilitada">
        <input type="text" data-f="name" value="${esc(t.name)}">
        <span class="tool-type">${TOOL_PARAMS[t.type]?.label || t.type}</span>
        <button class="danger admin-only" data-del title="Eliminar">✕</button>
      </div>
      <div class="tool-body">
        <label>Mín OK<input type="number" step="any" data-f="min" value="${t.min}"></label>
        <label>Máx OK<input type="number" step="any" data-f="max" value="${t.max}"></label>
        <span></span>
        ${params}
        <div class="roi">ROI: x=${t.roi[0]} y=${t.roi[1]} w=${t.roi[2]} h=${t.roi[3]}</div>
      </div>`;
    div.addEventListener("click", (e) => {
      if (e.target.closest("input,button,select")) return;
      selectedTool = t.id;
      renderTools();
      drawRois();
    });
    div.querySelectorAll("[data-f]").forEach((inp) =>
      inp.addEventListener("change", () => {
        const f = inp.dataset.f;
        t[f] = inp.type === "checkbox" ? inp.checked : inp.type === "number" ? +inp.value : inp.value;
        drawRois();
      })
    );
    div.querySelectorAll("[data-p]").forEach((inp) =>
      inp.addEventListener("change", () => {
        const p = inp.dataset.p;
        t.params[p] = inp.type === "checkbox" ? inp.checked : inp.type === "number" ? +inp.value : inp.value;
      })
    );
    div.querySelector("[data-del]").addEventListener("click", () => {
      if (!confirm(`¿Eliminar la herramienta "${t.name}"?`)) return;
      tools().splice(i, 1);
      renderTools();
      drawRois();
    });
    list.appendChild(div);
  });
}

$("#btn-add-tool").addEventListener("click", () => {
  const type = $("#new-tool-type").value;
  const def = TOOL_PARAMS[type] || {};
  const [w, h] = camSize || [cfg.camera.width, cfg.camera.height];
  const id = "t" + Date.now().toString(36);
  const full = type === "person_detect" || type === "yolo_detect";
  tools().push({
    id, name: `Herramienta${tools().length + 1}`, type, enabled: true,
    roi: full ? [0, 0, w, h] : [Math.round(w / 4), Math.round(h / 4), Math.round(w / 2), Math.round(h / 2)],
    params: structuredClone(def.params || {}), min: def.min ?? 0, max: def.max ?? 100,
  });
  selectedTool = id;
  renderTools();
  drawRois();
});

$("#btn-save-tools").addEventListener("click", () => saveConfig(`Receta ${recipe().number} guardada`));

// ------------------------------------------------------------ dibujo de ROI
const snap = $("#snap");
const canvas = $("#roi-canvas");
const ctx = canvas.getContext("2d");

// El lienzo usa la resolución real de la cámara para que las ROIs queden en píxeles de imagen.
function fitCanvas(cv, w, h, redraw) {
  if (!w || !h || (cv.width === w && cv.height === h)) return;
  cv.width = w;
  cv.height = h;
  redraw();
}
function fitAllCanvases() {
  const [w, h] = camSize || [];
  fitCanvas(canvas, w || snap.naturalWidth, h || snap.naturalHeight, drawRois);
  fitCanvas(zcanvas, w || zoneSnap.naturalWidth, h || zoneSnap.naturalHeight, drawZones);
}
snap.addEventListener("load", fitAllCanvases);

function drawRois(temp) {
  if (!cfg || !canvas.width) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const lw = Math.max(2, canvas.width / 400);
  ctx.font = `${Math.round(canvas.width / 50)}px Segoe UI`;
  for (const t of tools()) {
    if (!t.enabled) continue;
    const sel = t.id === selectedTool;
    const [x, y, w, h] = sel && temp ? temp : t.roi;
    ctx.strokeStyle = sel ? "#3d8bfd" : "#f5a524";
    ctx.lineWidth = sel ? lw * 1.5 : lw;
    ctx.setLineDash(sel ? [] : [lw * 4, lw * 3]);
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillText(t.name, x + 6, y + canvas.width / 45);
  }
}

function toImg(cv, e) {
  const r = cv.getBoundingClientRect();
  return [((e.clientX - r.left) * cv.width) / r.width, ((e.clientY - r.top) * cv.height) / r.height];
}
let dragStart = null;
canvas.addEventListener("pointerdown", (e) => {
  if (!isAdmin()) return;
  if (!selectedTool) return toast("Primero selecciona una herramienta de la lista");
  dragStart = toImg(canvas, e);
  canvas.setPointerCapture(e.pointerId);
});
function rectFrom(e) {
  const [x2, y2] = toImg(canvas, e);
  const [x1, y1] = dragStart;
  return [Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1)].map(Math.round);
}
canvas.addEventListener("pointermove", (e) => dragStart && drawRois(rectFrom(e)));
canvas.addEventListener("pointerup", (e) => {
  if (!dragStart) return;
  const r = rectFrom(e);
  dragStart = null;
  if (r[2] > 5 && r[3] > 5) {
    tools().find((t) => t.id === selectedTool).roi = r;
    renderTools();
  }
  drawRois();
});

// ------------------------------------------------------------ zonas de seguridad
const zoneSnap = $("#zone-snap");
const zcanvas = $("#zone-canvas");
const zctx = zcanvas.getContext("2d");
let drawingZone = null; // id de la zona que se está dibujando (clic = agregar punto)
let dragVertex = null; // índice del vértice que se arrastra
let hoverPt = null;

const ZONE_COLORS = { peligro: "#e5484d", advertencia: "#f5a524" };

function fillSafetyForm() {
  const s = cfg.safety;
  $("#saf-fps").value = s.fps;
  $("#saf-conf").value = s.conf;
  $("#saf-mode").value = s.mode;
  $("#saf-hold").value = s.hold_ms;
  $("#saf-model").value = s.model;
}

function readSafetyForm() {
  Object.assign(cfg.safety, {
    fps: Math.max(0.5, +$("#saf-fps").value || 5),
    conf: Math.min(0.95, Math.max(0.1, +$("#saf-conf").value || 0.5)),
    mode: $("#saf-mode").value,
    hold_ms: Math.max(0, +$("#saf-hold").value || 0),
    model: $("#saf-model").value.trim() || "yolo11n.pt",
  });
}

$("#btn-save-safety").addEventListener("click", () => {
  if (drawingZone) finishDrawing();
  const bad = cfg.safety.zones.filter((z) => z.points.length < 3).map((z) => z.name);
  if (bad.length) return toast(`Las zonas necesitan al menos 3 puntos: ${bad.join(", ")}`);
  readSafetyForm();
  saveConfig("Zonas de seguridad guardadas");
});

$("#btn-add-zone").addEventListener("click", () => {
  const id = "z" + Date.now().toString(36);
  cfg.safety.zones.push({ id, name: `Zona${cfg.safety.zones.length + 1}`, level: "peligro", enabled: true, points: [] });
  selectedZone = id;
  drawingZone = id;
  renderZones();
  drawZones();
});

function renderZones() {
  const list = $("#zone-list");
  const zones = cfg.safety.zones;
  list.innerHTML = zones.length ? "" : `<p class="hint">No hay zonas. Crea una y dibújala sobre la imagen.</p>`;
  zones.forEach((z, i) => {
    const div = document.createElement("div");
    div.className = "tool" + (z.id === selectedZone ? " selected" : "");
    div.innerHTML = `
      <div class="tool-head">
        <input type="checkbox" data-f="enabled" ${z.enabled ? "checked" : ""} title="Habilitada">
        <input type="text" data-f="name" value="${esc(z.name)}">
        <select data-f="level" class="level-select">
          <option value="peligro" ${z.level === "peligro" ? "selected" : ""}>Peligro</option>
          <option value="advertencia" ${z.level === "advertencia" ? "selected" : ""}>Advertencia</option>
        </select>
        <button class="danger admin-only" data-del title="Eliminar">✕</button>
      </div>
      <div class="zone-foot">
        <span class="hint">${z.points.length} puntos</span>
        <span class="zone-live" data-live="${esc(z.id)}"></span>
        <button class="admin-only" data-redraw>${drawingZone === z.id ? "Terminar" : "Redibujar"}</button>
      </div>`;
    div.addEventListener("click", (e) => {
      if (e.target.closest("input,button,select")) return;
      selectedZone = z.id;
      renderZones();
      drawZones();
    });
    div.querySelectorAll("[data-f]").forEach((inp) =>
      inp.addEventListener("change", () => {
        z[inp.dataset.f] = inp.type === "checkbox" ? inp.checked : inp.value;
        drawZones();
      })
    );
    div.querySelector("[data-del]").addEventListener("click", () => {
      if (!confirm(`¿Eliminar la zona "${z.name}"?`)) return;
      zones.splice(i, 1);
      if (drawingZone === z.id) drawingZone = null;
      renderZones();
      drawZones();
    });
    div.querySelector("[data-redraw]").addEventListener("click", () => {
      if (drawingZone === z.id) return finishDrawing();
      selectedZone = z.id;
      drawingZone = z.id;
      z.points = [];
      renderZones();
      drawZones();
    });
    list.appendChild(div);
  });
  $("#zone-hint").textContent = zoneView() !== "edit" ? "" : drawingZone
    ? "Haz clic para agregar puntos. Clic en el primer punto (o doble clic) para cerrar la zona."
    : "Selecciona una zona; arrastra sus vértices para ajustarla.";
  renderZoneStatus();
}

function renderZoneStatus() {
  const byId = Object.fromEntries((safetyState?.zones || []).map((z) => [z.id, z]));
  $$("[data-live]").forEach((el) => {
    const z = byId[el.dataset.live];
    el.innerHTML = !safetyState?.enabled || !z ? "" : z.occupied ? `<b class="nok-text">OCUPADA (${z.count})</b>` : `<span class="ok-text">libre</span>`;
  });
}

function finishDrawing() {
  const z = cfg.safety.zones.find((x) => x.id === drawingZone);
  drawingZone = null;
  hoverPt = null;
  if (z && z.points.length < 3) toast(`"${z.name}" necesita al menos 3 puntos`);
  renderZones();
  drawZones();
}

zoneSnap.addEventListener("load", fitAllCanvases);

function drawZones() {
  if (!cfg || !zcanvas.width) return;
  zctx.clearRect(0, 0, zcanvas.width, zcanvas.height);
  const lw = Math.max(2, zcanvas.width / 400);
  const r = lw * 3;
  zctx.font = `bold ${Math.round(zcanvas.width / 50)}px Segoe UI`;
  for (const z of cfg.safety.zones) {
    if (!z.points.length) continue;
    const sel = z.id === selectedZone;
    const drawing = drawingZone === z.id;
    const color = ZONE_COLORS[z.level] || ZONE_COLORS.peligro;
    zctx.beginPath();
    z.points.forEach(([x, y], i) => (i ? zctx.lineTo(x, y) : zctx.moveTo(x, y)));
    if (drawing && hoverPt) zctx.lineTo(...hoverPt);
    if (!drawing) zctx.closePath();
    zctx.globalAlpha = z.enabled ? 0.25 : 0.08;
    zctx.fillStyle = color;
    if (!drawing) zctx.fill();
    zctx.globalAlpha = z.enabled ? 1 : 0.4;
    zctx.strokeStyle = color;
    zctx.lineWidth = sel ? lw * 1.6 : lw;
    zctx.setLineDash(z.enabled ? [] : [lw * 4, lw * 3]);
    zctx.stroke();
    zctx.setLineDash([]);
    if (sel)
      z.points.forEach(([x, y], i) => {
        zctx.beginPath();
        zctx.arc(x, y, i === 0 && drawing ? r * 1.6 : r, 0, Math.PI * 2);
        zctx.fillStyle = "#fff";
        zctx.fill();
        zctx.stroke();
      });
    const [lx, ly] = z.points[0];
    zctx.fillStyle = color;
    zctx.fillText(z.name, lx + 8, ly - 8);
    zctx.globalAlpha = 1;
  }
}

function nearVertex(z, p, tol) {
  return z.points.findIndex(([x, y]) => Math.hypot(x - p[0], y - p[1]) < tol);
}

zcanvas.addEventListener("pointerdown", (e) => {
  if (!isAdmin()) return;
  const p = toImg(zcanvas, e).map(Math.round);
  const tol = zcanvas.width / 60;
  const z = cfg.safety.zones.find((x) => x.id === (drawingZone || selectedZone));
  if (!z) return toast("Crea o selecciona una zona primero");
  if (drawingZone) {
    if (z.points.length >= 3 && nearVertex(z, p, tol) === 0) return finishDrawing();
    // un doble clic genera dos clics en el mismo lugar: no repetir el punto
    const last = z.points[z.points.length - 1];
    if (last && Math.hypot(last[0] - p[0], last[1] - p[1]) < tol / 2) return;
    z.points.push(p);
    renderZones();
    drawZones();
    return;
  }
  const i = nearVertex(z, p, tol);
  if (i >= 0) {
    dragVertex = i;
    zcanvas.setPointerCapture(e.pointerId);
  }
});
zcanvas.addEventListener("pointermove", (e) => {
  const p = toImg(zcanvas, e).map(Math.round);
  if (dragVertex !== null) {
    cfg.safety.zones.find((x) => x.id === selectedZone).points[dragVertex] = p;
    drawZones();
  } else if (drawingZone) {
    hoverPt = p;
    drawZones();
  }
});
zcanvas.addEventListener("pointerup", () => (dragVertex = null));
zcanvas.addEventListener("dblclick", () => {
  if (drawingZone) finishDrawing();
});

// ------------------------------------------------------------ inicio
$("#btn-logout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
});

(async () => {
  me = await api("/api/me");
  $("#me-user").textContent = me.user;
  $("#me-role").textContent = me.role;
  document.body.classList.add("role-" + me.role);
  await loadConfig();
  await pollStatus();
  setInterval(pollStatus, 2000);
  setStream();
  connectWs();
})();
