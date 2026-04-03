"""
Test rápido de config.py — ejecutar con:
  docker compose run --rm solar-manager python -m app.test_config
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import load_config


def test_load():
    cfg = load_config("config.yaml")
    print("Config cargada correctamente:")
    print(f"  Solcast resource_id : {cfg.solcast.resource_id}")
    print(f"  Inversor URL        : {cfg.inverter.web_url}")
    print(f"  Capacidad batería   : {cfg.installation.battery_capacity_kwh} kWh")
    print(f"  Consumo diario      : {cfg.installation.average_daily_consumption_kwh} kWh")
    print(f"  Risk factor         : {cfg.charging.risk_factor}")
    print(f"  Dry run             : {cfg.system.dry_run}")
    print(f"  Timezone            : {cfg.system.timezone}")
    print("OK")


def test_validation():
    from app.config import ChargingConfig
    try:
        ChargingConfig(min_soc_pct=80, max_soc_pct=50)
        print("ERROR: debería haber fallado la validación")
    except Exception as e:
        print(f"Validación funciona correctamente: {e}")


if __name__ == "__main__":
    test_load()
    test_validation()
