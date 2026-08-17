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

def test_daily_consumption_window():
    """El par daily_consumption_* debe respetar window_days >= min_days_in_window.

    Sin esta comprobación, el contador de días válidos nunca alcanzaría el mínimo
    y el consumo diario quedaría clavado en el fallback de config en silencio —
    el mismo footgun documentado para los otros tres pares.
    """
    from app.config import ChargingConfig
    cfg = ChargingConfig()
    assert cfg.daily_consumption_window_days == 30
    assert cfg.daily_consumption_min_days_in_window == 14
    print(f"  Defaults consumo diario: ventana {cfg.daily_consumption_window_days}d, "
          f"mín {cfg.daily_consumption_min_days_in_window}d ✓")
    try:
        ChargingConfig(daily_consumption_window_days=10,
                       daily_consumption_min_days_in_window=15)
        print("  ERROR: debería haber rechazado ventana < mínimo")
        raise SystemExit(1)
    except ValueError:
        print("  Ventana menor que el mínimo rechazada ✓")


if __name__ == "__main__":
    test_load()
    print()
    test_tariff()
    print()
    test_holidays_loaded()
    print()
    test_validation()
    print()
    test_daily_consumption_window()
