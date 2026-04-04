"""
Test de automation.py — ejecutar con acceso a la red local del inversor:
  docker compose run --rm solar-manager python -m app.test_automation

Por defecto ejecuta en dry_run=True — navega y rellena pero NO pulsa Escribir.
Para escribir de verdad: DRY_RUN=false docker compose run --rm ...
"""
import logging
import sys
from app.config import load_config
from app.automation import set_charge_schedule, AutomationError

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)

def main():
    cfg = load_config("config.yaml")

    # El test siempre usa dry_run=True salvo que se fuerce con DRY_RUN=false
    dry_run = cfg.system.dry_run is False and \
              "--write" in sys.argv

    soc_test = 75.0   # SOC de prueba

    print(f"\nTest de automatización")
    print(f"  Inversor  : {cfg.inverter.web_url}")
    print(f"  SOC test  : {soc_test}%")
    print(f"  Dry run   : {not ('--write' in sys.argv)}")
    print()

    try:
        set_charge_schedule(
            cfg=cfg.inverter,
            charge_needed=True,       # siempre True en el test para probar la escritura
            target_soc_pct=soc_test,
            dry_run="--write" not in sys.argv,
        )
        print("\nOK")
    except AutomationError as e:
        print(f"\nError: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
