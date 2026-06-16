"""
test_charge_current_scenarios.py — narración de escenarios del controlador de corriente.

A diferencia de `test_charge_current.py` (asserts escuetos), este test *cuenta* qué
haría el algoritmo en cada situación. Para cada escenario imprime:

  - carga de la batería ACTUAL   (kWh y %)
  - carga de la batería OBJETIVO (kWh y %)
  - tiempo disponible para la carga (horas)
  - energía disponible para la carga (kWh)
      · SOLAR: producción solar restante calibrada del forecast
      · VALLE: energía que la red puede entregar a la corriente máxima en la ventana
  - potencia de carga configurada (amperios DC y vatios sobre la tensión de batería)

Escenario protagonista: la batería necesita ir del 30% al 100% pero el forecast solo
da para llegar al 80% → en modo SOLAR es "todo o nada": si la solar prevista no cubre
el objetivo COMPLETO, el algoritmo carga a máximo (66A) para no quedarse corto.

Ejecutar:
  PYTHONPATH=. python app/test_charge_current_scenarios.py
  o en el contenedor:  docker exec -i solar-manager python -m app.test_charge_current_scenarios
"""
import sys
from types import SimpleNamespace as NS

from app.config import ChargeCurrentConfig
from app.main import _compute_target_charge_current, _VALLE_END_HOUR

CAP = 22.5      # kWh — capacidad de la batería de la instalación
V = 50.0        # V   — tensión de batería (lado DC; NO la red AC de 230V)
MAX_SOC = 100   # %   — tope de carga usado en estos escenarios


def _cfg(**overrides):
    return NS(
        charge_current=ChargeCurrentConfig(**overrides),
        installation=NS(battery_capacity_kwh=CAP),
        charging=NS(max_soc_pct=float(MAX_SOC)),
    )


def _state(soc, temp):
    return NS(soc_pct=soc, battery_temp_c=temp, battery_voltage_v=V)


def _sched(charge_needed, target=0):
    return NS(charge_needed=charge_needed, target_soc_pct=target)


def kwh(pct):
    return pct / 100.0 * CAP


def narra(sc, cfg, amps, mode):
    """Imprime el cuadro de un escenario con todos los campos pedidos."""
    soc = sc["soc"]
    if sc["modo"] == "VALLE":
        target_pct = sc["sched"][1]
        horas = _VALLE_END_HOUR - sc["hour"]
        e_disp = f"{cfg.charge_current.max_a * V * horas / 1000.0:.2f} kWh  (red, entregable a {cfg.charge_current.max_a}A máx)"
    else:  # SOLAR
        target_pct = MAX_SOC
        horas = sc["solar_end"] - sc["hour"]
        rem = sc["remaining"]
        e_disp = ("sin forecast (no disponible)" if rem is None
                  else f"{rem:.2f} kWh  (solar restante calibrada)")

    pendiente = max(0.0, kwh(target_pct) - kwh(soc))
    print(f"\n  ── {sc['titulo']}  [{sc['modo']}]")
    print(f"     Carga ACTUAL ........... {kwh(soc):5.2f} kWh  ({soc:.0f}%)")
    print(f"     Carga OBJETIVO ......... {kwh(target_pct):5.2f} kWh  ({target_pct:.0f}%)")
    print(f"     Energía a cargar ....... {pendiente:5.2f} kWh")
    print(f"     Tiempo disponible ...... {horas:.2f} h")
    print(f"     Energía disponible ..... {e_disp}")
    print(f"     ► Potencia configurada . {amps} A  ({amps * V:.0f} W)   modo: {mode}")


def run():
    passed = failed = 0

    def ejecuta(sc):
        nonlocal passed, failed
        cfg = sc.get("cfg") or _cfg()
        state = _state(sc["soc"], sc["temp"])
        if sc["modo"] == "VALLE":
            sched, hour = _sched(*sc["sched"]), sc["hour"]
            remaining, solar_end = 0.0, 0.0
        else:
            sched, hour = None, sc["hour"]
            remaining, solar_end = sc["remaining"], sc["solar_end"]
        amps, mode = _compute_target_charge_current(
            cfg, state, hour, sched, sc["current"], remaining, solar_end)
        narra(sc, cfg, amps, mode)
        exp = sc["esperado"]
        if amps == exp:
            passed += 1
            print(f"     ✓ esperado {exp}A")
        else:
            failed += 1
            print(f"     ✗ esperado {exp}A  pero dio {amps}A")

    print("=" * 72)
    print(" MODO SOLAR — la solar prevista DEBE cubrir el objetivo completo, si no → máx")
    print("=" * 72)

    escenarios_solar = [
        # PROTAGONISTA: 30→100% pero el forecast solo da para el 80% (11.25 kWh).
        # 15.75 kWh pendientes > 11.25 kWh de sol → insuficiente → MÁX 66A.
        dict(titulo="30→100%, forecast solo cubre hasta 80%", modo="SOLAR",
             soc=30, temp=32, hour=11.0, solar_end=18.0,
             remaining=kwh(80) - kwh(30), current=40, esperado=66),

        # Forecast cubre JUSTO hasta el 100% (15.75 kWh), 8h por delante → carga suave.
        # amps = 24·15.75/8 = 47A.
        dict(titulo="30→100%, forecast cubre justo el 100%", modo="SOLAR",
             soc=30, temp=32, hour=10.0, solar_end=18.0,
             remaining=kwh(100) - kwh(30), current=40, esperado=47),

        # Forecast con holgura (20 kWh) y mucho tiempo (10h) → carga muy suave.
        # amps = 24·15.75/10 = 38A.
        dict(titulo="30→100%, forecast de sobra (20 kWh), 10h", modo="SOLAR",
             soc=30, temp=32, hour=8.0, solar_end=18.0,
             remaining=20.0, current=40, esperado=38),

        # INTERMEDIO: 60→100% (9 kWh), forecast 15 kWh ≥ 9 → suficiente, 7h.
        # amps = 24·9/7 = 31A.
        dict(titulo="60→100%, forecast 15 kWh suficiente, 7h", modo="SOLAR",
             soc=60, temp=32, hour=11.0, solar_end=18.0,
             remaining=15.0, current=40, esperado=31),

        # EXTREMO casi-llena: 95→100% (1.125 kWh), solar de sobra → mínimo (suelo 15A).
        dict(titulo="95→100%, casi llena, solar de sobra", modo="SOLAR",
             soc=95, temp=32, hour=11.0, solar_end=18.0,
             remaining=10.0, current=40, esperado=15),

        # EXTREMO llena: 100% → no toca la corriente (deja el valor actual, 40A).
        dict(titulo="100%, batería llena → no tocar", modo="SOLAR",
             soc=100, temp=32, hour=11.0, solar_end=18.0,
             remaining=10.0, current=40, esperado=40),

        # Sin forecast disponible → no se puede asegurar la solar → MÁX 66A.
        dict(titulo="30→100%, sin forecast → máx", modo="SOLAR",
             soc=30, temp=32, hour=11.0, solar_end=18.0,
             remaining=None, current=40, esperado=66),

        # Batería FRÍA (28≤30ºC) con puerta de temperatura activa → MÁX 66A
        # aunque la solar bastaría (capta picos intermitentes en invierno).
        dict(titulo="30→100%, fría 28ºC + gate temp ON → máx", modo="SOLAR",
             soc=30, temp=28, hour=11.0, solar_end=18.0,
             remaining=kwh(100) - kwh(30), current=40, esperado=66),
    ]
    for sc in escenarios_solar:
        ejecuta(sc)

    print("\n" + "=" * 72)
    print(" MODO VALLE — carga de red 00:00–08:00; mín. corriente para llegar a tiempo")
    print("=" * 72)

    escenarios_valle = [
        # PROTAGONISTA en valle: 30→100% (15.75 kWh) con 6h de valle (02:00).
        # amps = 24·15.75/6 = 63A.
        dict(titulo="30→100%, quedan 6h de valle (02:00)", modo="VALLE",
             soc=30, temp=24, hour=2.0, sched=(True, 100),
             current=55, esperado=63),

        # Poco tiempo: 30→100% pero solo 2h de valle (06:00) → MÁX 66A (no llega justo).
        dict(titulo="30→100%, solo 2h de valle (06:00)", modo="VALLE",
             soc=30, temp=24, hour=6.0, sched=(True, 100),
             current=55, esperado=66),

        # Objetivo casi alcanzado: 90→100% (2.25 kWh), 6h → mínimo (suelo 15A).
        dict(titulo="90→100%, poco que cargar, 6h", modo="VALLE",
             soc=90, temp=24, hour=2.0, sched=(True, 100),
             current=55, esperado=15),

        # Valle SIN carga programada → no toca la corriente (deja 55A).
        dict(titulo="valle sin carga programada → no tocar", modo="VALLE",
             soc=50, temp=24, hour=2.0, sched=(False, 0),
             current=55, esperado=55),
    ]
    for sc in escenarios_valle:
        ejecuta(sc)

    print("\n" + "-" * 72)
    if failed:
        print(f"✗ {failed} escenario(s) fallaron, {passed} OK")
        sys.exit(1)
    print(f"✓ Todos los escenarios pasaron ({passed})")


if __name__ == "__main__":
    run()
