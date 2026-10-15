// VisionServer - interfaz web: 4 bloques (zonas de seguridad, EPP, ANDON, stock), cámaras y sistema
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => root.querySelectorAll(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const enc = encodeURIComponent;

let cfg = null;
let me = null;
let models = [];
let camStatus = [];
let emailConfigured = false;
let wsConnected = false;
let events = [];
let activeTab = "safety";
const states = {};
const isAdmin = () => me?.role === "admin";

const LEVELS = [["ninguna", "Sin alarma"], ["advertencia", "Advertencia"], ["peligro", "Peligro"]];
const LEVEL_TEXT = { ninguna: "NORMAL", advertencia: "ADVERTENCIA", peligro: "PELIGRO" };
const PPE_ITEMS = [["helmet", "Casco"], ["gloves", "Guantes"], ["goggles", "Lentes"],
  ["vest", "Chaleco"], ["mask", "Cubrebocas"], ["shoes", "Calzado"]];
const FUNCTION_LABELS = { safety: "Zonas de seguridad", ppe: "EPP", andon: "ANDON", stock: "Stock" };

// ------------------------------------------------------------ definición de los 4 bloques
// params: parámetros de detección · alarmParams: niveles de alarma · toolFields: campos de cada herramienta
const MODULES = {
  safety: {
    title: "Zonas de seguridad", opc: "Safety", tcp: "SAFETY", shape: "poly",
    toolWord: "Zona", listTitle: "Zonas", addLabel: "+ Nueva zona",
    listHint: "Dibuja cada zona sobre el piso. Peligro = alarma roja; advertencia = amarilla.",
    note: "⚠ Función de asistencia. No es un sistema de seguridad certificado y no sustituye cortinas de luz, escáneres de seguridad ni paros de emergencia.",
    params: [
      { k: "fps", label: "Cuadros por segundo", type: "number", min: 0.5, max: 30, step: 0.5 },
      { k: "conf", label: "Confianza mínima", type: "number", min: 0.1, max: 0.95, step: 0.05 },
      { k: "mode", label: "Detección", type: "select", options: [["pies", "Pies dentro de la zona"], ["caja", "Cualquier parte del cuerpo"]] },
      { k: "hold_ms", label: "Retardo al liberar (ms)", type: "number", min: 0, step: 100 },
      { k: "model", label: "Modelo de personas", type: "model" },
    ],
    alarmParams: [],
    alarmHint: "El nivel de cada zona (peligro o advertencia) se elige en Recetas y herramientas.",
    toolFields: [{ k: "level", label: "Nivel", type: "select", options: [["peligro", "Peligro"], ["advertencia", "Advertencia"]] }],
    newTool: () => ({ level: "peligro", points: [] }),
    tableTitle: "Zonas", head: ["Zona", "Nivel", "Estado"],
    row: (t) => [esc(t.name), esc(t.zone_level),
      t.occupied ? `<b class="${t.zone_level === "peligro" ? "nok-text" : "warn-text"}">OCUPADA (${t.count})</b>` : `<span class="ok-text">libre</span>`],
    kpis: (m) => [["Personas", m.persons], ["Zonas ocupadas", m.occupied, m.occupied ? "bad" : ""]],
    big: { ninguna: "ZONAS LIBRES", advertencia: "ADVERTENCIA", peligro: "PELIGRO" },
    tcpHelp: "SAFETY → SAF;<peligro 0/1>;<advertencia 0/1>;<personas>;<zona1 0/1>;<zona2 0/1>;...",
  },
  ppe: {
    title: "EPP", opc: "PPE", tcp: "EPP", shape: "rect",
    toolWord: "Área", listTitle: "Áreas de verificación", addLabel: "+ Nueva área",
    listHint: "Opcional. Sin áreas se verifica toda la imagen con el equipo requerido de la receta.",
    note: "Modelos: epp_yolov8n.pt (equipo, licencia MIT) + yolo26n.pt (personas). Las personas más pequeñas que el alto mínimo no se evalúan. Los guantes son el equipo más difícil de ver: si hay falsas alarmas, acerca la cámara o desmarca guantes.",
    params: [
      { k: "fps", label: "Cuadros por segundo", type: "number", min: 0.2, max: 15, step: 0.2 },
      { k: "conf", label: "Confianza del equipo", type: "number", min: 0.1, max: 0.95, step: 0.05 },
      { k: "person_conf", label: "Confianza de personas", type: "number", min: 0.1, max: 0.95, step: 0.05 },
      { k: "min_person_px", label: "Alto mínimo de la persona (px)", type: "number", min: 20, step: 10 },
      { k: "confirm_ms", label: "Tiempo sin EPP para alarmar (ms)", type: "number", min: 0, step: 100 },
      { k: "hold_ms", label: "Retardo al liberar (ms)", type: "number", min: 0, step: 100 },
      { k: "model", label: "Modelo de EPP", type: "model" },
      { k: "person_model", label: "Modelo de personas", type: "model" },
    ],
    recipeFields: [{ k: "require", label: "Equipo requerido (toda la imagen y áreas nuevas)", type: "items" }],
    alarmParams: [{ k: "level", label: "Nivel de la alarma por falta de EPP", type: "select", options: LEVELS.slice(1) }],
    toolFields: [{ k: "require", label: "Equipo requerido en esta área", type: "items" }],
    newTool: (name) => ({ require: { ...(recipeOf(name).require || {}) } }),
    tableTitle: "Áreas", head: ["Área", "Personas", "Sin EPP", "Estado"],
    row: (t) => [esc(t.name), t.persons, t.violations,
      t.ok ? `<span class="ok-text">${esc(t.text)}</span>` : `<b class="nok-text">${esc(t.text)}</b>`],
    kpis: (m) => [["Personas", m.persons], ["EPP completo", m.ok, "ok"], ["Sin EPP", m.violations, m.violations ? "bad" : ""],
      ["Sin casco", m.no_helmet], ["Sin guantes", m.no_gloves], ["Sin lentes", m.no_goggles],
      ...[["Sin chaleco", m.no_vest], ["Sin cubrebocas", m.no_mask], ["Sin calzado", m.no_shoes]].filter((k) => k[1])],
    big: { ninguna: "EPP OK", advertencia: "FALTA EPP", peligro: "FALTA EPP" },
    tcpHelp: "EPP → EPP;<alarma 0/1>;<personas>;<sin_epp>;<sin_casco>;<sin_guantes>;<sin_lentes>;<area1 ok 0/1>;...",
  },
  andon: {
    title: "ANDON", opc: "Andon", tcp: "ANDON", shape: "rect",
    toolWord: "Torreta", listTitle: "Torretas", addLabel: "+ Nueva torreta",
    listHint: "Dibuja un rectángulo alrededor de cada torreta o semáforo. Sin torretas se buscan luces en toda la imagen.",
    note: "Detección por color y brillo (OpenCV, sin modelo: no pesa y casi no usa CPU). Si una luz apagada aparece encendida, sube el brillo mínimo; si una encendida no se detecta, bájalo.",
    params: [
      { k: "fps", label: "Cuadros por segundo", type: "number", min: 0.5, max: 15, step: 0.5 },
      { k: "hold_ms", label: "Retardo de intermitencia (ms)", type: "number", min: 0, step: 100 },
    ],
    alarmParams: [
      { k: "levels.red", label: "Luz roja encendida", type: "select", options: LEVELS },
      { k: "levels.yellow", label: "Luz amarilla encendida", type: "select", options: LEVELS },
      { k: "levels.green", label: "Luz verde encendida", type: "select", options: LEVELS },
      { k: "levels.off", label: "Torreta apagada", type: "select", options: LEVELS },
    ],
    toolFields: [
      { k: "v_min", label: "Brillo mín (0-255)", type: "number", min: 0, max: 255, step: 5 },
      { k: "s_min", label: "Saturación mín (0-255)", type: "number", min: 0, max: 255, step: 5 },
      { k: "min_area", label: "Área mín (px)", type: "number", min: 1, step: 5 },
    ],
    newTool: () => ({ v_min: 180, s_min: 80, min_area: 40 }),
    tableTitle: "Torretas", head: ["Torreta", "Luces", "Activaciones R/A/V", "Estado"],
    row: (t) => [esc(t.name),
      `<span class="lamps">${["red", "yellow", "green"].map((c) => `<span class="lamp ${c} ${t.lit[c] ? "on" : ""} ${t.blink[c] ? "blink" : ""}" title="${t.lamps[c]} encendida(s)"></span>`).join("")}</span>`,
      `${t.activations.red} / ${t.activations.yellow} / ${t.activations.green}`, esc(t.text)],
    kpis: (m) => [["Encendidas", m.lit], ["Rojas", m.red, "red"], ["Amarillas", m.yellow, "yellow"], ["Verdes", m.green, "green"]],
    big: { ninguna: "NORMAL", advertencia: "ADVERTENCIA", peligro: "ALARMA" },
    tcpHelp: "ANDON → AND;<encendidas>;<rojas>;<amarillas>;<verdes>;<torreta1>;<torreta2>;...\n         cada torreta: R=rojo A=amarillo V=verde encendidas, '-' = apagada",
  },
  stock: {
    title: "Stock", opc: "Stock", tcp: "STOCK", shape: "rect",
    toolWord: "Posición", listTitle: "Posiciones del rack", addLabel: "+ Nueva posición",
    listHint: "Dibuja cada nivel o hueco del rack. Sin posiciones solo se cuentan cajas y racks en toda la imagen.",
    note: "Modelo: yoloe-26s-seg.pt (YOLOE-26, vocabulario abierto): detecta lo que se describe en texto, en inglés. El conteo de cajas es confiable; el tipo cartón / plástico es aproximado. Puedes agregar clases como \"pallet\" o \"plastic tote\"; tras cambiar las clases, el primer análisis tarda unos segundos.",
    params: [
      { k: "fps", label: "Cuadros por segundo", type: "number", min: 0.05, max: 5, step: 0.05 },
      { k: "conf", label: "Confianza mínima", type: "number", min: 0.05, max: 0.95, step: 0.05 },
      { k: "imgsz", label: "Resolución de análisis", type: "select", num: true, options: [[640, "640 (rápido)"], [960, "960"], [1280, "1280 (cajas pequeñas)"]] },
      { k: "confirm_s", label: "Tiempo para confirmar un cambio (s)", type: "number", min: 0, step: 1 },
      { k: "model", label: "Modelo", type: "model" },
      { k: "box_classes", label: "Cajas a buscar (inglés, coma)", type: "text" },
      { k: "rack_classes", label: "Racks a buscar (inglés, coma)", type: "text" },
    ],
    alarmParams: [
      { k: "levels.empty", label: "Posición vacía", type: "select", options: LEVELS },
      { k: "levels.low", label: "Menos cajas que el mínimo", type: "select", options: LEVELS },
      { k: "levels.over", label: "Más cajas que el máximo", type: "select", options: LEVELS },
      { k: "levels.no_rack", label: "No se detecta el rack", type: "select", options: LEVELS },
    ],
    toolFields: [
      { k: "min_boxes", label: "Cajas mín", type: "number", min: 0, step: 1 },
      { k: "max_boxes", label: "Cajas máx", type: "number", min: 0, step: 1 },
      { k: "require_rack", label: "Exigir rack", type: "check" },
    ],
    newTool: () => ({ min_boxes: 1, max_boxes: 99, require_rack: true }),
    tableTitle: "Posiciones", head: ["Posición", "Cajas", "Rack", "Estado"],
    row: (t) => [esc(t.name), `${t.boxes} <span class="hint">(${t.min}–${t.max})</span>`,
      t.rack ? "sí" : `<span class="nok-text">no</span>`,
      t.status === "OK" ? `<span class="ok-text">OK</span>` : `<b class="${t.level === "peligro" ? "nok-text" : t.level === "advertencia" ? "warn-text" : ""}">${esc(t.status)}</b>`],
    kpis: (m) => [["Cajas", m.boxes], ["De cartón", m.cardboard], ["De plástico", m.plastic], ["Racks", m.racks], ["Posiciones OK", m.pos_ok, "ok"],
      ["Vacías", m.pos_empty, m.pos_empty ? "bad" : ""], ["Bajas", m.pos_low, m.pos_low ? "bad" : ""],
      ["Sin rack", m.pos_no_rack, m.pos_no_rack ? "bad" : ""]],
    big: { ninguna: "STOCK OK", advertencia: "REVISAR STOCK", peligro: "ALARMA" },
    tcpHelp: "STOCK → STK;<alarma 0/1>;<cajas>;<racks>;<pos1 cajas:estado>;<pos2 cajas:estado>;...\n         estado: OK | BAJO | VACIO | EXCESO | SINRACK",
  },
};
const MODULE_NAMES = Object.keys(MODULES);

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 3500);
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

const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });
const cfgOf = (name) => cfg.modules[name];
const recipeOf = (name) => {
  const m = cfgOf(name);
  return m.recipes.find((r) => r.id === m.active_recipe) || m.recipes[0];
};
const newId = (p) => p + Date.now().toString(36) + Math.floor(Math.random() * 100);

// ------------------------------------------------------------ campos de formulario genéricos
const getPath = (obj, path) => path.split(".").reduce((o, k) => o?.[k], obj);
function setPath(obj, path, v) {
  const ks = path.split(".");
  const last = ks.pop();
  let o = obj;
  for (const k of ks) o = o[k] ??= {};
  o[last] = v;
}

function modelOptions(value) {
  const list = models.includes(value) || !value ? models : [value, ...models];
  return list.map((m) => [m, m]);
}

function fieldHtml(f, value) {
  if (f.type === "check")
    return `<label class="check"><input type="checkbox" data-k="${f.k}" ${value ? "checked" : ""}> ${f.label}</label>`;
  if (f.type === "items")
    return `<div class="items-field"><span class="hint">${f.label}</span><div class="items">${PPE_ITEMS
      .map(([k, l]) => `<label class="check"><input type="checkbox" data-k="${f.k}.${k}" ${value?.[k] ? "checked" : ""}> ${l}</label>`)
      .join("")}</div></div>`;
  if (f.type === "select" || f.type === "model") {
    const opts = f.type === "model" ? modelOptions(value) : f.options;
    return `<label>${f.label}<select data-k="${f.k}" ${f.num ? 'data-num="1"' : ""}>${opts
      .map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(l)}</option>`)
      .join("")}</select></label>`;
  }
  const num = f.type === "number"
    ? `type="number" step="${f.step ?? "any"}" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""}`
    : "";
  return `<label>${f.label}<input data-k="${f.k}" value="${esc(value ?? "")}" ${num}></label>`;
}

function readField(inp) {
  if (inp.type === "checkbox") return inp.checked;
  if (inp.type === "number" || inp.dataset.num) return inp.value === "" ? 0 : +inp.value;
  return inp.value;
}

function renderFields(root, fields, obj, onChange) {
  root.innerHTML = fields.map((f) => fieldHtml(f, getPath(obj, f.k))).join("");
  bindFields(root, obj, onChange);
}
function bindFields(root, obj, onChange) {
  root.querySelectorAll("[data-k]").forEach((inp) =>
    inp.addEventListener("change", () => {
      setPath(obj, inp.dataset.k, readField(inp));
      onChange?.(inp.dataset.k);
    })
  );
}

// ------------------------------------------------------------ pestañas
$$(".tab").forEach((b) =>
  b.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab-page").forEach((p) => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
    activeTab = b.dataset.tab;
    setStreams();
    if (ui[activeTab]?.sub === "comms") refreshComms(activeTab);
  })
);

function goTo(tab) {
  $(`.tab[data-tab="${tab}"]`)?.click();
}

// ------------------------------------------------------------ video
// Solo se transmite el video visible: cada video abierto consume CPU del servidor.
function setImgSrc(img, url) {
  if (!url) {
    if (img.getAttribute("src")) img.removeAttribute("src"); // cierra la conexión MJPEG
    return;
  }
  if (img.dataset.url === url && img.getAttribute("src")) return; // ya está conectado
  img.dataset.url = url;
  img.src = `${url}&t=${Date.now()}`;
}

function setStreams() {
  for (const [name, u] of Object.entries(ui)) {
    const visible = activeTab === name && u.cam;
    setImgSrc(u.el("stream"), visible && u.sub === "monitor" && `/stream.mjpg?cam=${enc(u.cam)}&view=${name}`);
    setImgSrc(u.el("snap"), visible && u.sub === "tools" && `/stream.mjpg?cam=${enc(u.cam)}&view=raw`);
  }
  setImgSrc($("#cam-stream"), activeTab === "cameras" && selectedCam && `/stream.mjpg?cam=${enc(selectedCam)}&view=raw`);
}

function reconnectOnError(img) {
  // Si la conexión de video se corta (reinicio del servidor, Wi-Fi), reconectar sola.
  img.addEventListener("error", () => {
    if (!img.getAttribute("src")) return;
    img.removeAttribute("src");
    setTimeout(setStreams, 2000);
  });
}

const camSize = (cid) => camStatus.find((c) => c.id === cid)?.size;

// ------------------------------------------------------------ páginas de bloque
const ui = {};

function buildModulePages() {
  const tpl = $("#module-template");
  for (const [name, def] of Object.entries(MODULES)) {
    const page = $(`#tab-${name}`);
    page.appendChild(tpl.content.cloneNode(true));
    const el = (k) => page.querySelector(`[data-el="${k}"]`);
    const u = (ui[name] = { name, def, page, el, sub: "monitor", cam: null, selected: null, drawing: null,
      dragVertex: null, hoverPt: null, dragStart: null, pending: false, commsTimer: null });
    el("title").textContent = def.title;
    el("list-title").textContent = def.listTitle;
    el("add-tool").textContent = def.addLabel;
    el("list-hint").textContent = def.listHint;
    el("note").textContent = def.note || "";
    el("table-title").textContent = def.tableTitle;
    el("rcp-node").textContent = `${def.opc}.RecipeNumber`;
    el("rcp-cmd").textContent = `RECIPE ${def.tcp} n`;
    el("tcp-help").textContent = def.tcpHelp;
    $("thead", el("table")).innerHTML = `<tr>${def.head.map((h) => `<th>${h}</th>`).join("")}</tr>`;

    $$(".subtab", page).forEach((b) =>
      b.addEventListener("click", () => {
        u.sub = b.dataset.sub;
        $$(".subtab", page).forEach((x) => x.classList.toggle("active", x === b));
        $$(".sub-page", page).forEach((p) => p.classList.toggle("active", p.dataset.subpage === u.sub));
        if (u.sub !== "tools" && u.drawing) finishDrawing(name);
        setStreams();
        if (u.sub === "tools") drawCanvas(name);
        if (u.sub === "comms") refreshComms(name);
      })
    );
    el("run").addEventListener("click", () => toggleRun(name));
    el("sound").addEventListener("click", () => setSound(name, !cfgOf(name).alarm.sound));
    el("sound-check").addEventListener("change", (e) => setSound(name, e.target.checked));
    el("recipe").addEventListener("change", (e) => selectRecipe(name, e.target.value));
    el("camera").addEventListener("change", (e) => {
      u.cam = e.target.value;
      try { localStorage.setItem(`vs-cam-${name}`, u.cam); } catch {}
      u.selected = null;
      u.drawing = null;
      renderTools(name);
      drawCanvas(name);
      setStreams();
      renderState(name);
    });
    el("reset").addEventListener("click", async () => {
      try { await post(`/api/modules/${name}/reset`); toast("Contadores reiniciados"); }
      catch (e) { toast("Error: " + e.message); }
    });
    el("rcp-number").addEventListener("change", (e) => {
      const n = Math.max(1, Math.round(+e.target.value));
      if (cfgOf(name).recipes.some((r) => r !== recipeOf(name) && r.number === n)) {
        toast(`Ya existe una receta con el número ${n}`);
        e.target.value = recipeOf(name).number;
        return;
      }
      recipeOf(name).number = n;
      renderRecipeSelect(name);
    });
    el("rcp-name").addEventListener("change", (e) => {
      recipeOf(name).name = e.target.value.trim() || recipeOf(name).name;
      renderRecipeSelect(name);
    });
    el("rcp-new").addEventListener("click", () => {
      const nm = prompt("Nombre de la nueva receta:", `Receta ${nextRecipeNumber(name)}`);
      const extra = name === "ppe" ? { require: { ...(recipeOf(name).require || {}) } } : {};
      if (nm) newRecipe(name, nm.trim(), [], extra);
    });
    el("rcp-dup").addEventListener("click", () => {
      const r = recipeOf(name);
      const copy = structuredClone(r.tools).map((t) => ({ ...t, id: newId("t") }));
      const { id, number, name: rn, tools, ...extra } = structuredClone(r);
      newRecipe(name, `${r.name} (copia)`, copy, extra);
    });
    el("rcp-del").addEventListener("click", () => {
      const m = cfgOf(name);
      if (m.recipes.length <= 1) return toast("Debe existir al menos una receta");
      const r = recipeOf(name);
      if (!confirm(`¿Eliminar la receta ${r.number} "${r.name}" de ${def.title}?`)) return;
      m.recipes = m.recipes.filter((x) => x !== r);
      m.active_recipe = m.recipes[0].id;
      u.selected = null;
      renderModuleConfig(name);
      saveConfig("Receta eliminada");
    });
    el("add-tool").addEventListener("click", () => addTool(name));
    el("save-tools").addEventListener("click", () => {
      if (u.drawing) finishDrawing(name);
      if (def.shape === "poly") {
        const bad = recipeOf(name).tools.filter((z) => z.points.length < 3).map((z) => z.name);
        if (bad.length) return toast(`Las zonas necesitan al menos 3 puntos: ${bad.join(", ")}`);
      }
      saveConfig(`${def.title}: receta ${recipeOf(name).number} guardada`);
    });
    el("save-params").addEventListener("click", () => saveConfig(`${def.title}: parámetros guardados`));
    el("save-comms").addEventListener("click", () => {
      readMailForm(name);
      saveConfig(`${def.title}: alertas guardadas`);
    });
    el("mail-test").addEventListener("click", async () => {
      readMailForm(name);
      const to = cfgOf(name).alarm.email.to;
      if (!to.trim()) return toast("Escribe al menos un destinatario");
      toast("Enviando correo de prueba…");
      try {
        await api("/api/config", { method: "PUT", body: JSON.stringify(cfg) });
        const r = await post("/api/email/test", { module: name, to });
        toast(`Correo de prueba enviado a ${r.to.join(", ")}`);
      } catch (e) { toast("No se envió: " + e.message); }
    });
    reconnectOnError(el("stream"));
    reconnectOnError(el("snap"));
    el("snap").addEventListener("load", () => fitCanvas(name));
    setupCanvas(name);
  }
}

// ------------------------------------------------------------ Ejecutar / Detener y sonido
async function toggleRun(name) {
  const u = ui[name];
  if (u.pending) return;
  const target = !cfgOf(name).running; // lo que muestra el botón es lo que se pide
  u.pending = true;
  const b = u.el("run");
  b.disabled = true;
  b.textContent = target ? "Iniciando…" : "Deteniendo…";
  try {
    const mods = await post(`/api/modules/${name}/run`, { running: target });
    await new Promise((r) => setTimeout(r, 600)); // un doble clic no debe apagar lo que se acaba de encender
    u.pending = false;
    applyRunning(mods);
    toast(`${MODULES[name].title}: ${mods[name] ? "en ejecución" : "detenido"}`);
  } catch (e) {
    u.pending = false;
    renderRun(name);
    toast("Error: " + e.message);
  }
}

function applyRunning(mods) {
  for (const [n, on] of Object.entries(mods)) {
    if (cfg?.modules[n]) cfg.modules[n].running = on;
    renderRun(n);
  }
}

function renderRun(name) {
  const u = ui[name];
  if (!u || !cfg) return;
  const on = !!cfgOf(name).running;
  if (!u.pending) {
    const b = u.el("run");
    b.disabled = false;
    b.classList.toggle("running", on);
    b.textContent = on ? "■ Detener" : "▶ Ejecutar";
  }
  const pill = u.el("pill");
  pill.classList.toggle("on", on);
  pill.textContent = on ? "En ejecución" : "Detenido";
  $(`[data-dot="${name}"]`).classList.toggle("on", on);
}

async function setSound(name, on) {
  try {
    await post(`/api/modules/${name}/sound`, { on });
    cfgOf(name).alarm.sound = on;
    renderSound(name);
    renderAlarms(true);
    toast(`${MODULES[name].title}: sonido ${on ? "activado" : "silenciado"}`);
  } catch (e) { toast("Error: " + e.message); }
}

function renderSound(name) {
  const on = cfgOf(name).alarm.sound !== false;
  const b = ui[name].el("sound");
  b.textContent = on ? "🔊 Sonido" : "🔇 Silenciado";
  b.classList.toggle("muted", !on);
  ui[name].el("sound-check").checked = on;
}

// ------------------------------------------------------------ recetas
function renderRecipeSelect(name) {
  const m = cfgOf(name);
  const sel = ui[name].el("recipe");
  sel.innerHTML = m.recipes.slice().sort((a, b) => a.number - b.number)
    .map((r) => `<option value="${esc(r.id)}">${r.number} · ${esc(r.name)}</option>`).join("");
  sel.value = m.active_recipe;
  ui[name].el("rcp-number").value = recipeOf(name).number;
  ui[name].el("rcp-name").value = recipeOf(name).name;
}

async function selectRecipe(name, key) {
  try {
    const r = await post(`/api/modules/${name}/recipe`, { key });
    applyRecipeChange(name, r);
    toast(`${MODULES[name].title}: receta ${r.number} · ${r.name}`);
  } catch (e) {
    toast("Error: " + e.message);
    renderRecipeSelect(name);
  }
}

function applyRecipeChange(name, r) {
  if (!cfg || cfgOf(name).active_recipe === r.id) return;
  cfgOf(name).active_recipe = r.id;
  ui[name].selected = null;
  renderModuleConfig(name);
}

const nextRecipeNumber = (name) => Math.max(0, ...cfgOf(name).recipes.map((r) => r.number)) + 1;

function newRecipe(name, rname, tools, extra = {}) {
  const r = { ...extra, id: newId("r"), number: nextRecipeNumber(name), name: rname, tools };
  cfgOf(name).recipes.push(r);
  cfgOf(name).active_recipe = r.id;
  ui[name].selected = null;
  renderModuleConfig(name);
  saveConfig(`${MODULES[name].title}: receta ${r.number} creada`);
}

// ------------------------------------------------------------ configuración de un bloque
function renderCameraSelect(name) {
  const u = ui[name];
  const sel = u.el("camera");
  const cams = cfg.cameras;
  let stored = null;
  try { stored = localStorage.getItem(`vs-cam-${name}`); } catch {}
  const withFn = cams.filter((c) => c.enabled !== false && c.functions?.includes(name));
  if (!cams.some((c) => c.id === u.cam)) u.cam = null;
  u.cam = u.cam || (cams.some((c) => c.id === stored) ? stored : null) || withFn[0]?.id || cams[0]?.id || null;
  sel.innerHTML = cams.map((c) => {
    const has = c.enabled !== false && c.functions?.includes(name);
    return `<option value="${esc(c.id)}">${esc(c.name)}${has ? "" : " (función no asignada)"}</option>`;
  }).join("");
  if (u.cam) sel.value = u.cam;
}

function renderModuleConfig(name) {
  const u = ui[name];
  const def = u.def;
  const m = cfgOf(name);
  renderRecipeSelect(name);
  renderCameraSelect(name);
  renderRun(name);
  renderSound(name);
  renderFields(u.el("recipe-fields"), def.recipeFields || [], recipeOf(name));
  renderFields(u.el("params"), def.params, m);
  renderFields(u.el("alarm-params"), def.alarmParams, m);
  if (def.alarmHint) u.el("alarm-params").insertAdjacentHTML("beforeend", `<p class="hint">${def.alarmHint}</p>`);
  const e = m.alarm.email;
  u.el("mail-enabled").checked = !!e.enabled;
  u.el("mail-to").value = e.to || "";
  u.el("mail-level").value = e.min_level || "peligro";
  u.el("mail-cooldown").value = e.cooldown_min ?? 10;
  u.el("mail-image").checked = e.attach_image !== false;
  u.el("mail-clear").checked = !!e.on_clear;
  u.el("mail-missing").hidden = emailConfigured;
  renderTools(name);
  drawCanvas(name);
}

function readMailForm(name) {
  const u = ui[name];
  Object.assign(cfgOf(name).alarm.email, {
    enabled: u.el("mail-enabled").checked,
    to: u.el("mail-to").value.trim(),
    min_level: u.el("mail-level").value,
    cooldown_min: Math.max(0, +u.el("mail-cooldown").value || 0),
    attach_image: u.el("mail-image").checked,
    on_clear: u.el("mail-clear").checked,
  });
}

// ------------------------------------------------------------ herramientas (zonas, áreas, torretas, posiciones)
const camTools = (name) => recipeOf(name).tools.filter((t) => t.camera === ui[name].cam);

function addTool(name, quiet = false) {
  const u = ui[name];
  if (!u.cam) return toast("Primero agrega una cámara (pestaña Cámaras)");
  const def = u.def;
  const [w, h] = camSize(u.cam) || [1280, 720];
  const t = { id: newId("t"), name: `${def.toolWord} ${camTools(name).length + 1}`,
    enabled: true, camera: u.cam, ...def.newTool(name) };
  if (def.shape === "rect") t.roi = [Math.round(w / 4), Math.round(h / 4), Math.round(w / 2), Math.round(h / 2)];
  recipeOf(name).tools.push(t);
  u.selected = t.id;
  if (def.shape === "poly") u.drawing = t.id;
  renderTools(name);
  drawCanvas(name);
  if (!quiet)
    toast(def.shape === "poly" ? "Haz clic sobre la imagen para marcar los puntos de la zona"
      : "Arrastra sobre la imagen para dibujar el rectángulo");
  return t;
}

function renderTools(name) {
  const u = ui[name];
  const def = u.def;
  const list = u.el("tool-list");
  const tools = camTools(name);
  const other = recipeOf(name).tools.length - tools.length;
  list.innerHTML = tools.length ? "" : `<p class="hint">No hay ${def.listTitle.toLowerCase()} en esta cámara.</p>`;
  tools.forEach((t) => {
    const div = document.createElement("div");
    div.className = "tool" + (t.id === u.selected ? " selected" : "");
    const geo = def.shape === "poly" ? `${t.points.length} puntos`
      : `x=${t.roi[0]} y=${t.roi[1]} w=${t.roi[2]} h=${t.roi[3]}`;
    div.innerHTML = `
      <div class="tool-head">
        <input type="checkbox" data-f="enabled" ${t.enabled ? "checked" : ""} title="Habilitada">
        <input type="text" data-f="name" value="${esc(t.name)}">
        <button class="danger admin-only" data-del title="Eliminar">✕</button>
      </div>
      <div class="tool-body param-grid"></div>
      <div class="zone-foot">
        <span class="hint">${geo}</span>
        <span class="zone-live" data-live="${esc(t.id)}"></span>
        ${def.shape === "poly" ? `<button class="admin-only" data-redraw>${u.drawing === t.id ? "Terminar" : "Redibujar"}</button>` : ""}
      </div>`;
    renderFields($(".tool-body", div), def.toolFields, t, () => drawCanvas(name));
    div.addEventListener("click", (e) => {
      if (e.target.closest("input,button,select,label")) return;
      u.selected = t.id;
      renderTools(name);
      drawCanvas(name);
    });
    div.querySelectorAll("[data-f]").forEach((inp) =>
      inp.addEventListener("change", () => {
        t[inp.dataset.f] = inp.type === "checkbox" ? inp.checked : inp.value;
        drawCanvas(name);
      })
    );
    $("[data-del]", div).addEventListener("click", () => {
      if (!confirm(`¿Eliminar "${t.name}"?`)) return;
      const all = recipeOf(name).tools;
      all.splice(all.indexOf(t), 1);
      if (u.drawing === t.id) u.drawing = null;
      renderTools(name);
      drawCanvas(name);
    });
    $("[data-redraw]", div)?.addEventListener("click", () => {
      if (u.drawing === t.id) return finishDrawing(name);
      u.selected = t.id;
      u.drawing = t.id;
      t.points = [];
      renderTools(name);
      drawCanvas(name);
    });
    list.appendChild(div);
  });
  if (other > 0) list.insertAdjacentHTML("beforeend", `<p class="hint">Esta receta tiene ${other} más en otras cámaras.</p>`);
  u.el("draw-hint").textContent = !isAdmin() ? "Solo un administrador puede dibujar."
    : def.shape === "poly"
      ? u.drawing ? "Clic para agregar puntos. Clic en el primer punto (o doble clic) para cerrar la zona."
        : "Selecciona una zona; arrastra sus vértices para ajustarla."
      : "Arrastra sobre la imagen para dibujar el rectángulo del elemento seleccionado (sin selección se crea uno nuevo).";
  renderToolLive(name);
}

function renderToolLive(name) {
  const s = states[name];
  const byId = Object.fromEntries((s?.tools || []).map((t) => [t.id, t]));
  $$("[data-live]", ui[name].page).forEach((el) => {
    const t = byId[el.dataset.live];
    el.innerHTML = !s?.running || !t ? "" : `<b class="${t.level === "peligro" ? "nok-text" : t.level === "advertencia" ? "warn-text" : "ok-text"}">${esc(t.text)}</b>`;
  });
}

function finishDrawing(name) {
  const u = ui[name];
  const z = recipeOf(name).tools.find((x) => x.id === u.drawing);
  u.drawing = null;
  u.hoverPt = null;
  if (z && z.points.length < 3) toast(`"${z.name}" necesita al menos 3 puntos`);
  renderTools(name);
  drawCanvas(name);
}

// ------------------------------------------------------------ dibujo sobre la imagen
const SHAPE_COLORS = { peligro: "#e5484d", advertencia: "#f5a524" };

// El lienzo usa la resolución real de la cámara para que todo quede en píxeles de imagen.
function fitCanvas(name) {
  const u = ui[name];
  const cv = u.el("canvas");
  const snap = u.el("snap");
  const [w, h] = camSize(u.cam) || [snap.naturalWidth, snap.naturalHeight];
  if (!w || !h || (cv.width === w && cv.height === h)) return;
  cv.width = w;
  cv.height = h;
  drawCanvas(name);
}

function drawCanvas(name) {
  const u = ui[name];
  if (!cfg || !u) return;
  const cv = u.el("canvas");
  if (!cv.width) return;
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  const lw = Math.max(2, cv.width / 400);
  ctx.font = `bold ${Math.round(cv.width / 50)}px Segoe UI`;
  for (const t of camTools(name)) {
    const sel = t.id === u.selected;
    const color = u.def.shape === "poly" ? SHAPE_COLORS[t.level] || SHAPE_COLORS.peligro : sel ? "#3d8bfd" : "#f5a524";
    ctx.globalAlpha = t.enabled ? 1 : 0.4;
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = sel ? lw * 1.6 : lw;
    ctx.setLineDash(t.enabled ? [] : [lw * 4, lw * 3]);
    if (u.def.shape === "poly") {
      if (!t.points.length) continue;
      const drawing = u.drawing === t.id;
      ctx.beginPath();
      t.points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      if (drawing && u.hoverPt) ctx.lineTo(...u.hoverPt);
      if (!drawing) {
        ctx.closePath();
        ctx.globalAlpha = t.enabled ? 0.25 : 0.08;
        ctx.fill();
        ctx.globalAlpha = t.enabled ? 1 : 0.4;
      }
      ctx.stroke();
      ctx.setLineDash([]);
      if (sel)
        t.points.forEach(([x, y], i) => {
          ctx.beginPath();
          ctx.arc(x, y, i === 0 && drawing ? lw * 5 : lw * 3, 0, Math.PI * 2);
          ctx.fillStyle = "#fff";
          ctx.fill();
          ctx.stroke();
        });
      ctx.fillStyle = color;
      ctx.fillText(t.name, t.points[0][0] + 8, t.points[0][1] - 8);
    } else {
      const [x, y, w, h] = sel && u.temp ? u.temp : t.roi;
      ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([]);
      ctx.fillText(t.name, x + 6, y + cv.width / 45);
    }
    ctx.globalAlpha = 1;
  }
}

function toImg(cv, e) {
  const r = cv.getBoundingClientRect();
  return [((e.clientX - r.left) * cv.width) / r.width, ((e.clientY - r.top) * cv.height) / r.height].map(Math.round);
}
const nearVertex = (t, p, tol) => t.points.findIndex(([x, y]) => Math.hypot(x - p[0], y - p[1]) < tol);

function setupCanvas(name) {
  const u = ui[name];
  const cv = u.el("canvas");
  const selTool = () => recipeOf(name).tools.find((t) => t.id === (u.drawing || u.selected));
  const rectFrom = (e) => {
    const [x2, y2] = toImg(cv, e);
    const [x1, y1] = u.dragStart;
    return [Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1)];
  };
  cv.addEventListener("pointerdown", (e) => {
    if (!isAdmin()) return;
    let t = selTool();
    if (!t || t.camera !== u.cam) {
      if (u.def.shape !== "rect") return toast("Primero selecciona o crea una zona de la lista");
      t = addTool(name, true); // arrastrar sin nada seleccionado crea un elemento nuevo
    }
    const p = toImg(cv, e);
    if (u.def.shape === "rect") {
      u.dragStart = p;
      cv.setPointerCapture(e.pointerId);
      return;
    }
    const tol = cv.width / 60;
    if (u.drawing) {
      if (t.points.length >= 3 && nearVertex(t, p, tol) === 0) return finishDrawing(name);
      // un doble clic genera dos clics en el mismo lugar: no repetir el punto
      const last = t.points[t.points.length - 1];
      if (last && Math.hypot(last[0] - p[0], last[1] - p[1]) < tol / 2) return;
      t.points.push(p);
      renderTools(name);
      drawCanvas(name);
      return;
    }
    const i = nearVertex(t, p, tol);
    if (i >= 0) {
      u.dragVertex = i;
      cv.setPointerCapture(e.pointerId);
    }
  });
  cv.addEventListener("pointermove", (e) => {
    if (u.def.shape === "rect") {
      if (!u.dragStart) return;
      u.temp = rectFrom(e);
      drawCanvas(name);
      return;
    }
    const p = toImg(cv, e);
    if (u.dragVertex !== null) {
      selTool().points[u.dragVertex] = p;
      drawCanvas(name);
    } else if (u.drawing) {
      u.hoverPt = p;
      drawCanvas(name);
    }
  });
  cv.addEventListener("pointerup", (e) => {
    if (u.def.shape === "rect" && u.dragStart) {
      const r = rectFrom(e);
      u.dragStart = null;
      u.temp = null;
      if (r[2] > 5 && r[3] > 5) {
        selTool().roi = r;
        renderTools(name);
      }
      drawCanvas(name);
    }
    u.dragVertex = null;
  });
  cv.addEventListener("dblclick", () => u.drawing && finishDrawing(name));
}

// ------------------------------------------------------------ estado en vivo de un bloque
function renderState(name) {
  const u = ui[name];
  const s = states[name];
  if (!u || !s || !cfg) return;
  const def = u.def;
  const lv = s.running ? s.level : "ninguna";
  const lp = u.el("level-pill");
  lp.className = "level-pill " + (s.running ? lv : "");
  lp.textContent = !s.running ? "" : s.error && !s.active ? "ERROR" : LEVEL_TEXT[lv];
  lp.hidden = !s.running;
  $(`.tab[data-tab="${name}"]`).classList.toggle("tab-danger", s.running && lv === "peligro");
  $(`.tab[data-tab="${name}"]`).classList.toggle("tab-warning", s.running && lv === "advertencia");

  const tile = u.el("tile");
  tile.classList.toggle("ok", s.running && s.active && lv === "ninguna" && !s.error);
  tile.classList.toggle("warn", s.running && lv === "advertencia");
  tile.classList.toggle("nok", s.running && lv === "peligro");
  u.el("big").textContent = !s.running ? "DETENIDO" : !s.active ? "INICIANDO…" : def.big[lv];
  u.el("meta").innerHTML = s.running
    ? `<span>Receta ${s.recipe.number}: ${esc(s.recipe.name)}</span><span>${s.fps} ciclos/s</span>` : "";
  u.el("tile-text").innerHTML = s.error ? `<span class="nok-text">${esc(s.error)}</span>` : esc(s.text || "");
  const camState = s.cameras?.[u.cam];
  u.el("vinfo").textContent = !s.running ? "Bloque detenido: presiona Ejecutar"
    : !cfg.cameras.find((c) => c.id === u.cam)?.functions?.includes(name) ? "Esta cámara no tiene asignada la función (pestaña Cámaras)"
      : camState?.error || "";

  const m = { ...s.summary };
  for (const k of Object.keys(m)) m[k] ??= 0;
  u.el("sum-title").textContent = Object.keys(s.cameras || {}).length > 1 ? "Resumen (todas las cámaras)" : "Resumen";
  u.el("kpis").innerHTML = def.kpis(m).map(([label, v, cls]) =>
    `<div class="kpi ${cls || ""}"><b>${v ?? 0}</b><span>${label}</span></div>`).join("");
  const rows = s.tools.filter((t) => !u.cam || t.camera === u.cam || Object.keys(s.cameras || {}).length <= 1);
  $("tbody", u.el("table")).innerHTML = !s.running ? `<tr><td colspan="${def.head.length}" class="hint">Detenido</td></tr>`
    : rows.length ? rows.map((t) => `<tr>${def.row(t).map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${def.head.length}" class="hint">Sin resultados</td></tr>`;
  renderToolLive(name);
}

// ------------------------------------------------------------ alarmas visibles y sonido
let alarmKey = "";
function renderAlarms(force) {
  const active = MODULE_NAMES.map((n) => states[n]).filter((s) => s?.running && s.level !== "ninguna");
  active.sort((a, b) => (b.level === "peligro") - (a.level === "peligro"));
  const key = JSON.stringify(active.map((s) => [s.module, s.level, s.text, cfgOf(s.module).alarm.sound]));
  if (key === alarmKey && !force) return;
  alarmKey = key;
  $("#alarms").innerHTML = active.map((s) => {
    const on = cfgOf(s.module).alarm.sound !== false;
    return `<div class="alarm ${s.level === "peligro" ? "danger" : "warning"}">
      <button class="alarm-go" data-go="${s.module}">${s.level === "peligro" ? "⛔" : "⚠"} ${esc(MODULES[s.module].title.toUpperCase())}: ${esc(s.text || LEVEL_TEXT[s.level])}</button>
      <button class="alarm-sound" data-snd="${s.module}">${on ? "🔇 Silenciar" : "🔊 Activar sonido"}</button></div>`;
  }).join("");
  $$("[data-go]", $("#alarms")).forEach((b) => b.addEventListener("click", () => goTo(b.dataset.go)));
  $$("[data-snd]", $("#alarms")).forEach((b) =>
    b.addEventListener("click", () => setSound(b.dataset.snd, !(cfgOf(b.dataset.snd).alarm.sound !== false))));
}

// Sonido con Web Audio (sin archivos). El navegador solo permite sonar después de un clic.
const audio = { ctx: null, tick: 0 };
function audioCtx() {
  if (!audio.ctx) {
    try { audio.ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch { audio.ctx = null; }
  }
  return audio.ctx;
}
document.addEventListener("pointerdown", () => audioCtx()?.resume?.(), { capture: true });
document.addEventListener("keydown", () => audioCtx()?.resume?.(), { capture: true });

function beep(ctx, freq, start, dur, vol) {
  const osc = ctx.createOscillator();
  const g = ctx.createGain();
  osc.type = "square";
  osc.frequency.value = freq;
  g.gain.setValueAtTime(0.0001, start);
  g.gain.exponentialRampToValueAtTime(vol, start + 0.02);
  g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
  osc.connect(g).connect(ctx.destination);
  osc.start(start);
  osc.stop(start + dur + 0.05);
}

function soundLevel() {
  let lv = null;
  for (const n of MODULE_NAMES) {
    const s = states[n];
    if (!s?.running || s.level === "ninguna" || cfgOf(n).alarm.sound === false) continue;
    if (s.level === "peligro") return "peligro";
    lv = "advertencia";
  }
  return lv;
}

setInterval(() => {
  if (!cfg) return;
  audio.tick++;
  const lv = soundLevel();
  const ctx = lv ? audioCtx() : audio.ctx;
  $("#sound-unlock").hidden = !(lv && ctx && ctx.state !== "running");
  if (!lv || !ctx || ctx.state !== "running") return;
  const t = ctx.currentTime;
  if (lv === "peligro") { // sirena continua de dos tonos
    beep(ctx, 880, t, 0.2, 0.12);
    beep(ctx, 660, t + 0.25, 0.2, 0.12);
    beep(ctx, 880, t + 0.5, 0.2, 0.12);
    beep(ctx, 660, t + 0.75, 0.2, 0.12);
  } else if (audio.tick % 3 === 0) { // advertencia: un tono cada 3 s
    beep(ctx, 520, t, 0.3, 0.08);
    beep(ctx, 520, t + 0.4, 0.3, 0.08);
  }
}, 1000);

// ------------------------------------------------------------ eventos (historial de alarmas)
function upsertEvent(ev) {
  const i = events.findIndex((e) => e.id === ev.id);
  if (i >= 0) events[i] = ev;
  else events.unshift(ev);
  if (events.length > 400) events.length = 400;
  renderEvents(ev.module);
}

function renderEvents(name) {
  const u = ui[name];
  if (!u) return;
  const list = events.filter((e) => e.module === name).slice(0, 15);
  u.el("events").innerHTML = list.length ? list.map((e) => `
    <div class="event"><span class="dot ${e.level === "peligro" ? "nok" : e.level === "advertencia" ? "warn" : "ok"}"></span>
      <span class="ev-time">${esc(e.time.slice(5, 16))}</span>
      <span class="ev-text">${esc(e.text)}${e.email ? `<br><span class="hint">✉ ${esc(e.email)}</span>` : ""}</span></div>`).join("")
    : `<p class="hint">Sin alarmas registradas.</p>`;
}

// ------------------------------------------------------------ comunicación de un bloque
async function refreshComms(name) {
  const u = ui[name];
  clearTimeout(u.commsTimer);
  if (activeTab !== name || u.sub !== "comms") return;
  try {
    const c = await api(`/api/modules/${name}/comms`);
    u.el("opc-info").innerHTML = c.opc_enabled
      ? `Namespace <span class="mono">${esc(c.namespace)}</span> · NodeId <span class="mono">ns=&lt;idx&gt;;s=&lt;nodo&gt;</span>`
      : `<span class="warn-text">OPC UA deshabilitado (pestaña Sistema).</span>`;
    $("tbody", u.el("opc-table")).innerHTML = c.nodes.map((n) =>
      `<tr><td>${esc(n.path)}</td><td>${esc(n.type)}</td><td>${n.value === null ? "—" : esc(typeof n.value === "boolean" ? (n.value ? "TRUE" : "FALSE") : n.value)}</td></tr>`).join("");
    u.el("tcp-info").innerHTML = c.tcp_enabled
      ? `Puerto <b>${c.tcp_port}</b> · envía <span class="mono">${esc(c.tcp_command)}</span> terminado en CR o CRLF`
      : `<span class="warn-text">TCP deshabilitado (pestaña Sistema).</span>`;
    u.el("tcp-live").textContent = c.tcp_response;
  } catch {}
  u.commsTimer = setTimeout(() => refreshComms(name), 1500);
}

// ------------------------------------------------------------ WebSocket
function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => {
    wsConnected = true;
    setChip("#chip-ws", true);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "module") {
      states[msg.name] = msg.data;
      if (cfg) {
        if (cfgOf(msg.name).alarm.sound !== msg.data.sound) {
          cfgOf(msg.name).alarm.sound = msg.data.sound;
          renderSound(msg.name);
        }
        renderState(msg.name);
        renderAlarms();
      }
    } else if (msg.type === "event") upsertEvent(msg.data);
    else if (msg.type === "recipe") applyRecipeChange(msg.name, msg.data);
    else if (msg.type === "modules") applyRunning(msg.data);
  };
  ws.onclose = (ev) => {
    wsConnected = false;
    if (ev.code === 4401) return (location.href = "/login");
    setChip("#chip-ws", false);
    setTimeout(connectWs, 2000);
  };
}

// ------------------------------------------------------------ estado general
function setChip(sel, ok, title = "") {
  const c = $(sel);
  c.classList.toggle("ok", ok === true);
  c.classList.toggle("bad", ok === false);
  c.title = title;
}

async function pollStatus() {
  try {
    const s = await api("/api/status");
    camStatus = s.cameras;
    emailConfigured = s.email_configured;
    MODULE_NAMES.forEach((n) => {
      ui[n].el("mail-missing").hidden = emailConfigured;
      fitCanvas(n);
      if (!wsConnected) {
        states[n] = s.modules[n];
        renderState(n);
      }
    });
    if (!wsConnected) renderAlarms();
    const enabled = camStatus.filter((c) => c.enabled);
    const bad = enabled.filter((c) => !c.ok);
    setChip("#chip-cam", enabled.length ? !bad.length : null,
      enabled.map((c) => `${c.name}: ${c.ok ? c.fps + " fps" : c.error || "sin imagen"}`).join("\n"));
    $("#chip-cam").textContent = `Cámaras ${enabled.length - bad.length}/${enabled.length}`;
    setChip("#chip-opc", s.opcua.enabled ? s.opcua.running : null, s.opcua.error || s.opcua.endpoints[0]);
    setChip("#chip-tcp", s.tcp.enabled ? s.tcp.running : null, s.tcp.error || `puerto ${s.tcp.port}`);
    $("#opc-endpoints").innerHTML = s.opcua.endpoints.map((e) => `<li>${esc(e)}</li>`).join("");
    $("#web-urls").innerHTML = s.web.map((u) => `<li><a href="${esc(u)}" style="color:inherit">${esc(u)}</a></li>`).join("");
    $("#tcp-clients").textContent = s.tcp.clients;
    renderCamList();
  } catch {
    ["#chip-cam", "#chip-opc", "#chip-tcp"].forEach((c) => setChip(c, false, "sin conexión"));
  }
}

// ------------------------------------------------------------ configuración
async function loadConfig() {
  cfg = await api("/api/config");
  try { models = await api("/api/models"); } catch { models = []; }
  MODULE_NAMES.forEach(renderModuleConfig);
  renderCameras();
  fillSystemForm();
}

async function saveConfig(msg) {
  try {
    const r = await api("/api/config", { method: "PUT", body: JSON.stringify(cfg) });
    toast(r.restart_required ? "Guardado. Reinicia el servicio para aplicar el puerto web." : msg || "Guardado");
    setTimeout(setStreams, 500);
  } catch (e) {
    toast("Error al guardar: " + e.message);
  }
}

// ------------------------------------------------------------ cámaras
let selectedCam = null;

function renderCameras() {
  if (!cfg.cameras.some((c) => c.id === selectedCam)) selectedCam = cfg.cameras[0]?.id || null;
  renderCamList();
  fillCamForm();
  setStreams();
}

function renderCamList() {
  if (!cfg) return;
  const st = Object.fromEntries(camStatus.map((c) => [c.id, c]));
  $("#cam-list").innerHTML = cfg.cameras.length ? cfg.cameras.map((c) => {
    const s = st[c.id];
    const dot = c.enabled === false ? "" : s?.ok ? "ok" : "nok";
    return `<div class="tool cam-item ${c.id === selectedCam ? "selected" : ""}" data-cam="${esc(c.id)}">
      <div class="tool-head"><span class="dot ${dot}"></span><b>${esc(c.name)}</b>
        <span class="tool-type">${c.enabled === false ? "deshabilitada" : s?.ok ? `${s.size?.join("×") || ""} · ${s.fps} fps` : esc(s?.error || "sin imagen")}</span></div>
      <div class="badges">${(c.functions || []).map((f) => `<span class="badge">${FUNCTION_LABELS[f]}</span>`).join("") || '<span class="hint">Sin funciones asignadas</span>'}</div>
    </div>`;
  }).join("") : `<p class="hint">No hay cámaras.</p>`;
  $$("[data-cam]", $("#cam-list")).forEach((d) =>
    d.addEventListener("click", () => {
      selectedCam = d.dataset.cam;
      renderCamList();
      fillCamForm();
      setStreams();
    })
  );
  const s = st[selectedCam];
  $("#cam-info").textContent = s ? (s.ok ? `${s.name} · ${s.size?.join("×") || ""} · ${s.fps} fps` : s.error || "") : "";
}

const camOf = () => cfg.cameras.find((c) => c.id === selectedCam);

function fillCamForm() {
  const c = camOf();
  $("#cam-form").hidden = !c;
  if (!c) return;
  $("#cam-name").value = c.name;
  $("#cam-enabled").checked = c.enabled !== false;
  $("#cam-type").value = c.type;
  $("#cam-source").value = c.source;
  $("#cam-user").value = c.user || "";
  $("#cam-password").value = c.password || "";
  $("#cam-width").value = c.width;
  $("#cam-height").value = c.height;
  $("#cam-functions").innerHTML = MODULE_NAMES.map((f) =>
    `<label class="check"><input type="checkbox" data-fn="${f}" ${c.functions?.includes(f) ? "checked" : ""}> ${FUNCTION_LABELS[f]}</label>`).join("");
  $$("[data-fn]").forEach((inp) => inp.addEventListener("change", readCamForm));
}

function readCamForm() {
  const c = camOf();
  if (!c) return;
  Object.assign(c, {
    name: $("#cam-name").value.trim() || c.name,
    enabled: $("#cam-enabled").checked,
    type: $("#cam-type").value,
    source: $("#cam-source").value.trim(),
    user: $("#cam-user").value.trim(),
    password: $("#cam-password").value,
    width: +$("#cam-width").value || 1280,
    height: +$("#cam-height").value || 720,
    functions: [...$$("[data-fn]")].filter((i) => i.checked).map((i) => i.dataset.fn),
  });
  renderCamList();
}
["#cam-name", "#cam-enabled", "#cam-type", "#cam-source", "#cam-user", "#cam-password", "#cam-width", "#cam-height"]
  .forEach((s) => $(s).addEventListener("change", readCamForm));

$("#btn-add-cam").addEventListener("click", () => {
  let n = cfg.cameras.length + 1;
  while (cfg.cameras.some((c) => c.id === `cam${n}`)) n++;
  cfg.cameras.push({ id: `cam${n}`, name: `Cámara ${n}`, enabled: true, type: "url", source: "rtsp://",
    user: "", password: "", width: 1280, height: 720, functions: [] });
  selectedCam = `cam${n}`;
  renderCameras();
  toast("Completa los datos de la cámara y presiona Guardar cámaras");
});

$("#btn-del-cam").addEventListener("click", () => {
  const c = camOf();
  if (!c) return;
  if (!confirm(`¿Eliminar la cámara "${c.name}"? Sus zonas, áreas, torretas y posiciones quedarán sin uso.`)) return;
  cfg.cameras = cfg.cameras.filter((x) => x !== c);
  renderCameras();
  MODULE_NAMES.forEach(renderModuleConfig);
  saveConfig("Cámara eliminada");
});

$("#btn-save-cam").addEventListener("click", async () => {
  readCamForm();
  await saveConfig("Cámaras guardadas");
  MODULE_NAMES.forEach(renderModuleConfig);
  setStreams();
});

// ------------------------------------------------------------ sistema: correo y comunicación
const MAIL_HELP = {
  gmail: "Gmail: activa la verificación en 2 pasos y crea una <b>contraseña de aplicación</b> en myaccount.google.com/apppasswords (16 letras). La contraseña normal de Gmail no funciona.",
  outlook: "Outlook / Microsoft 365: la cuenta debe tener habilitado <b>SMTP autenticado</b> (el administrador de Microsoft 365 lo activa por buzón). Si la cuenta usa verificación en 2 pasos, usa una contraseña de aplicación.",
  smtp: "Servidor de correo de la empresa. Pide al área de TI el servidor, puerto y si requiere usuario. Para un relay interno sin autenticación deja el usuario vacío.",
};

function renderMailProvider() {
  const p = $("#mail-provider").value;
  $$("[data-prov]").forEach((el) => (el.hidden = el.dataset.prov !== p));
  $("#mail-address-label").textContent = p === "gmail" ? "Correo de Gmail" : p === "outlook" ? "Correo de Outlook / Microsoft 365" : "Remitente (correo)";
  $("#mail-password-label").textContent = p === "gmail" ? "Contraseña de aplicación" : "Contraseña";
  $("#mail-help").innerHTML = MAIL_HELP[p];
}
$("#mail-provider").addEventListener("change", renderMailProvider);

function fillSystemForm() {
  const e = cfg.email;
  $("#mail-provider").value = e.provider || "gmail";
  $("#mail-host").value = e.host || "";
  $("#mail-port").value = e.port || 587;
  $("#mail-security").value = e.security || "starttls";
  $("#mail-user").value = e.user || "";
  $("#mail-address").value = e.address || "";
  $("#mail-password").value = e.password || "";
  $("#mail-from-name").value = e.from_name || "VisionServer";
  renderMailProvider();
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

function readMailSystem() {
  cfg.email = {
    provider: $("#mail-provider").value,
    host: $("#mail-host").value.trim(),
    port: +$("#mail-port").value || 587,
    security: $("#mail-security").value,
    user: $("#mail-user").value.trim(),
    address: $("#mail-address").value.trim(),
    password: $("#mail-password").value,
    from_name: $("#mail-from-name").value.trim() || "VisionServer",
  };
}

$("#btn-save-mail").addEventListener("click", async () => {
  readMailSystem();
  await saveConfig("Correo guardado");
  pollStatus();
});

$("#btn-mail-test").addEventListener("click", async () => {
  const to = $("#mail-test-to").value.trim();
  if (!to) return toast("Escribe el correo al que se enviará la prueba");
  readMailSystem();
  toast("Enviando correo de prueba…");
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify(cfg) });
    const r = await post("/api/email/test", { to });
    toast(`Correo de prueba enviado a ${r.to.join(", ")}`);
    pollStatus();
  } catch (e) { toast("No se envió: " + e.message); }
});

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
  saveConfig("Comunicación guardada");
});

// ------------------------------------------------------------ inicio
$("#btn-logout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
});
reconnectOnError($("#cam-stream"));

(async () => {
  buildModulePages();
  me = await api("/api/me");
  $("#me-user").textContent = me.user;
  $("#me-role").textContent = me.role;
  document.body.classList.add("role-" + me.role);
  await pollStatus();
  await loadConfig();
  try { events = await api("/api/events?limit=300"); } catch { events = []; }
  MODULE_NAMES.forEach((n) => { renderEvents(n); renderState(n); });
  await pollStatus();
  setInterval(pollStatus, 2000);
  setStreams();
  connectWs();
})();
