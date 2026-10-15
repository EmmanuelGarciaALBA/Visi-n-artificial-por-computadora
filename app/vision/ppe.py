"""Equipo de protección personal (EPP): casco, guantes y lentes (y opcional chaleco, cubrebocas, calzado).

Dos modelos:
  - personas: YOLO COCO (yolo26n.pt), compartido con zonas de seguridad (caché por cuadro)
  - equipo:   epp_yolov8n.pt (MIT) -> Gloves, Vest, goggles, helmet, mask, safety_shoe

Cada equipo detectado se asigna a la persona en cuya región del cuerpo cae su centro
(casco y lentes: cabeza; guantes: brazos/manos; chaleco: torso; calzado: pies).
Una persona sin alguno de los equipos requeridos durante `confirm_ms` genera la alarma.

Herramienta (área): {id, name, enabled, camera, roi: [x, y, w, h], require: {helmet: bool, ...}}
Sin áreas, se evalúa toda la imagen con los requisitos de la receta.
"""
import cv2

from . import yolo
from .module import GRAY, LEVEL_COLORS, NEUTRAL, Debounce, Module, banner, in_rect, put_label, worst
from ..config import PPE_ITEMS, PPE_REQUIRE_DEFAULT

# Clase del modelo EPP para cada equipo (se comparan sin distinguir mayúsculas)
ITEM_CLASS = {"helmet": "helmet", "gloves": "gloves", "goggles": "goggles", "vest": "vest",
              "mask": "mask", "shoes": "safety_shoe"}
ITEM_LABEL = {"helmet": "casco", "gloves": "guantes", "goggles": "lentes", "vest": "chaleco",
              "mask": "cubrebocas", "shoes": "calzado"}
# Región del cuerpo (fracciones del recuadro de la persona): x_margen, y_inicio, y_fin
ITEM_REGION = {"helmet": (0.10, -0.12, 0.30), "goggles": (0.05, -0.05, 0.30), "mask": (0.05, 0.0, 0.35),
               "vest": (0.10, 0.12, 0.75), "gloves": (0.35, 0.10, 1.00), "shoes": (0.15, 0.70, 1.08)}
GEAR_COLOR = (255, 200, 0)
ALL = "_all"


def _region(box, item):
    x, y, w, h = box
    mx, y0, y1 = ITEM_REGION[item]
    return [x - mx * w, y + y0 * h, w * (1 + 2 * mx), (y1 - y0) * h]


class PpeModule(Module):
    name = "ppe"
    title = "EPP"
    opc_folder = "PPE"
    opc_tools_folder = "Areas"
    opc_summary = [
        ("PersonCount", "UInt32", lambda s: s["summary"].get("persons", 0)),
        ("PersonsOk", "UInt32", lambda s: s["summary"].get("ok", 0)),
        ("PersonsWithoutPPE", "UInt32", lambda s: s["summary"].get("violations", 0)),
        ("NoHelmet", "UInt32", lambda s: s["summary"].get("no_helmet", 0)),
        ("NoGloves", "UInt32", lambda s: s["summary"].get("no_gloves", 0)),
        ("NoGoggles", "UInt32", lambda s: s["summary"].get("no_goggles", 0)),
        ("NoVest", "UInt32", lambda s: s["summary"].get("no_vest", 0)),
        ("NoMask", "UInt32", lambda s: s["summary"].get("no_mask", 0)),
        ("NoShoes", "UInt32", lambda s: s["summary"].get("no_shoes", 0)),
        ("ViolationEvents", "UInt32", lambda s: s["counters"].get("events", 0)),
    ]
    opc_tool_fields = [
        ("Ok", "Boolean", lambda t: t["ok"]),
        ("Persons", "UInt32", lambda t: t["persons"]),
        ("PersonsWithoutPPE", "UInt32", lambda t: t["violations"]),
    ]
    tcp_prefix = "EPP"

    def __init__(self, hub):
        self._deb = Debounce()
        super().__init__(hub)

    def reset(self):
        self._deb.clear()

    def empty_summary(self):
        return {"persons": 0, "evaluated": 0, "ok": 0, "violations": 0, **{f"no_{i}": 0 for i in PPE_ITEMS}}

    def process(self, cid, fid, img, tools, recipe, now):
        c = self.cfg
        persons = yolo.detect(img, c.get("person_model") or yolo.DEFAULT_MODEL, c.get("person_conf", 0.5),
                              [yolo.PERSON], key=(cid, fid))
        gear = yolo.detect(img, c.get("model") or "epp_yolov8n.pt", c.get("conf", 0.35), None, key=(cid, fid))
        for g in gear:
            g["item"] = next((i for i, n in ITEM_CLASS.items() if n == g["cls"].lower()), None)
        gear = [g for g in gear if g["item"]]

        base_req = {**PPE_REQUIRE_DEFAULT, **recipe.get("require", {})}
        H, W = img.shape[:2]
        areas = [t for t in tools if t.get("roi")] or [
            {"id": ALL, "name": "Toda la imagen", "roi": [0, 0, W, H], "require": base_req}]
        min_h = float(c.get("min_person_px", 120))

        area_stats = {a["id"]: {"persons": 0, "violations": 0, "missing": {}} for a in areas}
        people = []
        for p in persons:
            x, y, w, h = p["box"]
            feet = (x + w / 2, y + h)
            area = next((a for a in areas if in_rect(*feet, a["roi"]) or in_rect(x + w / 2, y + h / 2, a["roi"])),
                        None)
            if area is None:
                continue  # fuera de las áreas vigiladas
            st = area_stats[area["id"]]
            st["persons"] += 1
            if h < min_h:
                people.append({"box": p["box"], "status": "lejos", "missing": [], "have": []})
                continue
            req = {**base_req, **area.get("require", {})}
            have = [i for i in PPE_ITEMS
                    if any(in_rect(g["box"][0] + g["box"][2] / 2, g["box"][1] + g["box"][3] / 2, _region(p["box"], i))
                           for g in gear if g["item"] == i)]
            missing = [i for i in PPE_ITEMS if req.get(i) and i not in have]
            if missing:
                st["violations"] += 1
                for i in missing:
                    st["missing"][i] = st["missing"].get(i, 0) + 1
            people.append({"box": p["box"], "status": "falta" if missing else "ok", "missing": missing, "have": have})

        confirm = float(c.get("confirm_ms", 1500)) / 1000
        hold = float(c.get("hold_ms", 2000)) / 1000
        alarm_level = c.get("level", "peligro")
        states, texts = [], []
        for a in areas:
            st = area_stats[a["id"]]
            key = (cid, a["id"])
            was = self._deb._on.get(key, False)
            active = self._deb.update(key, st["violations"] > 0, now, confirm, hold)
            if active and not was:
                self.counters["events"] = self.counters.get("events", 0) + 1
            miss = ", ".join(f"{ITEM_LABEL[i]} ({n})" for i, n in st["missing"].items())
            if active:
                where = "" if a["id"] == ALL else f" en {a['name']}"
                texts.append(f"{max(1, st['violations'])} persona(s) sin EPP{where}: {miss or 'equipo faltante'}")
            states.append({"id": a["id"], "name": a["name"], "camera": cid, "level": alarm_level if active else "ninguna",
                           "ok": not active, "persons": st["persons"], "violations": st["violations"],
                           "missing": st["missing"],
                           "text": f"FALTA EPP: {miss}" if st["violations"] else
                                   ("OK" if st["persons"] else "sin personas")})

        evaluated = [p for p in people if p["status"] != "lejos"]
        summary = {"persons": sum(s["persons"] for s in area_stats.values()), "evaluated": len(evaluated),
                   "ok": sum(1 for p in evaluated if not p["missing"]),
                   "violations": sum(1 for p in evaluated if p["missing"]),
                   **{f"no_{i}": sum(1 for p in evaluated if i in p["missing"]) for i in PPE_ITEMS}}
        return {"tools": states, "summary": summary, "level": worst(s["level"] for s in states),
                "text": " · ".join(texts), "people": people, "gear": gear, "areas": areas}

    def draw_tools(self, img, tools):
        for a in tools:
            if a.get("roi"):
                x, y, w, h = [int(v) for v in a["roi"]]
                cv2.rectangle(img, (x, y), (x + w, y + h), NEUTRAL, 2)
                put_label(img, a["name"], (x + 6, y + 24), NEUTRAL)

    def draw(self, img, res, tools):
        states = {s["id"]: s for s in res["tools"]}
        for a in res["areas"]:
            if a["id"] == ALL:
                continue
            x, y, w, h = [int(v) for v in a["roi"]]
            color = LEVEL_COLORS[states[a["id"]]["level"]] if a["id"] in states else NEUTRAL
            cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
            put_label(img, a["name"], (x + 6, y + 24), color)
        for g in res["gear"]:
            x, y, w, h = g["box"]
            cv2.rectangle(img, (x, y), (x + w, y + h), GEAR_COLOR, 1)
            put_label(img, f"{ITEM_LABEL[g['item']]} {g['conf']:.2f}", (x, max(12, y - 4)), GEAR_COLOR, 0.45)
        for p in res["people"]:
            x, y, w, h = p["box"]
            color = GRAY if p["status"] == "lejos" else LEVEL_COLORS["peligro"] if p["missing"] else LEVEL_COLORS["ninguna"]
            cv2.rectangle(img, (x, y), (x + w, y + h), color, 3)
            if p["status"] == "lejos":
                label = "muy lejos"
            elif p["missing"]:
                label = "SIN " + ", ".join(ITEM_LABEL[i] for i in p["missing"]).upper()
            else:
                label = "EPP OK"
            put_label(img, label, (x, max(16, y - 8)), color, 0.6)
        s = res["summary"]
        lv = res["level"]
        banner(img, f"EN VIVO   {'FALTA EPP' if lv != 'ninguna' else 'EPP OK'}   personas: {s['persons']}"
                    f"   sin EPP: {s['violations']}", LEVEL_COLORS[lv])

    def tcp_fields(self, s):
        m = s["summary"]
        return ["1" if s["level"] != "ninguna" else "0", str(m.get("persons", 0)), str(m.get("violations", 0)),
                str(m.get("no_helmet", 0)), str(m.get("no_gloves", 0)), str(m.get("no_goggles", 0))] + \
               ["1" if t["ok"] else "0" for t in s["tools"] if t["id"] != ALL]
