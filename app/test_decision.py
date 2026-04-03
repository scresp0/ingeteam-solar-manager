"""
Test de decision.py — ejecutar con:
  docker compose run --rm solar-manager python -m app.test_decision
"""
from app.decision import SolarForecast, DecisionInput, calculate_charge_target, decision_summary

BASE = dict(
    battery_capacity_kwh=22.0,
    daily_consumption_kwh=16.0,
    night_consumption_kwh=3.5,
    risk_factor=0.7,
    min_soc_pct=35.0,
    max_soc_pct=100.0,
    safety_margin_kwh=1.0,
)

def case(name, forecast, soc_actual_pct, expected_charge):
    inp = DecisionInput(forecast=forecast, soc_actual_pct=soc_actual_pct, **BASE)
    result = calculate_charge_target(inp)
    print(f"\n--- {name} ---")
    print(decision_summary(inp, result))
    print(f"  => charge_needed esperado: {expected_charge}, obtenido: {result.charge_needed}")
    return result


# Caso 1: batería llena, día muy soleado → no cargar
r = case(
    "Batería al 97%, día muy soleado",
    SolarForecast(p10=12.0, p50=16.0, p90=18.0),
    soc_actual_pct=97.0,
    expected_charge=False,
)
assert not r.charge_needed, f"No debería cargar, got charge_needed={r.charge_needed}"

# Caso 2: batería baja, día muy nublado → cargar (carga parcial, no necesariamente al máximo)
r = case(
    "Batería al 20%, día muy nublado",
    SolarForecast(p10=1.0, p50=2.0, p90=3.0),
    soc_actual_pct=20.0,
    expected_charge=True,
)
assert r.charge_needed, "Debería cargar"
assert r.target_soc_pct > 20.0, f"SOC objetivo debe ser mayor que el actual, got {r.target_soc_pct}%"

# Caso 2b: batería muy baja + sin sol + consumo alto → carga máxima (clamped)
r = case(
    "Batería al 5%, sin sol, consumo alto",
    SolarForecast(p10=0.0, p50=0.0, p90=0.0),
    soc_actual_pct=5.0,
    expected_charge=True,
)
assert r.charge_needed, "Debería cargar"
assert r.target_soc_pct == 100.0, f"Esperaba 100% (clamped), got {r.target_soc_pct}%"
assert r.clamped, "Debería estar clamped al máximo"

# Caso 3: batería media, día parcial → carga parcial
r = case(
    "Batería al 60%, día parcial",
    SolarForecast(p10=6.0, p50=9.0, p90=12.0),
    soc_actual_pct=60.0,
    expected_charge=True,
)
assert r.charge_needed, "Debería cargar"
assert 35.0 < r.target_soc_pct < 100.0, f"Esperaba carga parcial, got {r.target_soc_pct}%"

# Caso 4: batería alta, día soleado → no cargar
r = case(
    "Batería al 80%, día soleado",
    SolarForecast(p10=10.0, p50=14.0, p90=16.0),
    soc_actual_pct=80.0,
    expected_charge=False,
)
assert not r.charge_needed, f"No debería cargar, got {r.charge_needed}"

print("\n\nTodos los tests pasaron OK")
