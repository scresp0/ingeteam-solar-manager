"""
test_charge_current.py — pruebas de la lógica del controlador de corriente de carga.

Prueba la función pura `main._compute_target_charge_current` (sin MODBUS, sin
forecast, sin Playwright): modos VALLE/SOLAR/IDLE, umbral de temperatura, gate de
"solar suficiente", mínimos/máximos y casos en los que NO se toca la corriente.

Ejecutar:
  PYTHONPATH=. python app/test_charge_current.py
  o en el contenedor:  docker exec -i solar-manager python -m app.test_charge_current
"""
import sys
from types import SimpleNamespace as NS

from app.config import ChargeCurrentConfig
from app.main import _compute_target_charge_current


def _cfg(**overrides):
    return NS(
        charge_current=ChargeCurrentConfig(**overrides),
        installation=NS(battery_capacity_kwh=22.5),
        charging=NS(max_soc_pct=95.0),
    )


def _state(soc, temp, v=50.0):
    return NS(soc_pct=soc, battery_temp_c=temp, battery_voltage_v=v)


def _sched(charge_needed, target=0):
    return NS(charge_needed=charge_needed, target_soc_pct=target)


# firma: _compute_target_charge_current(cfg, state, hour, sched, current, remaining_solar, solar_end)
def comp(cfg, state, hour, sched, current, remaining, solar_end):
    return _compute_target_charge_current(cfg, state, hour, sched, current, remaining, solar_end)


def main():
    cfg = _cfg()   # defaults: hot 30, floor 15, max 66, margin 1.2
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

    # amps_for(E,h) = clamp(round(E*1000/(50*h)*1.2), 15, 66)
    print("=== VALLE (carga de red 00:00–08:00) ===")
    # SOC50→95: E=10.125 kWh, 6h → 40A
    check("SOC50→95, quedan 6h", comp(cfg, _state(50, 24), 2.0, _sched(True, 95), 55, 0, 0), 40, "VALLE")
    check("déficit grande → 66", comp(cfg, _state(10, 24), 6.0, _sched(True, 100), 55, 0, 0), 66, "VALLE")
    check("déficit pequeño → suelo 15", comp(cfg, _state(90, 24), 1.0, _sched(True, 95), 55, 0, 0), 15, "VALLE")
    check("objetivo alcanzado → no tocar (deja 55)", comp(cfg, _state(95, 24), 2.0, _sched(True, 95), 55, 0, 0), 55, "sin cambios")
    check("valle SIN carga → no tocar (deja 55)", comp(cfg, _state(50, 24), 2.0, _sched(False), 55, 0, 0), 55, "sin cambios")

    print("=== SOLAR (08:00 – fin de producción) ===")
    # temp ≤ 30 → máx
    check("templada (≤30) → 66", comp(cfg, _state(70, 28), 12.0, None, 40, 10.0, 17.0), 66, "máx")
    # temp > 30 y solar suficiente: SOC70→95 E=5.625, 5h → 27A
    check("calor + solar suficiente → mín 27", comp(cfg, _state(70, 32), 12.0, None, 40, 10.0, 17.0), 27, "mín")
    # temp > 30 pero solar insuficiente → 66
    check("calor + solar insuficiente → 66", comp(cfg, _state(30, 32), 12.0, None, 40, 5.0, 17.0), 66, "solar restante")
    # temp > 30 pero forecast no disponible → 66
    check("calor + sin forecast → 66", comp(cfg, _state(70, 32), 12.0, None, 40, None, 17.0), 66, "no disponible")
    # batería llena → no tocar
    check("batería llena → no tocar (deja 40)", comp(cfg, _state(96, 32), 12.0, None, 40, 10.0, 17.0), 40, "sin cambios")
    # cerca del fin → sube a 66
    check("cerca del fin → 66", comp(cfg, _state(80, 32), 16.8, None, 40, 10.0, 17.0), 66, "mín")

    print("=== IDLE / fronteras ===")
    check("tarde tras fin solar → no tocar", comp(cfg, _state(80, 32), 20.0, None, 33, 0.0, 17.0), 33, "sin cambios")
    check("hora == fin solar → no tocar", comp(cfg, _state(70, 32), 17.0, None, 33, 10.0, 17.0), 33, "sin cambios")
    check("tensión 0 → fallback 50V (=caso VALLE base)", comp(cfg, _state(50, 24, 0.0), 2.0, _sched(True, 95), 55, 0, 0), 40, "VALLE")

    print("=== parámetros no-default (hot 35, floor 10, max 50) ===")
    cfg2 = _cfg(hot_threshold_c=35.0, floor_a=10, max_a=50)
    check("temp 32 ≤ 35 → máx 50", comp(cfg2, _state(70, 32), 12.0, None, 40, 10.0, 17.0), 50, "máx")
    check("temp 40 > 35 + solar ok → mín 27", comp(cfg2, _state(70, 40), 12.0, None, 40, 10.0, 17.0), 27, "mín")
    check("mín por encima de max_a=50 → 50", comp(cfg2, _state(10, 40), 12.0, None, 40, 25.0, 17.0), 50, "mín")

    print("=== SOLAR con puerta de temperatura DESACTIVADA (temp_gate_enabled=False) ===")
    cfg3 = _cfg(temp_gate_enabled=False)
    # batería fría (28 ≤ 30) pero solar suficiente → ahora carga suave (antes era 66)
    check("fría + solar suficiente → mín 27", comp(cfg3, _state(70, 28), 12.0, None, 40, 10.0, 17.0), 27, "solar suficiente")
    # la temperatura ya no influye: misma decisión esté fría o caliente
    check("caliente + solar suficiente → mín 27", comp(cfg3, _state(70, 40), 12.0, None, 40, 10.0, 17.0), 27, "solar suficiente")
    # solar insuficiente sigue forzando máx aunque esté fría
    check("fría + solar insuficiente → 66", comp(cfg3, _state(30, 28), 12.0, None, 40, 5.0, 17.0), 66, "solar restante")
    # sin forecast sigue forzando máx
    check("fría + sin forecast → 66", comp(cfg3, _state(70, 28), 12.0, None, 40, None, 17.0), 66, "no disponible")

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        sys.exit(1)
    print(f"✓ Todos los tests de control de corriente pasaron ({passed})")


if __name__ == "__main__":
    main()
