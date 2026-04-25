"""
test_decision.py — tests unitarios de decide_charge y decide_discharge.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_decision
"""
from datetime import date, datetime, timedelta
from unittest.mock import patch

from app.decision import (
    SolarForecast, DecisionInput,
    decide_charge, decide_discharge,
    charge_summary, discharge_summary,
    is_valley_day, reference_date,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEEKEND_DAYS = [5, 6]
HOLIDAYS = []

BASE = dict(
    battery_capacity_kwh=22.55,
    daily_consumption_kwh=16.0,
    night_consumption_kwh=3.5,
    risk_factor=0.7,
    min_soc_pct=30.0,
    max_soc_pct=100.0,
    safety_margin_kwh=1.0,
    weekend_days=WEEKEND_DAYS,
    holidays=HOLIDAYS,
)

FORECAST_SUNNY  = SolarForecast(p10=12.0, p50=18.0, p90=22.0)
FORECAST_CLOUDY = SolarForecast(p10=1.0,  p50=3.0,  p90=5.0)
FORECAST_MEDIUM = SolarForecast(p10=6.0,  p50=10.0, p90=14.0)
FORECAST_ZERO   = SolarForecast(p10=0.0,  p50=0.0,  p90=0.0)


def make_inp(soc, forecast_day1=None, forecast_day2=None, **kwargs):
    return DecisionInput(
        forecast_day1=forecast_day1 or FORECAST_MEDIUM,
        forecast_day2=forecast_day2 or FORECAST_MEDIUM,
        soc_actual_pct=soc,
        **{**BASE, **kwargs},
    )


def ok(msg):
    print(f"  ✓  {msg}")


def fail(msg):
    print(f"  ✗  {msg}")
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Tests de is_valley_day
# ---------------------------------------------------------------------------

print("\n=== is_valley_day ===")

lunes = date(2026, 4, 13)
assert lunes.weekday() == 0
assert not is_valley_day(lunes, WEEKEND_DAYS, [])
ok("lunes no es valle")

sabado = date(2026, 4, 11)
assert sabado.weekday() == 5
assert is_valley_day(sabado, WEEKEND_DAYS, [])
ok("sábado es valle")

domingo = date(2026, 4, 12)
assert is_valley_day(domingo, WEEKEND_DAYS, [])
ok("domingo es valle")

festivo = date(2026, 12, 25)
assert is_valley_day(festivo, WEEKEND_DAYS, ["2026-12-25"])
ok("festivo explícito es valle")

martes_festivo = date(2026, 12, 8)
assert not is_valley_day(martes_festivo, WEEKEND_DAYS, [])
assert is_valley_day(martes_festivo, WEEKEND_DAYS, ["2026-12-08"])
ok("festivo en martes solo es valle si está en la lista")


# ---------------------------------------------------------------------------
# Tests de reference_date
# ---------------------------------------------------------------------------

print("\n=== reference_date ===")

def mock_now(dt: datetime):
    """Parchea datetime.now() dentro de app.decision."""
    class _MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt
    return patch("app.decision.datetime", _MockDatetime)

with mock_now(datetime(2026, 4, 16, 23, 55)):
    assert reference_date(8) == date(2026, 4, 16)
    ok("23:55 del jueves → referencia = jueves")

with mock_now(datetime(2026, 4, 17, 0, 30)):
    assert reference_date(8) == date(2026, 4, 16)
    ok("00:30 del viernes → referencia = jueves (día anterior)")

with mock_now(datetime(2026, 4, 17, 7, 59)):
    assert reference_date(8) == date(2026, 4, 16)
    ok("07:59 → todavía madrugada, referencia = día anterior")

with mock_now(datetime(2026, 4, 17, 8, 0)):
    assert reference_date(8) == date(2026, 4, 17)
    ok("08:00 → ya es día actual, referencia = hoy")

with mock_now(datetime(2026, 4, 17, 5, 0)):
    assert reference_date(6) == date(2026, 4, 16)
    ok("05:00 con cutoff=6 → referencia = día anterior")

with mock_now(datetime(2026, 4, 17, 6, 0)):
    assert reference_date(6) == date(2026, 4, 17)
    ok("06:00 con cutoff=6 → referencia = hoy")


# ---------------------------------------------------------------------------
# Tests de decide_charge — mañana laborable
# ---------------------------------------------------------------------------

print("\n=== decide_charge — mañana laborable ===")

# _ref_date=domingo 12/04 → mañana=lunes 13/04 (laborable)
REF_DOM = date(2026, 4, 12)

inp = make_inp(97, forecast_day1=FORECAST_SUNNY)
r = decide_charge(inp, _ref_date=REF_DOM)
print(f"\n  Batería 97% + soleado: charge={r.charge_needed}")
assert not r.charge_needed
assert not r.valley_day_skip
ok("batería llena + soleado → no cargar")

inp = make_inp(20, forecast_day1=FORECAST_CLOUDY)
r = decide_charge(inp, _ref_date=REF_DOM)
print(f"  Batería 20% + nublado: charge={r.charge_needed}, target={r.target_soc_pct}%")
assert r.charge_needed
assert r.target_soc_pct > 20.0
assert not r.valley_day_skip
ok("batería baja + nublado → cargar")

inp = make_inp(5, forecast_day1=FORECAST_ZERO, max_soc_pct=50.0)
r = decide_charge(inp, _ref_date=REF_DOM)
print(f"  Batería 5% + zero + max=50%: target={r.target_soc_pct}%, clamped={r.clamped}")
assert r.charge_needed
assert r.target_soc_pct == 50.0
assert r.clamped
ok("clamped al max_soc=50%")

inp = make_inp(90, forecast_day1=FORECAST_SUNNY)
r = decide_charge(inp, _ref_date=REF_DOM)
print(f"  Batería 90% + soleado: charge={r.charge_needed}")
assert not r.charge_needed
ok("batería alta + soleado → no cargar")

inp = make_inp(80, forecast_day1=FORECAST_MEDIUM)
r = decide_charge(inp, _ref_date=REF_DOM)
print(f"  Batería 80% + medio: charge={r.charge_needed} (déficit esperado ~2 kWh)")
assert r.charge_needed
ok("batería 80% + medio → carga (déficit real ~2 kWh)")


# ---------------------------------------------------------------------------
# Tests de decide_charge — mañana es día valle
# ---------------------------------------------------------------------------

print("\n=== decide_charge — mañana es día valle ===")

# _ref_date=sábado 11/04 → mañana=domingo 12/04 (valle)
REF_SAB = date(2026, 4, 11)

inp = make_inp(5, forecast_day1=FORECAST_ZERO)
r = decide_charge(inp, _ref_date=REF_SAB)
print(f"\n  Batería 5% + nublado + domingo: charge={r.charge_needed}, skip={r.valley_day_skip}")
assert not r.charge_needed
assert r.valley_day_skip
ok("no carga si mañana es domingo aunque haya déficit")

inp = make_inp(5, forecast_day1=FORECAST_ZERO, holidays=["2026-04-12"])
r = decide_charge(inp, _ref_date=REF_SAB)
assert not r.charge_needed
assert r.valley_day_skip
ok("no carga si mañana es festivo")


# ---------------------------------------------------------------------------
# Tests de decide_charge — ejecución en madrugada (caso del bug original)
# ---------------------------------------------------------------------------

print("\n=== decide_charge — ejecución en madrugada ===")

# ref=jueves → mañana=viernes (laborable) → debe cargar
inp = make_inp(20, forecast_day1=FORECAST_CLOUDY)
r = decide_charge(inp, _ref_date=date(2026, 4, 16))  # jueves
print(f"\n  Ref=jueves (madrugada viernes) + batería baja: charge={r.charge_needed}, skip={r.valley_day_skip}")
assert r.charge_needed
assert not r.valley_day_skip
ok("ref=jueves → mañana=viernes laborable → carga")

# ref=viernes → mañana=sábado (valle) → no debe cargar
inp = make_inp(5, forecast_day1=FORECAST_ZERO)
r = decide_charge(inp, _ref_date=date(2026, 4, 17))  # viernes
print(f"  Ref=viernes (madrugada sábado) + batería baja: charge={r.charge_needed}, skip={r.valley_day_skip}")
assert not r.charge_needed
assert r.valley_day_skip
ok("ref=viernes → mañana=sábado valle → no carga")


# ---------------------------------------------------------------------------
# Tests de decide_discharge — mañana laborable
# ---------------------------------------------------------------------------

print("\n=== decide_discharge — mañana laborable ===")

# ref=domingo → mañana=lunes (laborable)
inp = make_inp(80)
d = decide_discharge(inp, _ref_date=date(2026, 4, 12))
print(f"\n  Mañana laborable: blocked={d.discharge_blocked}")
assert not d.discharge_blocked
ok("mañana laborable → descarga libre siempre")


# ---------------------------------------------------------------------------
# Tests de decide_discharge — mañana es día valle
# ---------------------------------------------------------------------------

print("\n=== decide_discharge — mañana es día valle ===")

# ref=sábado → mañana=domingo (valle)
REF_SAB = date(2026, 4, 11)

inp = make_inp(90, forecast_day1=FORECAST_SUNNY, forecast_day2=FORECAST_SUNNY)
d = decide_discharge(inp, _ref_date=REF_SAB)
print(f"\n  Batería 90% + soleado soleado: blocked={d.discharge_blocked}")
assert not d.discharge_blocked
ok("solar suficiente en 2 días → descarga libre")

inp = make_inp(40, forecast_day1=FORECAST_SUNNY, forecast_day2=FORECAST_CLOUDY)
d = decide_discharge(inp, _ref_date=REF_SAB)
print(f"  Batería 40% + soleado día1 + nublado día2: blocked={d.discharge_blocked}, deficit={d.deficit_day2_kwh}")
assert not d.discharge_blocked
assert d.deficit_day2_kwh > 0  # déficit calculado e informado, pero no se bloquea
ok("día valle → descarga libre aunque haya déficit en día 2 (info en log)")

inp = make_inp(70, forecast_day1=FORECAST_CLOUDY, forecast_day2=FORECAST_CLOUDY)
d = decide_discharge(inp, _ref_date=REF_SAB)
print(f"  Batería 70% + nublado nublado: blocked={d.discharge_blocked}, deficit={d.deficit_day2_kwh}")
assert not d.discharge_blocked
ok("día valle → descarga libre siempre (déficit se informa pero no bloquea)")

inp = make_inp(95, forecast_day1=FORECAST_SUNNY, forecast_day2=FORECAST_CLOUDY)
d = decide_discharge(inp, _ref_date=REF_SAB)
print(f"  Batería 95% + soleado día1 + nublado día2: blocked={d.discharge_blocked}, end_day1={d.energy_end_day1_kwh}")
ok(f"batería muy alta → {'bloquear' if d.discharge_blocked else 'descarga libre'} (deficit={d.deficit_day2_kwh} kWh)")


# ---------------------------------------------------------------------------
# Tests de decide_discharge — ejecución en madrugada
# ---------------------------------------------------------------------------

print("\n=== decide_discharge — ejecución en madrugada ===")

# ref=sábado → mañana=domingo (valle) → siempre descarga libre
inp = make_inp(40, forecast_day1=FORECAST_SUNNY, forecast_day2=FORECAST_CLOUDY)
d = decide_discharge(inp, _ref_date=date(2026, 4, 11))
print(f"\n  Ref=sábado + déficit día2: blocked={d.discharge_blocked}, deficit={d.deficit_day2_kwh}")
assert not d.discharge_blocked
ok("ref=sábado → mañana=domingo valle → descarga libre siempre")

# ref=domingo → mañana=lunes (laborable) → descarga libre
inp = make_inp(40, forecast_day1=FORECAST_SUNNY, forecast_day2=FORECAST_CLOUDY)
d = decide_discharge(inp, _ref_date=date(2026, 4, 12))
print(f"  Ref=domingo → mañana=lunes: blocked={d.discharge_blocked}")
assert not d.discharge_blocked
ok("ref=domingo → mañana=lunes laborable → descarga libre")


# ---------------------------------------------------------------------------
# Tests de charge_summary / discharge_summary
# ---------------------------------------------------------------------------

print("\n=== summaries con _ref_date ===")

# ref=jueves → mañana=viernes 17/04, pasado=sábado 18/04
REF_JUE = date(2026, 4, 16)

inp = make_inp(50, forecast_day1=FORECAST_MEDIUM, forecast_day2=FORECAST_MEDIUM)
r   = decide_charge(inp, _ref_date=REF_JUE)
s   = charge_summary(inp, r, _ref_date=REF_JUE)
assert "viernes" in s, f"Se esperaba 'viernes' en el resumen:\n{s}"
assert "17/04" in s
ok("charge_summary muestra viernes 17/04 cuando ref=jueves")

d = decide_discharge(inp, _ref_date=REF_JUE)
s = discharge_summary(inp, d, _ref_date=REF_JUE)
assert "viernes" in s
assert "sábado" in s
ok("discharge_summary muestra día1=viernes y día2=sábado")


# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------

print("\n✓ Todos los tests de decision.py pasaron correctamente\n")