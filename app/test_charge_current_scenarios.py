"""
test_charge_current_scenarios.py — informe narrado del control de corriente.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_charge_current_scenarios
  o con toda la batería:  make test

Qué es y qué no es
------------------
`test_charge_current.py` comprueba la función a nivel de unidad, caso por caso.
Este fichero cubre la misma función pero con otro propósito: imprimir un cuadro
legible por escenario —qué sabe el controlador y qué decide— para poder juzgar
si el comportamiento es el que se quiere. Los `assert` están para que el cuadro
no mienta.

Reescrito entero en v1.85. Los escenarios anteriores llamaban al controlador
SIN la ventana de excedente, porque la firma cambió en v1.72 (un escalar
`remaining` pasó a ser un `SolarWindow`) y el fichero se quedó atrás: el
parámetro se descartaba en silencio. Resultado: los doce escenarios recorrían la
rama de fallback ("sin forecast") mientras el cuadro anunciaba "solar restante
calibrada". Documentaban un modelo que ya no se ejecutaba.

La diferencia no es cosmética. Mismo escenario, con y sin perfil de excedente:

    sin ventana  → 54 A, "rampa a fin de producción — sin forecast"
    con ventana  → 15 A, "limitada por sol — más A no captan más"

Convenios
---------
· La corriente es del lado BATERÍA (~50 V DC), no de la red AC de 230 V.
· Una franja son 30 min, así que a I amperios caben `I·50·0.5/1000 = I/40` kWh
  en cada una. Con V=50 la aritmética sale redonda: 40 A ↔ 1 kWh por franja.
· La batería carga a `min(I·V·0.5h, excedente_de_la_franja)`: la corriente es un
  TECHO, no un caudal garantizado. Por eso el excedente se simula franja a
  franja en vez de repartir la energía linealmente sobre las horas restantes.
"""
from types import SimpleNamespace as NS

from app.config import ChargeCurrentConfig
from app.main import SolarWindow, _compute_target_charge_current, _VALLE_END_HOUR

CAP = 22.5      # kWh — capacidad de la batería de la instalación
V = 50.0        # V   — tensión de batería (lado DC)
MAX_SOC = 100   # %   — tope de carga

# Valores de producción (los de config.example.yaml), fijados explícitamente en vez
# de heredar los defaults del modelo: el informe pretende ilustrar lo que hace la
# instalación real, pero sin que un cambio de default reescriba doce esperados.
_BASE = {"floor_a": 22, "max_a": 66, "margin": 1.33}
_BALANCE = {**_BASE, "battery_balance": True, "balance_soc_pct": 98.0,
            "balance_soc_pct_2": 99.0, "balance_floor_a": 12}


def _cfg(**overrides):
    return NS(
        charge_current=ChargeCurrentConfig(**{**_BASE, **overrides}),
        installation=NS(battery_capacity_kwh=CAP),
        charging=NS(max_soc_pct=float(MAX_SOC)),
    )


def _state(soc, temp):
    return NS(soc_pct=soc, battery_temp_c=temp, battery_voltage_v=V)


def _sched(charge_needed, target=0):
    return NS(charge_needed=charge_needed, target_soc_pct=target)


def kwh(pct):
    return pct / 100.0 * CAP


def plano(kwh_por_franja, n, end_hour=18.0):
    """Ventana con excedente constante: `n` franjas de 30 min iguales."""
    surplus = [kwh_por_franja] * n
    return SolarWindow(surplus, end_hour, sum(surplus), 0.5, "medido")


def campana(pico_kwh, n, end_hour=18.0):
    """Ventana en forma de campana, que es como se produce de verdad.

    Reparte `n` franjas simétricas alrededor del mediodía solar con el máximo en
    `pico_kwh`. Sirve para los escenarios donde lo que decide no es el total del
    día sino DÓNDE está la energía.
    """
    if n == 1:
        return SolarWindow([pico_kwh], end_hour, pico_kwh, 0.5, "medido")
    mitad = (n - 1) / 2.0
    surplus = [round(pico_kwh * (1 - (abs(i - mitad) / mitad) ** 2), 3)
               for i in range(n)]
    return SolarWindow(surplus, end_hour, sum(surplus), 0.5, "medido")


# ── Cuadro por escenario ───────────────────────────────────────────────────
def narra(sc, cfg, amps, calc, mode):
    soc = sc["soc"]
    w = sc.get("window")
    if sc["modo"] == "VALLE":
        target_pct = (sc["sched"][1] if len(sc["sched"]) > 1 else 0) or MAX_SOC
        horas = _VALLE_END_HOUR - sc["hour"]
        energia = (f"{cfg.charge_current.max_a * V * horas / 1000.0:.2f} kWh"
                   f"  (red, entregable a {cfg.charge_current.max_a} A)")
    else:
        target_pct = MAX_SOC
        horas = sc["solar_end"] - sc["hour"]
        if w is None:
            energia = "sin forecast — reparto lineal sobre las horas restantes"
        else:
            energia = (f"{w.surplus_kwh:.2f} kWh en {len(w.surplus)} franjas"
                       f"  (pico {max(w.surplus):.2f} kWh/franja"
                       f" = {max(w.surplus) / 0.5:.1f} kW)")

    pendiente = max(0.0, kwh(target_pct) - kwh(soc))
    lim = "" if amps == calc else f"   [calculado {calc} A, acotado a [{cfg.charge_current.floor_a},{cfg.charge_current.max_a}]]"
    print(f"\n  ── {sc['titulo']}  [{sc['modo']}]")
    print(f"     Carga ACTUAL ........... {kwh(soc):5.2f} kWh  ({soc:.0f}%)")
    print(f"     Carga OBJETIVO ......... {kwh(target_pct):5.2f} kWh  ({target_pct:.0f}%)")
    print(f"     Energía a cargar ....... {pendiente:5.2f} kWh"
          f"  (·{cfg.charge_current.margin} margen = {pendiente * cfg.charge_current.margin:.2f})")
    print(f"     Tiempo disponible ...... {horas:.2f} h")
    print(f"     Energía captable ....... {energia}")
    print(f"     Temperatura batería .... {sc['temp']:.0f} ºC"
          f"  (puerta {'ON' if cfg.charge_current.temp_gate_enabled else 'OFF'})")
    print(f"     ► Corriente fijada ..... {amps} A  ({amps * V:.0f} W){lim}")
    print(f"       Modo ................. {mode}")


def run():
    passed = failed = 0

    def ejecuta(sc):
        nonlocal passed, failed
        cfg = sc.get("cfg") or _cfg()
        state = _state(sc["soc"], sc["temp"])
        if sc["modo"] == "VALLE":
            sched, hour = _sched(*sc["sched"]), sc["hour"]
            solar_end, window = 0.0, None
        else:
            sched, hour = None, sc["hour"]
            solar_end, window = sc["solar_end"], sc.get("window")
        amps, calc, mode = _compute_target_charge_current(
            cfg, state, hour, sched, sc["current"], solar_end, window)
        narra(sc, cfg, amps, calc, mode)

        errores = []
        if amps != sc["esperado"]:
            errores.append(f"corriente {amps} A (esperada {sc['esperado']})")
        if "modo" in sc.get("espera", {}) and sc["espera"]["modo"] not in mode:
            errores.append(f"modo {mode!r} no contiene {sc['espera']['modo']!r}")
        if "calc" in sc.get("espera", {}) and sc["espera"]["calc"] != calc:
            errores.append(f"calculado {calc} A (esperado {sc['espera']['calc']})")
        if errores:
            failed += 1
            print(f"     ✗ {' · '.join(errores)}")
        else:
            passed += 1
            print(f"     ✓ {sc['esperado']} A")

    # ══════════════════════════════════════════════════════════════════════
    print("=" * 74)
    print(" SOLAR con perfil de excedente — lo que corre en producción (v1.72+)")
    print("=" * 74)
    print("\n La corriente sale de simular franja a franja cuánto cabe en la batería:")
    print(" Σ min(I·V·0.5h, excedente_franja) ≥ energía_pendiente · margen.")

    escenarios_solar = [
        # Excedente de sobra y toda la tarde por delante: nada limita salvo la
        # energía que falta. 11.25 kWh·1.33 = 14.96 captables en 16 franjas →
        # I/40·16 ≥ 14.96 → I ≥ 37.4 → 38 A. Coincide con la fórmula plana
        # 11250/(50·8)·1.33 = 37.4, como debe ser cuando el sol no limita.
        dict(titulo="50→100%, sol de sobra desde las 10:00", modo="SOLAR",
             soc=50, temp=35, hour=10.0, solar_end=18.0,
             window=plano(2.5, 16), current=40, esperado=38,
             espera=dict(modo="rampa al excedente previsto", calc=38)),

        # EL CASO QUE JUSTIFICA v1.72: día encapotado, 4.8 kWh de excedente para
        # 15.75 kWh de hueco. Ni a 66 A se llena (captura tope = 4.8), así que no
        # se devuelve max_a a ciegas: el pico previsto es 0.4 kWh/franja = 0.8 kW
        # = 16 A, y por encima de ahí no se capta ni un vatio más. Acotado al
        # suelo → 22 A. Antes de v1.72 esto era el acantilado a 66 A.
        dict(titulo="30→100%, día encapotado (4.8 kWh de excedente)", modo="SOLAR",
             soc=30, temp=35, hour=11.0, solar_end=18.0,
             window=plano(0.4, 12), current=40, esperado=22,
             espera=dict(modo="limitada por sol", calc=16)),

        # Excedente concentrado en 2 h al mediodía: el total (12 kWh) supera lo
        # que falta (9 kWh), pero está tan comprimido que ni 66 A lo capturan.
        # Pico 3 kWh/franja = 6 kW = 120 A → el límite REAL es la corriente, no el
        # sol, y la etiqueta lo distingue del caso anterior.
        dict(titulo="60→100%, excedente concentrado al mediodía", modo="SOLAR",
             soc=60, temp=35, hour=12.0, solar_end=18.0,
             window=plano(3.0, 4), current=40, esperado=66,
             espera=dict(modo="el excedente supera el tope", calc=120)),

        # EL ESCENARIO QUE MOTIVÓ v1.72. Campana realista (pico 1.6 kWh/franja,
        # 11.64 kWh en 6 h) para 6.75 kWh de hueco: el total sobra de largo, pero
        # las colas no dan I·V. Simulando franja a franja hacen falta 41 A; el
        # reparto lineal pedía 6750/(50·6)·1.33 = 30 A, se quedaba corto y el lazo
        # tenía que corregir al final de la tarde, cuando ya no hay sol que captar.
        #   captured(40) = 8.96 kWh < 6.75·1.33 = 8.98  → insuficiente por poco
        #   captured(41) = 9.11 kWh ≥ 8.98              → mínimo que cumple
        dict(titulo="70→100%, campana de 6 h: las colas no dan I·V", modo="SOLAR",
             soc=70, temp=35, hour=12.0, solar_end=18.0,
             window=campana(1.6, 12), current=40, esperado=41,
             espera=dict(modo="rampa al excedente previsto", calc=41)),

        # Casi llena con sol de sobra: 1.125 kWh·1.33 = 1.50 kWh en 10 franjas →
        # I ≥ 6 A. El suelo de config manda: 22 A, y el cuadro muestra el 6.
        dict(titulo="95→100%, casi llena y con sol de sobra", modo="SOLAR",
             soc=95, temp=35, hour=13.0, solar_end=18.0,
             window=plano(2.0, 10), current=40, esperado=22,
             espera=dict(modo="rampa al excedente previsto", calc=6)),

        # Tarde con prisa: queda 1 h de sol potente (5 kWh/franja) y 9 kWh de
        # hueco. Aquí SÍ hay que ir a tope, y por el motivo correcto: el sol da
        # más de lo que la corriente puede meter (pico = 200 A).
        dict(titulo="60→100% a las 17:00, última hora de sol fuerte", modo="SOLAR",
             soc=60, temp=35, hour=17.0, solar_end=18.0,
             window=plano(5.0, 2), current=30, esperado=66,
             espera=dict(modo="el excedente supera el tope", calc=200)),

        # Batería llena: no se toca la corriente, se deja como esté.
        dict(titulo="100%, batería llena → no tocar", modo="SOLAR",
             soc=100, temp=35, hour=13.0, solar_end=18.0,
             window=plano(2.0, 10), current=40, esperado=40,
             espera=dict(modo="batería llena")),
    ]
    for sc in escenarios_solar:
        ejecuta(sc)

    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 74)
    print(" SOLAR — la puerta de temperatura tiene prioridad sobre la simulación")
    print("=" * 74)

    escenarios_temp = [
        # Caso real del 2026-08-18 08:01: 21.5 kWh de excedente previsto y el día
        # entero por delante, pero la batería a 30.8 ºC (≤ 31) mandó a 66 A. La
        # puerta corta ANTES de simular nada. Se asume a cambio de no perder los
        # picos intermitentes de invierno (decisión del 2026-08-18).
        dict(titulo="30→100% con batería fría (28 ºC) y puerta ON", modo="SOLAR",
             soc=30, temp=28, hour=9.0, solar_end=18.0,
             window=plano(2.5, 18), current=40, esperado=66,
             espera=dict(modo="→ máx")),

        # La misma mañana con la puerta desactivada: se simula el excedente y sale
        # una rampa suave. 15.75·1.33 = 20.95 kWh en 18 franjas → I ≥ 46.6 → 47 A.
        dict(titulo="misma mañana con puerta OFF → rampa suave", modo="SOLAR",
             cfg=_cfg(temp_gate_enabled=False),
             soc=30, temp=28, hour=9.0, solar_end=18.0,
             window=plano(2.5, 18), current=40, esperado=47,
             espera=dict(modo="rampa al excedente previsto", calc=47)),

        # Caliente y con puerta ON: la puerta no dispara, se simula igual.
        dict(titulo="misma mañana pero caliente (35 ºC), puerta ON", modo="SOLAR",
             soc=30, temp=35, hour=9.0, solar_end=18.0,
             window=plano(2.5, 18), current=40, esperado=47,
             espera=dict(modo="rampa al excedente previsto", calc=47)),
    ]
    for sc in escenarios_temp:
        ejecuta(sc)

    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 74)
    print(" SOLAR sin forecast — fallback lineal (Solcast caído o sin datos)")
    print("=" * 74)
    print("\n Sin ventana no hay perfil que simular: se reparte la energía sobre")
    print(" las horas que quedan. Es la rama que los escenarios de v1.71 a v1.84")
    print(" ejercitaban por error creyendo que probaban la de arriba.")

    escenarios_fallback = [
        # 15.75 kWh en 7 h → 15750/(50·7)·1.33 = 59.85 → 60 A. Con la campana
        # equivalente del bloque anterior salía distinto: ese es todo el punto.
        dict(titulo="30→100% a las 11:00, sin forecast", modo="SOLAR",
             soc=30, temp=35, hour=11.0, solar_end=18.0,
             window=None, current=40, esperado=60,
             espera=dict(modo="sin forecast", calc=60)),

        # 5.625 kWh en 5 h → 5625/(50·5)·1.33 = 29.9 → 30 A.
        dict(titulo="75→100% a las 13:00, sin forecast", modo="SOLAR",
             soc=75, temp=35, hour=13.0, solar_end=18.0,
             window=None, current=40, esperado=30,
             espera=dict(modo="sin forecast", calc=30)),

        # Pasado el fin de producción no hay ventana de carga: no se toca nada.
        dict(titulo="20:00, pasado el fin de producción → no tocar", modo="SOLAR",
             soc=80, temp=30, hour=20.0, solar_end=18.0,
             window=None, current=33, esperado=33,
             espera=dict(modo="IDLE")),
    ]
    for sc in escenarios_fallback:
        ejecuta(sc)

    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 74)
    print(" VALLE — carga de red 00:00–08:00 (aquí el reparto lineal SÍ es correcto)")
    print("=" * 74)
    print("\n La red entrega potencia constante: no hay campana que simular.")

    escenarios_valle = [
        # 10.125 kWh en 6 h → 10125/(50·6)·1.33 = 44.9 → 45 A.
        dict(titulo="50→95% a las 02:00, quedan 6 h de valle", modo="VALLE",
             soc=50, temp=24, hour=2.0, sched=(True, 95), current=55, esperado=45,
             espera=dict(modo="VALLE", calc=45)),

        # 20.25 kWh en 2 h pide 269 A: se acota al tope del inversor.
        dict(titulo="10→100% a las 06:00, quedan 2 h → tope", modo="VALLE",
             soc=10, temp=24, hour=6.0, sched=(True, 100), current=55, esperado=66,
             espera=dict(modo="VALLE", calc=269)),

        # 1.125 kWh en 7 h pide 4 A: se acota al suelo de config.
        dict(titulo="90→95% a las 01:00, sobra tiempo → suelo", modo="VALLE",
             soc=90, temp=24, hour=1.0, sched=(True, 95), current=55, esperado=22,
             espera=dict(modo="VALLE", calc=4)),

        dict(titulo="ya en el objetivo → no tocar", modo="VALLE",
             soc=95, temp=24, hour=2.0, sched=(True, 95), current=55, esperado=55,
             espera=dict(modo="objetivo alcanzado")),

        dict(titulo="valle sin carga programada → no tocar", modo="VALLE",
             soc=50, temp=24, hour=2.0, sched=(False,), current=55, esperado=55,
             espera=dict(modo="IDLE")),
    ]
    for sc in escenarios_valle:
        ejecuta(sc)

    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 74)
    print(" BALANCE — último tramo, carga lo más suave que dé tiempo")
    print("=" * 74)
    print("\n Activo en producción (battery_balance: true) y sin cobertura hasta")
    print(" v1.85. Se evalúa ANTES que VALLE/SOLAR e ignora la temperatura.")

    bal = _cfg(**{k: v for k, v in _BALANCE.items() if k not in _BASE})
    escenarios_balance = [
        # 0.45 kWh en 4 h pide 3 A → suelo de balanceo (12 A). Ignora que la
        # batería esté fría: cerca del tope prima la carga suave.
        dict(titulo="98% a las 14:00 con sol → BALANCE", modo="SOLAR", cfg=bal,
             soc=98, temp=28, hour=14.0, solar_end=18.0,
             window=plano(2.0, 8), current=40, esperado=12,
             espera=dict(modo="BALANCE", calc=3)),

        # 2ª etapa al 99%: el suelo baja a la mitad (12//2 = 6 A).
        dict(titulo="99% → 2ª etapa, suelo a la mitad", modo="SOLAR", cfg=bal,
             soc=99, temp=28, hour=14.0, solar_end=18.0,
             window=plano(2.0, 8), current=40, esperado=6,
             espera=dict(modo="BALANCE (fino)", calc=1)),

        # También balancea de noche, pero solo si el valle está cargando: el tope
        # real de carga lo marca 6.3.1, no la corriente.
        dict(titulo="98% en valle con carga programada → BALANCE", modo="VALLE",
             cfg=bal, soc=98, temp=24, hour=5.0, sched=(True, 100),
             current=40, esperado=12, espera=dict(modo="BALANCE", calc=4)),

        # Sin ventana de carga (valle sin carga programada) el balanceo no entra:
        # forzar corriente cuando no se está cargando no balancearía nada.
        dict(titulo="98% en valle SIN carga → no entra BALANCE", modo="VALLE",
             cfg=bal, soc=98, temp=24, hour=5.0, sched=(False,),
             current=40, esperado=40, espera=dict(modo="IDLE")),
    ]
    for sc in escenarios_balance:
        ejecuta(sc)

    print("\n" + "-" * 74)
    if failed:
        print(f"✗ {failed} escenario(s) fallaron, {passed} OK")
        return 1
    print(f"✓ Todos los escenarios pasaron ({passed})")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run())
