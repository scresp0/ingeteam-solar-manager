"""
inverter_map.py — guarda y compara el mapa de registros que publica el inversor
(`GET /inverter/map/1`), para saber si una actualización de firmware rompe algo.

Uso (stdlib pura, funciona en el host sin la imagen Docker):

    python3 scripts/inverter_map.py save [--out FICHERO]
    python3 scripts/inverter_map.py diff ANTES.json [DESPUES.json]

`save` descarga el mapa y lo guarda en `docs/inverter-maps/` con fecha y versiones
de firmware (DSP y demás de `compatible`) en el nombre. `diff` compara dos mapas;
sin DESPUES.json, descarga el mapa en vivo.

El diff tiene dos partes:
  1. CRÍTICOS — lo que lee o busca solar-manager: los registros MODBUS de
     `app/inverter.py`, los campos del datalogger SDO de `logger_reader._SDO_FIELDS`
     y las etiquetas web de `automation.py` (perfil por defecto, SOC Grid, corriente).
     Las etiquetas y los campos SDO se extraen del código fuente con `ast`, así que no
     se desincronizan. Exit code 1 si alguno cambia o desaparece.
  2. Resto — altas, bajas y cambios del mapa completo, solo informativo.

Credenciales: INVERTER_WEB_URL / INVERTER_USERNAME / INVERTER_PASSWORD del entorno
o, si no están, del `.env` de la raíz del repo.
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAPS_DIR = REPO / "docs" / "inverter-maps"

# Registros MODBUS que lee app/inverter.py: tid del mapa → (sección, dirección base-0, nombre[, cat]).
# `cat` desambigua los holding cuyo tid se repite (L-242 existe en 1.1 plomo-ácido y en 1.2 BMS).
# Mantener en sincronía con inverter.py (las direcciones están como literales en sus lecturas).
MODBUS_REGISTERS = {
    "L-114": ("online", 15, "30016 Estado inversor"),
    "L-128": ("online", 17, "30018 Tensión batería"),
    "L-133": ("online", 19, "30020 Potencia batería"),
    "L-135": ("online", 20, "30021 SOC"),
    "L-137": ("online", 21, "30022 SOH"),
    "L-142": ("online", 26, "30027 Estado batería"),
    "L-152": ("online", 27, "30028 Temperatura batería"),
    "L-165": ("online", 33, "30034 FV 1 potencia"),
    "L-168": ("online", 36, "30037 FV 2 potencia"),
    "L-660": ("online", 71, "30072 Vatímetro externo (PacMeter)"),
    "L-709": ("online", 78, "30079 Cargas totales"),
    "L-242": ("holding", 86, "40087 Corriente máxima de carga", "1.2- BMS Battery Settings"),
    "L-462": ("holding", 125, "40126 SOC mínimo", "6.3- Self Consumption Settings"),
    "L-642": ("holding", 131, "40132 Capacidad nominal", "1.2- BMS Battery Settings"),
}
# Atributos de un registro cuyo cambio altera el valor leído.
VALUE_ATTRS = ("ty", "sc", "of", "start", "len")


# ---------------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------------

def _credentials() -> tuple[str, str, str]:
    env = dict(os.environ)
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'\""))
    try:
        return env["INVERTER_WEB_URL"].rstrip("/"), env["INVERTER_USERNAME"], env["INVERTER_PASSWORD"]
    except KeyError as e:
        sys.exit(f"Falta {e.args[0]} en el entorno o en .env")


def fetch_map() -> dict:
    url, user, password = _credentials()
    req = urllib.request.Request(f"{url}/inverter/map/1")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _firmwares(m: dict) -> list[str]:
    return [c.get("firmware", "?") for c in m.get("compatible", [])]


def cmd_save(args) -> int:
    m = fetch_map()
    if args.out:
        out = Path(args.out)
    else:
        MAPS_DIR.mkdir(parents=True, exist_ok=True)
        out = MAPS_DIR / f"map_{date.today():%Y%m%d}_{'_'.join(_firmwares(m))}.json"
    out.write_text(json.dumps(m, ensure_ascii=False, indent=1))
    print(f"Guardado {out} ({out.stat().st_size // 1024} KB) — firmwares: {', '.join(_firmwares(m))}")
    return 0


# ---------------------------------------------------------------------------
# Lo que usa el código (extraído con ast, sin importar los módulos)
# ---------------------------------------------------------------------------

def _module_ast(rel: str) -> ast.Module:
    return ast.parse((REPO / rel).read_text())


def sdo_fields() -> dict[str, str]:
    for node in _module_ast("app/logger_reader.py").body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "_SDO_FIELDS" for t in node.targets):
            return ast.literal_eval(node.value)
    sys.exit("No encuentro _SDO_FIELDS en app/logger_reader.py")


def web_labels() -> dict[str, str]:
    """Etiquetas que Playwright busca en la web: LABEL_* y las del perfil por defecto."""
    tree = _module_ast("app/automation.py")
    labels, profiles, default = {}, {}, None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) == "LabelProfile":
            profiles[name] = {kw.arg: ast.literal_eval(kw.value) for kw in node.value.keywords}
        elif name == "DEFAULT_PROFILE":
            default = node.value.id
        elif name.startswith("LABEL_") and isinstance(node.value, ast.Constant):
            labels[name] = node.value.value
    for field, text in profiles[default].items():
        if field != "name":
            labels[f"{default}.{field}"] = text
    # Menú y fila de la web de actualización: no son textos del mapa, no se pueden comprobar aquí.
    labels.pop("LABEL_MENU_UPDATE", None)
    labels.pop("LABEL_ROW_FIRMWARE", None)
    return labels


# ---------------------------------------------------------------------------
# Comparación
# ---------------------------------------------------------------------------

def _index(entries: list[dict], key) -> dict:
    """Indexa por clave; las repetidas (holding repite tids por programación) llevan nº de aparición."""
    seen, out = Counter(), {}
    for e in entries:
        k = key(e)
        out[(k, seen[k])] = e
        seen[k] += 1
    return out


def _sections(m: dict) -> dict[str, dict]:
    return {
        "online": _index(m.get("online", []), lambda e: e["tid"]),
        "holding": _index(m.get("holding", []), lambda e: (e.get("cat"), e["tid"])),
        "sdodata": _index(m.get("sdodata", []), lambda e: e["tid"]),
        "loggermap": _index(m.get("loggermap", {}).get("online", []), lambda e: e["name"]),
    }


def _find(m: dict, section: str, tid: str, cat: str | None = None) -> dict | None:
    return next((e for e in m.get(section, []) if e["tid"] == tid and cat in (None, e.get("cat"))), None)


def _value_diff(a: dict, b: dict, attrs) -> list[str]:
    return [f"{k}: {a.get(k)} → {b.get(k)}" for k in attrs if a.get(k) != b.get(k)]


def check_critical(old: dict, new: dict) -> list[str]:
    problems = []
    lang_old = old.get("langs", {}).get("SPANISH", {})
    lang_new = new.get("langs", {}).get("SPANISH", {})

    print("== MODBUS (app/inverter.py) ==")
    for tid, (section, addr, desc, *cat) in MODBUS_REGISTERS.items():
        cat = cat[0] if cat else None
        a, b = _find(old, section, tid, cat), _find(new, section, tid, cat)
        if b is None:
            msg = "DESAPARECIDO del mapa"
        elif b["add"] != addr:
            msg = f"MOVIDO: dirección {addr} → {b['add']} (el código sigue leyendo {addr})"
        elif a is not None and (d := _value_diff(a, b, VALUE_ATTRS)):
            msg = "FORMATO CAMBIADO: " + "; ".join(d)
        elif lang_old.get(tid) != lang_new.get(tid):
            msg = f"NOMBRE CAMBIADO: {lang_old.get(tid)!r} → {lang_new.get(tid)!r}"
        else:
            print(f"  OK  {desc} [{tid} @ {addr}]")
            continue
        problems.append(f"{desc}: {msg}")
        print(f"  !!  {desc} [{tid}]: {msg}")

    print("== Datalogger SDO (logger_reader._SDO_FIELDS) ==")
    for tid, name in sdo_fields().items():
        a, b = _find(old, "sdodata", tid), _find(new, "sdodata", tid)
        if b is None:
            msg = "DESAPARECIDO del sdodata"
        elif a is not None and (d := _value_diff(a, b, ("off",) + VALUE_ATTRS)):
            msg = "CAMBIADO: " + "; ".join(d)
        else:
            print(f"  OK  {name} [{tid}]")
            continue
        problems.append(f"SDO {name}: {msg}")
        print(f"  !!  {name} [{tid}]: {msg}")

    print("== Etiquetas web (automation.py, perfil por defecto) ==")
    texts = set(lang_new.values())
    for const, label in web_labels().items():
        # Coincidencia exacta o como prefijo (la web añade "(A)" a la corriente, por ejemplo).
        if label in texts or any(t.startswith(label) for t in texts):
            print(f"  OK  {const}: {label!r}")
        else:
            problems.append(f"Etiqueta {const} {label!r} no aparece en langs")
            print(f"  !!  {const}: {label!r} NO aparece en los textos del mapa")
    return problems


def report_rest(old: dict, new: dict) -> None:
    print("== Firmwares (compatible) ==")
    print(f"  {', '.join(_firmwares(old))} → {', '.join(_firmwares(new))}")
    so, sn = _sections(old), _sections(new)
    lang = new.get("langs", {}).get("SPANISH", {}) or old.get("langs", {}).get("SPANISH", {})
    for section in so:
        a, b = so[section], sn[section]
        added = [k for k in b if k not in a]
        removed = [k for k in a if k not in b]
        changed = [(k, d) for k in a if k in b and (d := _value_diff(a[k], b[k], ("add", "off") + VALUE_ATTRS))]
        print(f"== {section}: +{len(added)} −{len(removed)} ~{len(changed)} ==")

        def label(k):
            tid = k[0][1] if isinstance(k[0], tuple) else k[0]
            return f"{k[0]} {lang.get(tid, '')}".rstrip()
        for k in added:
            print(f"  +  {label(k)}")
        for k in removed:
            print(f"  −  {label(k)}")
        for k, d in changed:
            print(f"  ~  {label(k)}: {'; '.join(d)}")

    lo, ln = old.get("langs", {}).get("SPANISH", {}), new.get("langs", {}).get("SPANISH", {})
    renamed = [k for k in lo if k in ln and lo[k] != ln[k]]
    print(f"== langs (SPANISH): +{len(set(ln) - set(lo))} −{len(set(lo) - set(ln))} ~{len(renamed)} ==")
    for k in renamed:
        print(f"  ~  {k}: {lo[k]!r} → {ln[k]!r}")


def cmd_diff(args) -> int:
    old = json.loads(Path(args.before).read_text())
    new = json.loads(Path(args.after).read_text()) if args.after else fetch_map()
    problems = check_critical(old, new)
    print()
    report_rest(old, new)
    print()
    if problems:
        print(f"RESULTADO: {len(problems)} cambio(s) en lo que usa solar-manager:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("RESULTADO: nada de lo que usa solar-manager ha cambiado en el mapa.")
    print("(Navegación web, botones Leer/Escribir y menú Actualización no están en el mapa: "
          "comprobarlos con diag_automation.)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save", help="descarga y guarda el mapa actual")
    s.add_argument("--out", help="fichero de salida (por defecto docs/inverter-maps/)")
    d = sub.add_parser("diff", help="compara dos mapas (o uno con el inversor en vivo)")
    d.add_argument("before")
    d.add_argument("after", nargs="?")
    args = p.parse_args()
    return cmd_save(args) if args.cmd == "save" else cmd_diff(args)


if __name__ == "__main__":
    sys.exit(main())
