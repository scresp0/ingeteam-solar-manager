"""
Test de decision.py — ejecutar con:
  docker compose run --rm solar-manager python -m app.test_decision
"""
from app.decision import (
    SolarForecast, DecisionInput, calculate_charge_target, decision_summary
)

BASE = dict(
    battery_capacity_kwh=10.0,
    daily_consumption_kwh=15.0,
    risk_factor=0.7,
    min_soc_pct=15.0,
    max_soc_pct=95.0,
    safety_margin_kwh=1.0,
)

def case(name, forecast, soc_actual_pct, expected_action):
    inp = DecisionInput(
        forecast=forecast,
        soc_actual_pct=soc_actual_pct,
        **BASE,
    )
    result = calculate_charge_target(inp)
    print(f"\n--- {name} ---")
    print(decision_summary(inp, result))
    print(f"  => Acción esperada: {expected_action}")
    return result


# Caso 1: día muy soleado, batería a medio
r = case(
    "Día muy soleado, batería al 40%",
    SolarForecast(p10=12.0, p50=16.0, p90=18.0),
    soc_actual_pct=40.0,
    expected_action="Sin carga de red (solar cubre todo)"
)
assert r.to_charge_kwh == 0.0, f"Esperaba 0 kWh a cargar, got {r.to_charge_kwh}"

# Caso 2: día muy nublado, batería baja
r = case(
    "Día muy nublado, batería al 20%",
    SolarForecast(p10=1.0, p50=3.0, p90=5.0),
    soc_actual_pct=20.0,
    expected_action="Carga máxima (llegar al 95%)"
)
assert r.target_soc_pct == 95.0, f"Esperaba SOC 95%, got {r.target_soc_pct}"
assert r.clamped, "Esperaba clamped=True"

# Caso 3: día parcialmente nublado, carga parcial
# Solar efectiva = 8*0.7 + 11*0.3 = 5.6 + 3.3 = 8.9 kWh
# Energía almacenada = 60% * 10 = 6.0 kWh
# Déficit = max(0, 15 + 1 - 8.9 - 6.0) = max(0, 1.1) = 1.1 kWh
# SOC objetivo = (6.0 + 1.1) / 10 * 100 = 71% → no clamped
r = case(
    "Día parcial, batería al 60%",
    SolarForecast(p10=8.0, p50=11.0, p90=13.0),
    soc_actual_pct=60.0,
    expected_action="Carga parcial (~71%)"
)
assert 15.0 < r.target_soc_pct < 95.0, f"Esperaba carga parcial, got {r.target_soc_pct}%"
assert not r.clamped, "No debería estar clamped"

# Caso 4: batería ya llena, día soleado
r = case(
    "Batería casi llena (90%), día soleado",
    SolarForecast(p10=10.0, p50=14.0, p90=16.0),
    soc_actual_pct=90.0,
    expected_action="Sin carga de red"
)
assert r.to_charge_kwh == 0.0, f"Esperaba 0 kWh a cargar, got {r.to_charge_kwh}"

print("\n\nTodos los tests pasaron OK")
