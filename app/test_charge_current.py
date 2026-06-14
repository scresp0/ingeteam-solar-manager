"""
test_charge_current.py — pruebas de la lógica del controlador de corriente de carga.

Prueba la función pura `main._compute_target_charge_current` (sin MODBUS ni
Playwright): modos VALLE/SOLAR/IDLE, puerta de temperatura, mínimo (floor),
máximo (clamp), fallback de tensión y casos límite.

Ejecutar:
  PYTHONPATH=. python app/test_charge_current.py
  o en el contenedor:  docker exec -i solar-manager python -m app.test_charge_current
"""
import sys
from types import SimpleNamespace as NS

from app.config import ChargeCurrentConfig
from app.main import _compute_target_charge_current


def _cfg(**overrides):
    """cfg mínimo con un ChargeCurrentConfig real + installation/charging simulados."""
    return NS(
        charge_current=ChargeCurrentConfig(**overrides),
        installation=NS(battery_capacity_kwh=22.5),
        charging=NS(max_soc_pct=95.0),
    )


def _state(soc, temp, v=50.0):
    return NS(soc_pct=soc, battery_temp_c=temp, battery_voltage_v=v)


def _sched(charge_needed, target=0):
    return NS(charge_needed=charge_needed, target_soc_pct=target)


def main():
    cfg = _cfg()   # defaults: floor 15, max 66, night_default 66, margin 1.2, cold 26
    passed = failed = 0

    def check(desc, got, exp_amps=None, mode_has=None):
        nonlocal passed, failed
        amps, mode = got
        ok = (exp_amps is None or amps == exp_amps) and (mode_has is None or mode_has in mode)
        if ok:
            passed += 1
            print(f"  ✓  {desc} → {amps}A [{mode}]")
        else:
            failed += 1
            print(f"  ✗  {desc} → {amps}A [{mode}]  (esperado {exp_amps}A / modo~{mode_has!r})")

    # E=(target-soc)/100*22.5 ; I=round(E*1000/(V*horas)*1.2), clamp [floor,max]
    print("=== VALLE (carga de red, 00:00–08:00) ===")
    # SOC50→95: E=10.125 kWh, 6h, 50V → 10125/(50*6)*1.2 = 40.5 → round 40
    check("SOC50→95, quedan 6h", _compute_target_charge_current(cfg, _state(50, 24), 2.0, _sched(True, 95), 17.0), 40, "VALLE")
    # déficit grande: E=20.25, 2h → 243A → clamp 66
    check("déficit grande → tope 66", _compute_target_charge_current(cfg, _state(10, 24), 6.0, _sched(True, 100), 17.0), 66, "VALLE")
    # déficit mínimo: E=1.125, 7h → ~4A → suelo 15
    check("déficit pequeño → suelo 15", _compute_target_charge_current(cfg, _state(90, 24), 1.0, _sched(True, 95), 17.0), 15, "VALLE")
    check("objetivo ya alcanzado → idle", _compute_target_charge_current(cfg, _state(95, 24), 2.0, _sched(True, 95), 17.0), 66, "objetivo")
    check("target_soc=0 usa max_soc", _compute_target_charge_current(cfg, _state(50, 24), 2.0, _sched(True, 0), 17.0), 40, "VALLE")
    check("valle sin carga → idle", _compute_target_charge_current(cfg, _state(50, 24), 2.0, _sched(False), 17.0), 66, "IDLE")
    check("valle sin estado previo → idle", _compute_target_charge_current(cfg, _state(50, 24), 2.0, None, 17.0), 66, "IDLE")
    check("tensión 0 → fallback 50V (=caso base)", _compute_target_charge_current(cfg, _state(50, 24, 0.0), 2.0, _sched(True, 95), 17.0), 40, "VALLE")

    print("=== SOLAR (carga fotovoltaica, 08:00–T_fin) ===")
    check("batería fría (temp<26) → tope 66", _compute_target_charge_current(cfg, _state(70, 24), 12.0, None, 17.0), 66, "fría")
    # SOC70→95: E=5.625, 5h → 27A
    check("cálida SOC70, quedan 5h → 27A", _compute_target_charge_current(cfg, _state(70, 30), 12.0, None, 17.0), 27, "SOLAR")
    check("batería llena → idle 66", _compute_target_charge_current(cfg, _state(96, 30), 12.0, None, 17.0), 66, "llena")
    # cerca de T_fin: 0.5h → sube a 66 (autocorrección)
    check("cerca de T_fin → sube a 66", _compute_target_charge_current(cfg, _state(80, 30), 16.5, None, 17.0), 66, "SOLAR")
    # inicio de ventana, 9h holgadas: E=5.625 → 15A exactos → suelo
    check("inicio ventana holgado → suelo 15", _compute_target_charge_current(cfg, _state(70, 30), 8.0, None, 17.0), 15, "SOLAR")

    print("=== IDLE / fronteras ===")
    check("tarde (20h) → idle 66", _compute_target_charge_current(cfg, _state(80, 30), 20.0, None, 17.0), 66, "IDLE")
    check("hora = T_fin (no productivo) → idle", _compute_target_charge_current(cfg, _state(70, 30), 17.0, None, 17.0), 66, "IDLE")

    print("=== parámetros no-default ===")
    cfg2 = _cfg(cold_threshold_c=20.0, floor_a=10, max_a=50, night_default_a=50)
    check("umbral frío 20: temp24 ya no es fría → calcula 27", _compute_target_charge_current(cfg2, _state(70, 24), 12.0, None, 17.0), 27, "SOLAR")
    check("max_a=50 limita el tope", _compute_target_charge_current(cfg2, _state(10, 30), 6.0, _sched(True, 100), 17.0), 50, "VALLE")
    check("night_default=50 en idle", _compute_target_charge_current(cfg2, _state(80, 30), 20.0, None, 17.0), 50, "IDLE")

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        sys.exit(1)
    print(f"✓ Todos los tests de control de corriente pasaron ({passed})")


if __name__ == "__main__":
    main()
