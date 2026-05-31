#!/usr/bin/env python3
"""
migrate_config.py — renombra las claves obsoletas de config.yaml al formato nuevo.

Renombra (solo el nombre de la clave, conservando valor, comentario en línea,
indentación y el resto del fichero intacto):

    night_consumption_min_days  ->  night_consumption_min_days_in_window
    risk_factor_min_days        ->  risk_factor_min_days_in_window
    solar_bias_min_days         ->  solar_bias_min_days_in_window

No es imprescindible: el modelo acepta los nombres antiguos vía alias y la
pestaña Configuración los migra al guardar. Este script es la vía por CLI para
limpiar el YAML de una vez (p. ej. en producción).

Uso:
    python scripts/migrate_config.py [ruta/config.yaml] [--dry-run]

Sin argumento de ruta usa $CONFIG_PATH, luego /app/config.yaml, luego ./config.yaml.
Hace una copia de seguridad <fichero>.bak antes de escribir (salvo --dry-run).
Es idempotente: si no hay claves obsoletas, no toca nada.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Fuente de verdad: app.config.DEPRECATED_CONFIG_KEYS. Se importa si las deps
# están disponibles; si no (script lanzado con Python pelado), se usa esta copia.
_FALLBACK = {
    "night_consumption_min_days": "night_consumption_min_days_in_window",
    "risk_factor_min_days":       "risk_factor_min_days_in_window",
    "solar_bias_min_days":        "solar_bias_min_days_in_window",
}


def _rename_map() -> dict[str, str]:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.config import DEPRECATED_CONFIG_KEYS  # type: ignore
        return {old: new for (_section, old), new in DEPRECATED_CONFIG_KEYS.items()}
    except Exception:
        return dict(_FALLBACK)


def _resolve_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    if env := os.environ.get("CONFIG_PATH"):
        return Path(env)
    for cand in (Path("/app/config.yaml"), Path("config.yaml")):
        if cand.exists():
            return cand
    raise FileNotFoundError("No se encontró config.yaml (pasa la ruta como argumento).")


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    path_args = [a for a in args if not a.startswith("--")]

    try:
        path = _resolve_path(path_args[0] if path_args else None)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not path.exists():
        print(f"ERROR: no existe {path}", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    renames = _rename_map()

    changed: list[tuple[str, str]] = []
    skipped: list[str] = []
    out = text
    for old, new in renames.items():
        # ¿ya existe la clave nueva? entonces hay ambas → no tocar, avisar.
        if re.search(rf"(?m)^[ \t]*{re.escape(new)}[ \t]*:", out):
            if re.search(rf"(?m)^[ \t]*{re.escape(old)}[ \t]*:", out):
                skipped.append(old)
            continue
        # renombrar solo la clave al inicio de línea (no toca comentarios ni el
        # nombre nuevo, que lleva '_in_window' entre 'days' y los dos puntos).
        pattern = re.compile(rf"(?m)^([ \t]*){re.escape(old)}([ \t]*:)")
        out, n = pattern.subn(rf"\g<1>{new}\g<2>", out)
        if n:
            changed.append((old, new))

    if skipped:
        print("AVISO: estas claves existen con el nombre viejo Y el nuevo a la vez;")
        print("       elimina manualmente la obsoleta para evitar duplicados:")
        for k in skipped:
            print(f"       - {k}")

    if not changed:
        print(f"Nada que migrar en {path} (sin claves obsoletas pendientes).")
        return 0

    print(("[DRY RUN] " if dry_run else "") + f"Renombrando en {path}:")
    for old, new in changed:
        print(f"  {old}  ->  {new}")

    if dry_run:
        print("\n(--dry-run: no se ha escrito nada)")
        return 0

    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(text, encoding="utf-8")
    path.write_text(out, encoding="utf-8")
    print(f"\nHecho. Copia de seguridad en {backup}")
    print("Reinicia el contenedor para aplicar (make restart).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
