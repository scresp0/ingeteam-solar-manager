"""
test_automation.py — test de automation.py con acceso a la red local del inversor.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_automation [opciones]

Opciones:
  (sin flags)   lee el estado actual del inversor (6.3.1 y 6.3.2) — solo lectura
  --write       ejecuta además los tests de escritura en dry_run (navega, rellena, no guarda)
  --write --go  ejecuta los tests de escritura aplicando cambios reales al inversor
"""
import logging
import sys
from app.config import load_config
from app.automation import (
    read_inverter_schedule,
    set_charge_schedule,
    set_discharge_schedule,
    AutomationError,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)


def main():
    cfg = load_config("config.yaml")
    do_write = "--write" in sys.argv
    do_real  = "--go" in sys.argv and do_write
    dry_run  = not do_real
    soc_test = 75.0

    print(f"\nTest de automatización")
    print(f"  Inversor : {cfg.inverter.web_url}")
    print()

    # ── Lectura del estado actual (siempre) ────────────────────────────────
    print("── Leyendo estado actual del inversor (6.3.1 y 6.3.2) ──")
    state = read_inverter_schedule(cfg.inverter)
    if state is None:
        print("  ERROR: no se pudo leer la programación del inversor")
        sys.exit(1)

    print(f"  6.3.1 Carga    : {state.charge_str()}")
    print(f"  6.3.2 Descarga : {state.discharge_str()}")
    print()

    if not do_write:
        print("(Modo solo lectura — añade --write para probar la escritura)")
        return

    # ── Tests de escritura ─────────────────────────────────────────────────
    print(f"── Tests de escritura (dry_run={dry_run}, SOC test={soc_test}%) ──")

    print("\n  Test 6.3.1 (carga activa)...")
    try:
        set_charge_schedule(
            cfg=cfg.inverter,
            charge_needed=True,
            target_soc_pct=soc_test,
            dry_run=dry_run,
        )
        print("  6.3.1 OK")
    except AutomationError as e:
        print(f"  6.3.1 ERROR: {e}")
        sys.exit(1)

    print("\n  Test 6.3.2 (bloquear descarga)...")
    try:
        set_discharge_schedule(
            cfg=cfg.inverter,
            discharge_blocked=True,
            dry_run=dry_run,
        )
        print("  6.3.2 OK")
    except AutomationError as e:
        print(f"  6.3.2 ERROR: {e}")
        sys.exit(1)

    print("\nOK — ambas pantallas procesadas correctamente")


if __name__ == "__main__":
    main()
