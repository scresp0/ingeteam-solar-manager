"""
test_automation.py — test de automation.py con acceso a la red local del inversor.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_automation

Por defecto ejecuta en dry_run=True — navega y rellena pero NO pulsa Escribir.
Para escribir de verdad: añadir --write
"""
import logging
import sys
from app.config import load_config
from app.automation import set_charge_schedule, set_discharge_schedule, AutomationError

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)


def main():
    cfg = load_config("config.yaml")
    dry_run = "--write" not in sys.argv
    soc_test = 75.0

    print(f"\nTest de automatización")
    print(f"  Inversor  : {cfg.inverter.web_url}")
    print(f"  SOC test  : {soc_test}%")
    print(f"  Dry run   : {dry_run}")
    print()

    # --- Test 6.3.1: carga ---
    print("── Test 6.3.1 (carga) ──")
    try:
        set_charge_schedule(
            cfg=cfg.inverter,
            charge_needed=True,
            target_soc_pct=soc_test,
            dry_run=dry_run,
        )
        print("  6.3.1 OK\n")
    except AutomationError as e:
        print(f"  6.3.1 ERROR: {e}")
        sys.exit(1)

    # --- Test 6.3.2: descarga bloqueada ---
    print("── Test 6.3.2 (bloquear descarga) ──")
    try:
        set_discharge_schedule(
            cfg=cfg.inverter,
            discharge_blocked=True,
            dry_run=dry_run,
        )
        print("  6.3.2 OK\n")
    except AutomationError as e:
        print(f"  6.3.2 ERROR: {e}")
        sys.exit(1)

    print("OK — ambas pantallas configuradas correctamente")


if __name__ == "__main__":
    main()
