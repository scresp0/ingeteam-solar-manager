"""
Test de config.py — ejecutar con:
  docker compose run --rm solar-manager python -m app.test_config
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import load_config


def test_load():
    cfg = load_config("config.yaml")
    print("Config cargada correctamente:")
    print(f"  Solcast resource_id : {cfg.solcast.resource_id}")
    print(f"  Inversor URL        : {cfg.inverter.web_url}")
    print(f"  MODBUS host         : {cfg.inverter.get_modbus_host()}")
    print(f"  Capacidad batería   : {cfg.installation.battery_capacity_kwh} kWh")
    print(f"  Consumo diario      : {cfg.installation.average_daily_consumption_kwh} kWh")
    print(f"  Risk factor         : {cfg.charging.risk_factor}")
    print(f"  Dry run             : {cfg.system.dry_run}")
    print(f"  Timezone            : {cfg.system.timezone}")
    print("OK")


def test_tariff():
    cfg = load_config("config.yaml")
    t = cfg.tariff

    # Lunes laborable
    lunes = date(2025, 1, 6)
    assert not t.is_valley_day(lunes)
    intervals = t.get_valley_intervals(lunes)
    assert len(intervals) == 1
    assert intervals[0].start == "00:00" and intervals[0].end == "08:00"
    print(f"  Lunes: valle {intervals[0].start}-{intervals[0].end} ✓")

    # Sábado
    sabado = date(2025, 1, 11)
    assert t.is_valley_day(sabado)
    intervals = t.get_valley_intervals(sabado)
    assert len(intervals) == 1
    assert intervals[0].start == "00:00" and intervals[0].end == "24:00"
    print(f"  Sábado: todo valle {intervals[0].start}-{intervals[0].end} ✓")

    # Festivo configurado
    cfg.tariff.holidays = ["2025-12-25"]
    navidad = date(2025, 12, 25)
    assert t.is_valley_day(navidad)
    print(f"  Navidad (festivo): todo valle ✓")

    print("Tarifas OK")

def test_holidays_loaded():
    cfg = load_config("config.yaml")
    holidays = cfg.tariff.holidays
    assert isinstance(holidays, list)
    assert len(holidays) > 0
    assert all(isinstance(h, str) for h in holidays)
    print(f"  Festivos 2026 cargados correctamente: {len(holidays)} ✓")

def test_validation():
    from app.config import ChargingConfig
    try:
        ChargingConfig(min_soc_pct=80, max_soc_pct=50)
        print("ERROR: debería haber fallado la validación")
    except Exception as e:
        print(f"  Validación funciona correctamente ✓")


if __name__ == "__main__":
    test_load()
    print()
    test_tariff()
    print()
    test_holidays_loaded()
    print()
    test_validation()
