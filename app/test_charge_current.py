"""
test_charge_current.py — pruebas de la lógica del controlador de corriente de carga.

Prueba la función pura `main._compute_target_charge_current` (sin MODBUS, sin
forecast, sin Playwright): modos VALLE/SOLAR/IDLE, umbral de temperatura, rampa de
corriente hasta el fin de producción, mínimos/máximos y casos en los que NO se toca
la corriente.

Ejecutar:
  PYTHONPATH=. python app/test_charge_current.py
  o en el contenedor:  docker exec -i solar-manager python -m app.test_charge_current
"""
import sys
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import app.main as m
from app.config import ChargeCurrentConfig
from app.main import (
    _compute_target_charge_current, _min_current_for_surplus, SolarWindow,
    _house_consumption_for_slot,
)


# Los casos de este fichero están escritos sobre estos valores. Fijarlos aquí
# (en vez de heredar los defaults del modelo) aísla los tests de la calibración
# de producción: en v1.83 los defaults pasaron a floor_a 22 y margin 1.33 para
# alinearse con config.example.yaml, y eso no debe reescribir las expectativas
# de la lógica que se está probando.
_TEST_BASE = {"floor_a": 15, "margin": 1.2}


def _cfg(**overrides):
    return NS(
        charge_current=ChargeCurrentConfig(**{**_TEST_BASE, **overrides}),
        installation=NS(battery_capacity_kwh=22.5),
        charging=NS(max_soc_pct=95.0),
    )


def _state(soc, temp, v=50.0):
    return NS(soc_pct=soc, battery_temp_c=temp, battery_voltage_v=v)


def _sched(charge_needed, target=0):
    return NS(charge_needed=charge_needed, target_soc_pct=target)


# firma: _compute_target_charge_current(cfg, state, hour, sched, current,
#                                        solar_end, window=None)
def comp(cfg, state, hour, sched, current, solar_end):
    """Llama al controlador SIN ventana de excedente (window=None).

    Es la rama de fallback: reparto lineal hasta el fin de producción. La que
    corre en producción desde v1.72 (simulación franja a franja) se ejerce más
    abajo con `comp_win`. Este helper arrastraba un parámetro `remaining` que la
    firma ya no acepta desde v1.72 y que se descartaba en silencio: las llamadas
    le pasaban valores distintos como si cambiaran el resultado.
    """
    return _compute_target_charge_current(cfg, state, hour, sched, current, solar_end)


def main():
    cfg = _cfg()   # hot 30, floor 15, max 66, margin 1.2 (ver _TEST_BASE)
    passed = failed = 0

    def check(desc, got, exp_amps=None, mode_has=None):
        nonlocal passed, failed
        amps, _calc, mode = got
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
    check("SOC50→95, quedan 6h", comp(cfg, _state(50, 24), 2.0, _sched(True, 95), 55, 0), 40, "VALLE")
    check("déficit grande → 66", comp(cfg, _state(10, 24), 6.0, _sched(True, 100), 55, 0), 66, "VALLE")
    check("déficit pequeño → suelo 15", comp(cfg, _state(90, 24), 1.0, _sched(True, 95), 55, 0), 15, "VALLE")
    check("objetivo alcanzado → no tocar (deja 55)", comp(cfg, _state(95, 24), 2.0, _sched(True, 95), 55, 0), 55, "sin cambios")
    check("valle SIN carga → no tocar (deja 55)", comp(cfg, _state(50, 24), 2.0, _sched(False), 55, 0), 55, "sin cambios")

    # Todo este bloque va sin ventana de excedente, o sea por la rama de fallback
    # "sin forecast": reparto lineal hasta solar_end. Antes había un caso suelto
    # llamado "sin forecast" que, tras quitar el parámetro muerto, resultó ser una
    # copia exacta de "calor + mucho tiempo" — ya no aportaba nada.
    print("=== SOLAR sin forecast (08:00 – fin de producción, reparto lineal) ===")
    # temp ≤ 30 con gate ON → máx
    check("templada (≤30) → 66", comp(cfg, _state(70, 28), 12.0, None, 40, 17.0), 66, "máx")
    # temp > 30: SOC70→95 E=5.625, 5h → 27A (rampa)
    check("calor + mucho tiempo → mín 27", comp(cfg, _state(70, 32), 12.0, None, 40, 17.0), 27, "rampa")
    # SOC30→95 en 5h = 14.625 kWh → amps_for pide 70A, acotado a 66 (falta tiempo,
    # ya no depende de si la solar "cubre"): el remaining pequeño es irrelevante.
    check("poco tiempo, mucha energía → 66", comp(cfg, _state(30, 32), 12.0, None, 40, 17.0), 66, "rampa")
    # batería llena → no tocar
    check("batería llena → no tocar (deja 40)", comp(cfg, _state(96, 32), 12.0, None, 40, 17.0), 40, "sin cambios")
    # cerca del fin (poco tiempo) → sube a 66
    check("cerca del fin → 66", comp(cfg, _state(80, 32), 16.8, None, 40, 17.0), 66, "rampa")

    print("=== IDLE / fronteras ===")
    check("tarde tras fin solar → no tocar", comp(cfg, _state(80, 32), 20.0, None, 33, 17.0), 33, "sin cambios")
    check("hora == fin solar → no tocar", comp(cfg, _state(70, 32), 17.0, None, 33, 17.0), 33, "sin cambios")
    check("tensión 0 → fallback 50V (=caso VALLE base)", comp(cfg, _state(50, 24, 0.0), 2.0, _sched(True, 95), 55, 0), 40, "VALLE")

    print("=== parámetros no-default (hot 35, floor 10, max 50) ===")
    cfg2 = _cfg(hot_threshold_c=35.0, floor_a=10, max_a=50)
    check("temp 32 ≤ 35 → máx 50", comp(cfg2, _state(70, 32), 12.0, None, 40, 17.0), 50, "máx")
    check("temp 40 > 35 → rampa 27", comp(cfg2, _state(70, 40), 12.0, None, 40, 17.0), 27, "rampa")
    check("rampa por encima de max_a=50 → 50", comp(cfg2, _state(10, 40), 12.0, None, 40, 17.0), 50, "rampa")

    print("=== SOLAR sin forecast, puerta de temperatura DESACTIVADA ===")
    cfg3 = _cfg(temp_gate_enabled=False)
    # batería fría (28 ≤ 30) → carga suave igualmente (la temperatura no influye)
    check("fría → rampa 27", comp(cfg3, _state(70, 28), 12.0, None, 40, 17.0), 27, "rampa")
    # misma decisión esté fría o caliente
    check("caliente → rampa 27", comp(cfg3, _state(70, 40), 12.0, None, 40, 17.0), 27, "rampa")
    # poco tiempo + mucha energía → 66 aunque esté fría (falta tiempo, no temperatura)
    check("fría + poco tiempo → 66", comp(cfg3, _state(30, 28), 12.0, None, 40, 17.0), 66, "rampa")

    # ------------------------------------------------------------------
    # Simulación franja a franja del excedente solar (v1.72)
    # ------------------------------------------------------------------
    print("=== _min_current_for_surplus (corriente mínima sobre el perfil de excedente) ===")
    cc = ChargeCurrentConfig(**_TEST_BASE)   # floor 15, max 66, margin 1.2

    def check_raw(desc, got, exp_amps, exp_reached):
        nonlocal passed, failed
        amps, reached = got
        if amps == exp_amps and reached == exp_reached:
            passed += 1
            print(f"  ✓  {desc} → {amps}A (objetivo_alcanzable={reached})")
        else:
            failed += 1
            print(f"  ✗  {desc} → {amps}A (alcanzable={reached})  "
                  f"(esperado {exp_amps}A / alcanzable={exp_reached})")

    # Excedente muy holgado (4 kWh por franja): nada limita salvo la energía pedida.
    # Coincide con la fórmula plana E*1000/(V*H)*margin salvo redondeo hacia arriba:
    # 6 kWh en 10 franjas (5 h) → 6000/(50*5)*1.2 = 28.8 → 29A.
    holgado = [4.0] * 10
    check_raw("excedente holgado, 6 kWh en 5h", _min_current_for_surplus(holgado, 6.0, 50.0, cc), 29, True)

    # Mismo excedente TOTAL pero concentrado en 2 franjas: es justo lo que el reparto
    # lineal no veía. Capturar 7.2 kWh (6·margin) en 1 h exige 7.2 kW = 144A, muy por
    # encima del tope → devuelve el pico (4 kWh/0.5h = 8 kW = 160A) y no alcanzable.
    # Acotado da 66A: aquí el límite REAL es la corriente, no el sol.
    concentrado = [0.0] * 4 + [4.0, 4.0] + [0.0] * 4
    check_raw("mismo excedente concentrado en 1h → tope", _min_current_for_surplus(concentrado, 6.0, 50.0, cc), 160, False)

    # Excedente total insuficiente: ni con 66A se llena. NO debe devolver max_a a
    # ciegas, sino el pico de excedente (0.9 kWh/franja = 1.8 kW → 36A): por encima
    # de eso no se captura ni un vatio más. Es el acantilado que se evita.
    escaso = [0.9] * 6
    check_raw("excedente insuficiente → techo = pico, no 66", _min_current_for_surplus(escaso, 15.0, 50.0, cc), 36, False)

    # Sin excedente en absoluto (la casa se lo come todo) → 1A crudo, no alcanzable.
    check_raw("excedente nulo", _min_current_for_surplus([0.0] * 6, 5.0, 50.0, cc), 1, False)

    print("=== SOLAR con perfil de excedente (window) ===")

    def comp_win(cfg, state, hour, current, window):
        return _compute_target_charge_current(cfg, state, hour, None, current, window.end_hour, window)

    def win(surplus, end_hour=17.0):
        return SolarWindow(surplus, end_hour, sum(surplus), 0.5, "medido")

    # SOC70→95 = 5.625 kWh con excedente holgado → 5625/(50*5)*1.2 = 27 → 27A,
    # el mismo resultado que la rampa plana cuando el sol no limita.
    check("holgado, mismo que rampa plana", comp_win(cfg, _state(70, 32), 12.0, 40, win([4.0] * 10)), 27, "excedente previsto")
    # Excedente escaso: se queda en el pico útil (36A) en vez de saltar a 66A.
    check("escaso → limitada por sol, no 66", comp_win(cfg, _state(30, 32), 12.0, 40, win([0.9] * 6)), 36, "limitada por sol")
    # Excedente concentrado al mediodía: aquí sí manda el tope de corriente.
    check("concentrado → máx por tope de corriente",
          comp_win(cfg, _state(70, 32), 12.0, 40, win([0.0] * 4 + [4.0, 4.0] + [0.0] * 4)), 66, "supera el tope")
    # Excedente nulo → cae al suelo configurado (15A), no al máximo.
    check("nulo → suelo, no máximo", comp_win(cfg, _state(30, 32), 12.0, 40, win([0.0] * 6)), 15, "limitada por sol")
    # Batería llena manda sobre la simulación.
    check("llena con window → no tocar", comp_win(cfg, _state(96, 32), 12.0, 40, win([4.0] * 10)), 40, "sin cambios")
    # La puerta de temperatura sigue teniendo prioridad sobre la simulación:
    # el forecast p50 no ve los claros intermitentes que justifican captar a tope.
    check("fría con window → máx (gate manda)", comp_win(cfg, _state(70, 28), 12.0, 40, win([4.0] * 10)), 66, "máx")
    # Window vacía (forecast dice que ya no queda sol) → end_hour 0 → IDLE.
    check("sin sol restante → IDLE", comp_win(cfg, _state(70, 32), 12.0, 33, win([], 0.0)), 33, "sin cambios")

    print("=== Perfil histórico de consumo (_house_consumption_for_slot) ===")

    def check_val(desc, got, exp, tol=0.001):
        nonlocal passed, failed
        ok = abs(got - exp) < tol
        if ok:
            passed += 1
            print(f"  ✓  {desc} → {got:.3f} kWh")
        else:
            failed += 1
            print(f"  ✗  {desc} → {got:.3f} kWh (esperado {exp:.3f})")

    now = datetime(2026, 8, 18, 16, 0)
    profile = {"16:00": 1.6, "18:00": 0.3}   # kWh/franja: tarde alta, noche baja

    # Sin perfil (pocos días en InfluxDB) → pura persistencia, aunque la franja
    # coincida con una hora "conocida".
    check_val("sin perfil → persistencia", _house_consumption_for_slot(now, now, 0.3, None), 0.3)

    # Franja ACTUAL (peso persistencia = 1) → ignora el histórico aunque exista.
    check_val("franja actual → pura persistencia", _house_consumption_for_slot(now, now, 0.3, profile), 0.3)

    # Franja a 2h o más (fuera del plazo de mezcla) → puro histórico.
    check_val("franja a 2h → puro histórico",
              _house_consumption_for_slot(datetime(2026, 8, 18, 18, 0), now, 0.9, profile), 0.3)

    # Franja a 1h (mitad del plazo de 2h) → mezcla 50/50 entre persistencia (0.3)
    # e histórico de esa franja (1.6, la de las 16:00 reutilizada para el ejemplo).
    media = _house_consumption_for_slot(datetime(2026, 8, 18, 17, 0), now, 0.3, {"17:00": 1.6})
    check_val("franja a 1h → mezcla 50/50", media, (0.3 + 1.6) / 2)

    # Franja sin entrada en el perfil (hueco de datos en esa franja concreta) →
    # cae a persistencia, no a 0.
    check_val("franja sin dato en el perfil → persistencia",
              _house_consumption_for_slot(datetime(2026, 8, 18, 20, 0), now, 0.3, profile), 0.3)

    print("=== _solar_surplus_window: pre-check barato (v1.79) ===")

    def check_eq(desc, got, exp):
        nonlocal passed, failed
        if got == exp:
            passed += 1
            print(f"  ✓  {desc} → {got}")
        else:
            failed += 1
            print(f"  ✗  {desc} → {got}  (esperado {exp})")

    def _patch(intervals_fn):
        calls = {"bias": 0, "house": 0, "profile": 0}
        real = (m.get_today_intervals, m.get_dynamic_solar_bias,
                m._house_power_estimate, m.get_house_power_profile)

        def fake_bias(*a, **k):
            calls["bias"] += 1
            return 0.8

        def fake_house(*a, **k):
            calls["house"] += 1
            return 0.5, "medido"

        def fake_profile(*a, **k):
            calls["profile"] += 1
            return None

        m.get_today_intervals = intervals_fn
        m.get_dynamic_solar_bias = fake_bias
        m._house_power_estimate = fake_house
        m.get_house_power_profile = fake_profile
        return calls, real

    def _unpatch(real):
        (m.get_today_intervals, m.get_dynamic_solar_bias,
         m._house_power_estimate, m.get_house_power_profile) = real

    solcast_cfg = NS(
        solcast=NS(), system=NS(timezone="Europe/Madrid"), influxdb=NS(),
        charging=NS(solar_bias_window_days=30, solar_bias_min_days_in_window=14),
        charge_current=NS(house_profile_window_days=30, house_profile_min_days_in_window=14),
    )
    now = datetime(2026, 8, 19, 20, 0, tzinfo=ZoneInfo("Europe/Madrid"))

    # Todas las franjas restantes con pv_estimate=0.0 (como de noche en Solcast) →
    # debe cortar ANTES de pedir bias/consumo/perfil: 0 llamadas a cada uno.
    def intervals_sin_sol(*a, **k):
        return [
            {"period_end": "2026-08-19T20:00:00.0000000Z", "pv_estimate": 0.0},
            {"period_end": "2026-08-19T20:30:00.0000000Z", "pv_estimate": 0.0},
        ]

    calls, real = _patch(intervals_sin_sol)
    try:
        window = m._solar_surplus_window(solcast_cfg, now)
    finally:
        _unpatch(real)
    check_eq("sin sol restante → surplus vacío", len(window.surplus), 0)
    check_eq("sin sol restante → end_hour 0", window.end_hour, 0.0)
    check_eq("sin sol restante → NO llama a bias", calls["bias"], 0)
    check_eq("sin sol restante → NO llama a consumo medido", calls["house"], 0)
    check_eq("sin sol restante → NO llama a perfil histórico", calls["profile"], 0)

    # Con al menos una franja con pv_estimate > 0 → SÍ debe seguir el camino normal
    # (bias/consumo/perfil se llaman una vez cada uno) — confirma que el pre-check
    # no rompe el caso real.
    def intervals_con_sol(*a, **k):
        return [
            {"period_end": "2026-08-19T20:00:00.0000000Z", "pv_estimate": 0.0},
            {"period_end": "2026-08-19T20:30:00.0000000Z", "pv_estimate": 1.5},
        ]

    calls, real = _patch(intervals_con_sol)
    try:
        window = m._solar_surplus_window(solcast_cfg, now)
    finally:
        _unpatch(real)
    check_eq("con sol restante → SÍ llama a bias", calls["bias"], 1)
    check_eq("con sol restante → SÍ llama a consumo medido", calls["house"], 1)
    check_eq("con sol restante → SÍ llama a perfil histórico", calls["profile"], 1)
    check_eq("con sol restante → una franja de excedente", len(window.surplus), 1)

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        sys.exit(1)
    print(f"✓ Todos los tests de control de corriente pasaron ({passed})")


if __name__ == "__main__":
    main()
