"""
test_logger_reader.py — pruebas del cálculo de acumulados del datalogger.

Cubre sobre todo la distinción que costó un bug real (v1.73): `PacGrid` es la salida
AC del inversor (exportación incluida), NO el consumo de la vivienda. El consumo es
`PacGrid + PacMeter`.

Ejecutar:
  PYTHONPATH=. python app/test_logger_reader.py
  o en el contenedor:  docker exec -i solar-manager python -m app.test_logger_reader
"""
import sys
from datetime import date

from app.logger_reader import house_power_w, _calculate_stats


def _rec(pacgrid=0, pacmeter=0, pdc1=0, pdc2=0, sbatt=50):
    return {"PacGrid": pacgrid, "PacMeter": pacmeter, "Pdc1": pdc1, "Pdc2": pdc2,
            "Sbatt": sbatt, "EPvToGrid": 0}


def main():
    passed = failed = 0

    def check(desc, got, exp, tol=0.001):
        nonlocal passed, failed
        ok = abs(got - exp) <= tol if isinstance(exp, float) else got == exp
        if ok:
            passed += 1
            print(f"  ✓  {desc} → {got}")
        else:
            failed += 1
            print(f"  ✗  {desc} → {got}  (esperado {exp})")

    print("=== house_power_w: casa = PacGrid + PacMeter ===")
    # Caso real medido 2026-08-16 12:00 — mediodía exportando.
    # Con PacGrid a secas daría 1468 W: 3.4x el consumo real.
    check("mediodía exportando", house_power_w(_rec(1467.6, -1033.8)), 433.8)
    # Caso real medido 2026-08-16 00:00 — batería en min_soc, la casa tira de red.
    # El inversor no entrega nada; con PacGrid a secas el consumo sale NEGATIVO.
    check("noche, casa desde red", house_power_w(_rec(-23.0, 538.0)), 515.0)
    # Sin flujo de red: la casa consume justo lo que entrega el inversor.
    check("sin flujo de red", house_power_w(_rec(1483.0, 0.0)), 1483.0)
    # Suelo en 0: desincronización puntual entre las dos medidas.
    check("suelo en 0 ante lectura incoherente", house_power_w(_rec(-100.0, 20.0)), 0.0)

    print("=== _calculate_stats: acumulados y perfil de 30 min ===")
    # Día sintético de 1440 min: 8 h de noche importando 600 W, luego 16 h
    # produciendo 3000 W con la casa a 1200 W y el resto exportado.
    night = [_rec(pacgrid=0, pacmeter=600)] * 480
    day = [_rec(pacgrid=3000, pacmeter=-1800, pdc1=1500, pdc2=1500)] * 960
    stats = _calculate_stats(night + day, date(2026, 8, 16), "TEST")

    # Noche: 600 W × 8 h = 4.8 kWh. Con ∫PacGrid daría 0.0 (el bug corregido).
    check("night_consumption_kwh", stats.night_consumption_kwh, 4.8)
    # Día: 1200 W × 16 h = 19.2 kWh, más los 4.8 de la noche.
    check("consumption_kwh", stats.consumption_kwh, 24.0)
    # Solar: 3000 W × 16 h = 48 kWh.
    check("solar_kwh", stats.solar_kwh, 48.0)
    check("grid_consumed_kwh", stats.grid_consumed_kwh, 4.8)

    # Los perfiles de 30 min deben sumar exactamente los agregados diarios.
    check("48 slots de casa", len(stats.half_hour_house_kwh), 48)
    check("Σ half_hour_house == consumption", sum(stats.half_hour_house_kwh), 24.0, 0.01)
    check("Σ half_hour_solar == solar", sum(stats.half_hour_solar_kwh), 48.0, 0.01)
    check("Σ half_hour_import == grid_consumed", sum(stats.half_hour_grid_import_kwh), 4.8, 0.01)
    # Export: 1800 W × 16 h = 28.8 kWh (de PacMeter, no del contador EPvToGrid).
    check("Σ half_hour_export", sum(stats.half_hour_grid_export_kwh), 28.8, 0.01)

    # Slot 0 (00:00–00:30) es noche: casa 600 W × 0.5 h = 0.3 kWh, sin solar.
    check("slot 0 casa (noche)", stats.half_hour_house_kwh[0], 0.3)
    check("slot 0 solar (noche)", stats.half_hour_solar_kwh[0], 0.0)
    # Slot 16 (08:00–08:30) ya es día: casa 1200 W × 0.5 h = 0.6 kWh.
    check("slot 16 casa (día)", stats.half_hour_house_kwh[16], 0.6)
    check("slot 16 solar (día)", stats.half_hour_solar_kwh[16], 1.5)

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        sys.exit(1)
    print(f"✓ Todos los tests de logger_reader pasaron ({passed})")


if __name__ == "__main__":
    main()
