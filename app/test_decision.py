"""
test_decision.py — tests unitarios de decide_charge y decide_discharge.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_decision
"""
from datetime import date, timedelta
from unittest.mock import patch

from app.decision import (
    SolarForecast, DecisionInput,
    decide_charge, decide_discharge,
    charge_summary, discharge_summary,
    is_valley_day,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEEKEND_DAYS = [5, 6]  # sábado, domingo
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

# Lunes = 0, no es valle
lunes = date(2026, 4, 13)
assert lunes.weekday() == 0
assert not is_valley_day(lunes, WEEKEND_DAYS, [])
ok("lunes no es valle")

# Sábado = 5, es valle
sabado = date(2026, 4, 11)
assert sabado.weekday() == 5
assert is_valley_day(sabado, WEEKEND_DAYS, [])
ok("sábado es valle")

# Domingo = 6, es valle
domingo = date(2026, 4, 12)
assert is_valley_day(domingo, WEEKEND_DAYS, [])
ok("domingo es valle")

# Festivo explícito
festivo = date(2026, 12, 25)
assert is_valley_day(festivo, WEEKEND_DAYS, ["2026-12-25"])
ok("festivo explícito es valle")

# Festivo que cae en martes (no fin de semana)
martes_festivo = date(2026, 12, 8)
assert not is_valley_day(martes_festivo, WEEKEND_DAYS, [])
assert is_valley_day(martes_festivo, WEEKEND_DAYS, ["2026-12-08"])
ok("festivo en martes solo es valle si está en la lista")

# ---------------------------------------------------------------------------
# Tests de decide_charge — mañana laborable
# ---------------------------------------------------------------------------

print("\n=== decide_charge — mañana laborable ===")

# Fijamos "mañana" como lunes (laborable)
MONDAY = date(2026, 4, 13)

with patch("app.decision.date") as mock_date:
    mock_date.today.return_value = MONDAY - timedelta(days=1)
    mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

    # Caso 1: batería llena + día soleado → no cargar
    inp = make_inp(97, forecast_day1=FORECAST_SUNNY)
    r = decide_charge(inp)
    print(f"\n  Batería 97% + día soleado: charge={r.charge_needed}")
    assert not r.charge_needed, f"No debería cargar, got {r.charge_needed}"
    assert not r.valley_day_skip
    ok("batería llena + soleado → no cargar")

    # Caso 2: batería baja + día nublado → cargar
    inp = make_inp(20, forecast_day1=FORECAST_CLOUDY)
    r = decide_charge(inp)
    print(f"  Batería 20% + nublado: charge={r.charge_needed}, target={r.target_soc_pct}%")
    assert r.charge_needed
    assert r.target_soc_pct > 20.0
    assert not r.valley_day_skip
    ok("batería baja + nublado → cargar")

    # Caso 3: clamped al max_soc
    inp = make_inp(5, forecast_day1=FORECAST_ZERO, max_soc_pct=50.0)
    r = decide_charge(inp)
    print(f"  Batería 5% + zero + max=50%: target={r.target_soc_pct}%, clamped={r.clamped}")
    assert r.charge_needed
    assert r.target_soc_pct == 50.0
    assert r.clamped
    ok("clamped al max_soc=50%")

    # Caso 4: batería alta + día muy soleado → no cargar
    inp = make_inp(90, forecast_day1=FORECAST_SUNNY)
    r = decide_charge(inp)
    print(f"  Batería 90% + soleado: charge={r.charge_needed}")
    assert not r.charge_needed
    ok("batería alta + soleado → no cargar")

    # Caso 4b: verificar el umbral — batería 80% + medio SÍ carga (déficit real)
    inp = make_inp(80, forecast_day1=FORECAST_MEDIUM)
    r = decide_charge(inp)
    # Solar ef=7.2, amanecer=14.54, usable=7.78, needed=9.8 → déficit=2.02 → carga
    print(f"  Batería 80% + medio: charge={r.charge_needed} (déficit esperado ~2 kWh)")
    assert r.charge_needed
    ok("batería 80% + medio → carga (déficit real ~2 kWh)")

# ---------------------------------------------------------------------------
# Tests de decide_charge — mañana es día valle
# ---------------------------------------------------------------------------

print("\n=== decide_charge — mañana es día valle ===")

SUNDAY = date(2026, 4, 12)

with patch("app.decision.date") as mock_date:
    mock_date.today.return_value = SUNDAY - timedelta(days=1)
    mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

    # Aunque haya déficit enorme, no debe cargar si mañana es domingo
    inp = make_inp(5, forecast_day1=FORECAST_ZERO)
    r = decide_charge(inp)
    print(f"\n  Batería 5% + nublado + domingo: charge={r.charge_needed}, skip={r.valley_day_skip}")
    assert not r.charge_needed
    assert r.valley_day_skip
    ok("no carga si mañana es domingo aunque haya déficit")

    # Festivo explícito
    inp = make_inp(5, forecast_day1=FORECAST_ZERO, holidays=["2026-04-12"])
    r = decide_charge(inp)
    assert not r.charge_needed
    assert r.valley_day_skip
    ok("no carga si mañana es festivo")

# ---------------------------------------------------------------------------
# Tests de decide_discharge — mañana laborable
# ---------------------------------------------------------------------------

print("\n=== decide_discharge — mañana laborable ===")

with patch("app.decision.date") as mock_date:
    mock_date.today.return_value = MONDAY - timedelta(days=1)
    mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

    inp = make_inp(80)
    d = decide_discharge(inp)
    print(f"\n  Mañana laborable: blocked={d.discharge_blocked}")
    assert not d.discharge_blocked
    ok("mañana laborable → descarga libre siempre")

# ---------------------------------------------------------------------------
# Tests de decide_discharge — mañana es día valle
# ---------------------------------------------------------------------------

print("\n=== decide_discharge — mañana es día valle ===")

with patch("app.decision.date") as mock_date:
    mock_date.today.return_value = SUNDAY - timedelta(days=1)
    mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

    # Batería llena + día 1 y día 2 muy soleados → no bloquear
    inp = make_inp(90, forecast_day1=FORECAST_SUNNY, forecast_day2=FORECAST_SUNNY)
    d = decide_discharge(inp)
    print(f"\n  Batería 90% + soleado soleado: blocked={d.discharge_blocked}")
    assert not d.discharge_blocked
    ok("solar suficiente en 2 días → descarga libre")

    # Batería baja + día 1 soleado pero día 2 muy nublado → bloquear
    inp = make_inp(40, forecast_day1=FORECAST_SUNNY, forecast_day2=FORECAST_CLOUDY)
    d = decide_discharge(inp)
    print(f"  Batería 40% + soleado día1 + nublado día2: blocked={d.discharge_blocked}, deficit={d.deficit_day2_kwh}")
    assert d.discharge_blocked
    assert d.deficit_day2_kwh > 0
    ok("déficit en día 2 → bloquear descarga")

    # Batería alta + ambos nublados → bloquear
    inp = make_inp(70, forecast_day1=FORECAST_CLOUDY, forecast_day2=FORECAST_CLOUDY)
    d = decide_discharge(inp)
    print(f"  Batería 70% + nublado nublado: blocked={d.discharge_blocked}")
    assert d.discharge_blocked
    ok("ambos días nublados + batería media → bloquear")

    # Batería muy alta + día 1 muy soleado → no bloquear aunque día 2 sea nublado
    inp = make_inp(95, forecast_day1=FORECAST_SUNNY, forecast_day2=FORECAST_CLOUDY)
    d = decide_discharge(inp)
    print(f"  Batería 95% + muy soleado día1 + nublado día2: blocked={d.discharge_blocked}, end_day1={d.energy_end_day1_kwh}")
    # Con 95% de batería (21.4 kWh) + solar soleado, debería quedar suficiente para día 2
    ok(f"batería muy alta → {'bloquear' if d.discharge_blocked else 'descarga libre'} (deficit={d.deficit_day2_kwh} kWh)")

# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------

print("\n✓ Todos los tests de decision.py pasaron correctamente\n")
