"""
test_storage.py — tests de storage.py sin InfluxDB.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_storage
  o con toda la batería:  make test

Por qué existe
--------------
`storage.py` no tenía ningún test hasta v1.86, y es de donde salen los cuatro
parámetros dinámicos que alimentan `decide_charge` (risk factor, sesgo solar,
consumo nocturno y diario) más el perfil de consumo por franja que gobierna la
corriente de carga. Un error aquí no rompe nada de forma visible: devuelve un
número plausible y la decisión nocturna se desvía en silencio.

Cómo se prueba sin base de datos
--------------------------------
Todas las lecturas pasan por `storage._query` y todas las escrituras por
`storage._write_points`. Se sustituyen por dobles que devuelven tablas
sintéticas y capturan los puntos escritos, así que estos tests corren sin
InfluxDB, sin red y sin depender de qué haya guardado la instalación.

El doble imita la interfaz que consume el código: `table.records`, y de cada
registro `get_value()`, `get_time()` y `values` (el dict que deja `pivot`).
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import app.storage as storage
from app.config import InfluxDBConfig

passed = failed = 0


def check(desc, got, exp, tol=1e-6):
    global passed, failed
    if isinstance(exp, float) and isinstance(got, (int, float)) and got is not None:
        ok = abs(got - exp) <= tol
    else:
        ok = got == exp
    if ok:
        passed += 1
        print(f"  ✓  {desc} → {got}")
    else:
        failed += 1
        print(f"  ✗  {desc} → {got}  (esperado {exp})")


# ── Dobles de InfluxDB ─────────────────────────────────────────────────────
class _Rec:
    def __init__(self, value=None, time=None, values=None):
        self._value, self._time, self.values = value, time, values or {}

    def get_value(self):
        return self._value

    def get_time(self):
        return self._time


class _Table:
    def __init__(self, records):
        self.records = records


def tabla(records):
    return [_Table(records)]


CFG = InfluxDBConfig(enabled=True, url="http://x", org="o", bucket="b", token="t")
CFG_OFF = InfluxDBConfig(enabled=False)


def con_query(fn):
    """Sustituye storage._query por `fn` mientras dure el bloque."""
    class _Ctx:
        def __enter__(self):
            self.real = storage._query
            storage._query = fn
        def __exit__(self, *a):
            storage._query = self.real
    return _Ctx()


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# ── Media de un campo de stats_diarias (consumo nocturno y diario) ─────────
def test_avg_field():
    print("=== get_avg_night_consumption / get_avg_daily_consumption ===")

    def q(cfg, flux):
        # El filtro `_value > 0.5` lo aplica Flux en producción; aquí se simula
        # devolviendo ya solo los valores que pasarían el filtro.
        assert "stats_diarias" in flux and "night_consumption_kwh" in flux, flux
        return tabla([_Rec(value=v) for v in [4.0, 5.0, 6.0] * 5])   # 15 días

    with con_query(q):
        check("media de 15 días", storage.get_avg_night_consumption(CFG, 30, 14), 5.0)
        check("con mínimo 16 → fallback (None)",
              storage.get_avg_night_consumption(CFG, 30, 16), None)
    check("InfluxDB deshabilitado → None",
          storage.get_avg_night_consumption(CFG_OFF, 30, 1), None)

    def q_diario(cfg, flux):
        assert "consumption_kwh" in flux and "night_consumption_kwh" not in flux, flux
        return tabla([_Rec(value=v) for v in [16.0, 20.0]])

    with con_query(q_diario):
        check("consumo diario consulta su propio campo",
              storage.get_avg_daily_consumption(CFG, 30, 2), 18.0)

    # Justo en el mínimo se acepta: el criterio es "menos de min_days → fallback".
    def q_exacto(cfg, flux):
        return tabla([_Rec(value=3.0) for _ in range(14)])

    with con_query(q_exacto):
        check("exactamente el mínimo se acepta",
              storage.get_avg_night_consumption(CFG, 30, 14), 3.0)
        check("uno menos del mínimo → None",
              storage.get_avg_night_consumption(CFG, 30, 15), None)

    def q_rota(cfg, flux):
        raise RuntimeError("InfluxDB caído")

    with con_query(q_rota):
        check("consulta fallida → None (no rompe el ciclo)",
              storage.get_avg_night_consumption(CFG, 30, 1), None)


# ── El JOIN ciclo_carga ↔ stats_diarias ────────────────────────────────────
def _pairs_query(ciclo, stats):
    """Doble que responde a las dos consultas de _forecast_real_pairs.

    `ciclo`: [(datetime_utc, p10, p50)] — el pivot deja los campos en rec.values
    `stats`: [(datetime_utc, solar_kwh)]
    """
    def q(cfg, flux):
        if "ciclo_carga" in flux:
            return tabla([_Rec(time=t, values={"forecast_p10_kwh": p10,
                                               "forecast_p50_kwh": p50})
                          for t, p10, p50 in ciclo])
        return tabla([_Rec(time=t, value=v) for t, v in stats])
    return q


def test_join_forecast_real():
    print("\n=== _forecast_real_pairs: JOIN forecast_date = ciclo_UTC + 1 día ===")

    # Ciclo de las 23:55 locales = 21:55 UTC en verano → el forecast es para el
    # día siguiente, cuyo stats_diarias lleva timestamp de medianoche UTC.
    ciclo = [(utc(2026, 8, 20, 21, 55), 10.0, 20.0)]
    stats = [(utc(2026, 8, 21), 18.0)]
    with con_query(_pairs_query(ciclo, stats)):
        check("ciclo de la noche cruza con el día siguiente",
              storage._forecast_real_pairs(CFG, 30), [(10.0, 20.0, 18.0)])

    # Reinicio a las 00:30 UTC: sigue cuadrando (día siguiente = ese mismo día).
    ciclo = [(utc(2026, 8, 21, 0, 30), 10.0, 20.0)]
    stats = [(utc(2026, 8, 22), 15.0)]
    with con_query(_pairs_query(ciclo, stats)):
        check("ejecución de madrugada cruza con el día siguiente",
              storage._forecast_real_pairs(CFG, 30), [(10.0, 20.0, 15.0)])

    # Ejecución manual a mediodía UTC: apunta a un día que aún no tiene stats.
    # Se descarta en silencio, que es el comportamiento documentado (I-6).
    ciclo = [(utc(2026, 8, 21, 12, 0), 10.0, 20.0)]
    stats = [(utc(2026, 8, 21), 18.0)]
    with con_query(_pairs_query(ciclo, stats)):
        check("ciclo de mediodía sin stats del día siguiente → descartado",
              storage._forecast_real_pairs(CFG, 30), [])

    # Día sin producción real almacenada: no hay par que formar.
    ciclo = [(utc(2026, 8, 20, 21, 55), 10.0, 20.0),
             (utc(2026, 8, 21, 21, 55), 11.0, 21.0)]
    stats = [(utc(2026, 8, 21), 18.0)]
    with con_query(_pairs_query(ciclo, stats)):
        check("solo se emparejan los días con ambos datos",
              storage._forecast_real_pairs(CFG, 30), [(10.0, 20.0, 18.0)])

    # Un ciclo al que le falta un campo del pivot no debe colarse a medias.
    def q_incompleto(cfg, flux):
        if "ciclo_carga" in flux:
            return tabla([_Rec(time=utc(2026, 8, 20, 21, 55),
                               values={"forecast_p50_kwh": 20.0})])
        return tabla([_Rec(time=utc(2026, 8, 21), value=18.0)])

    with con_query(q_incompleto):
        check("ciclo sin p10 → descartado", storage._forecast_real_pairs(CFG, 30), [])


def test_risk_factor():
    print("\n=== get_dynamic_risk_factor: rf = (real − p50) / (p10 − p50) ===")

    def dias(n, p10, p50, real, base=date(2026, 8, 1)):
        ciclo, stats = [], []
        for i in range(n):
            d = base + timedelta(days=i)
            ciclo.append((datetime(d.year, d.month, d.day, 21, 55, tzinfo=timezone.utc),
                          p10, p50))
            nxt = d + timedelta(days=1)
            stats.append((datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc), real))
        return _pairs_query(ciclo, stats)

    # real == p50 → rf 0 (el forecast central acertó, no hace falta pesimismo)
    with con_query(dias(14, 10.0, 20.0, 20.0)):
        check("real = p50 → rf 0", storage.get_dynamic_risk_factor(CFG, 30, 14), 0.0)
    # real == p10 → rf 1 (el día salió tan malo como el escenario pesimista)
    with con_query(dias(14, 10.0, 20.0, 10.0)):
        check("real = p10 → rf 1", storage.get_dynamic_risk_factor(CFG, 30, 14), 1.0)
    # a mitad de camino → 0.5
    with con_query(dias(14, 10.0, 20.0, 15.0)):
        check("real a medio camino → rf 0.5", storage.get_dynamic_risk_factor(CFG, 30, 14), 0.5)
    # Producción por encima del p50: el clamp impide un rf negativo.
    with con_query(dias(14, 10.0, 20.0, 25.0)):
        check("real > p50 → clamp a 0", storage.get_dynamic_risk_factor(CFG, 30, 14), 0.0)
    # Peor que el p10: clamp a 1.
    with con_query(dias(14, 10.0, 20.0, 2.0)):
        check("real < p10 → clamp a 1", storage.get_dynamic_risk_factor(CFG, 30, 14), 1.0)
    # Pocos días → fallback
    with con_query(dias(13, 10.0, 20.0, 15.0)):
        check("13 días con mínimo 14 → None",
              storage.get_dynamic_risk_factor(CFG, 30, 14), None)
    # p10 >= p50 sería una división por cero o un signo invertido: se descarta.
    with con_query(dias(14, 20.0, 20.0, 15.0)):
        check("p10 == p50 → días descartados → None",
              storage.get_dynamic_risk_factor(CFG, 30, 14), None)
    check("InfluxDB deshabilitado → None",
          storage.get_dynamic_risk_factor(CFG_OFF, 30, 1), None)


def test_solar_bias():
    print("\n=== get_dynamic_solar_bias: factor = real / p50, clamp [0.5, 1.5] ===")

    def dias(n, p50, real, base=date(2026, 8, 1)):
        ciclo, stats = [], []
        for i in range(n):
            d = base + timedelta(days=i)
            ciclo.append((datetime(d.year, d.month, d.day, 21, 55, tzinfo=timezone.utc),
                          p50 / 2, p50))
            nxt = d + timedelta(days=1)
            stats.append((datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc), real))
        return _pairs_query(ciclo, stats)

    with con_query(dias(14, 20.0, 20.0)):
        check("forecast clavado → 1.0", storage.get_dynamic_solar_bias(CFG, 30, 14), 1.0)
    # El caso medido en esta instalación: Solcast sobreestima ~19%.
    with con_query(dias(14, 20.0, 16.2)):
        check("Solcast sobreestima → 0.81", storage.get_dynamic_solar_bias(CFG, 30, 14), 0.81)
    # Clamp defensivo ante muestras absurdas.
    with con_query(dias(14, 20.0, 2.0)):
        check("ratio 0.1 → clamp inferior 0.5",
              storage.get_dynamic_solar_bias(CFG, 30, 14), 0.5)
    with con_query(dias(14, 20.0, 60.0)):
        check("ratio 3.0 → clamp superior 1.5",
              storage.get_dynamic_solar_bias(CFG, 30, 14), 1.5)
    with con_query(dias(10, 20.0, 18.0)):
        check("10 días con mínimo 14 → None",
              storage.get_dynamic_solar_bias(CFG, 30, 14), None)


# ── Perfil de consumo por franja ───────────────────────────────────────────
def test_house_profile():
    print("\n=== get_house_power_profile: mediana por franja de 30 min ===")

    def perfil(dias_por_franja, valores_por_franja=None):
        """Genera `dias_por_franja` días con dos franjas: 08:00 y 08:30."""
        recs = []
        for i in range(dias_por_franja):
            d = utc(2026, 8, 1) + timedelta(days=i)
            v8, v830 = (valores_por_franja or (0.3, 0.6))
            recs.append(_Rec(time=d.replace(hour=8, minute=0), value=v8))
            recs.append(_Rec(time=d.replace(hour=8, minute=30), value=v830))
        return lambda cfg, flux: tabla(recs)

    with con_query(perfil(14)):
        p = storage.get_house_power_profile(CFG, 30, 14)
        check("dos franjas", sorted(p or {}), ["08:00", "08:30"])
        check("mediana de la franja 08:00", (p or {}).get("08:00"), 0.3)
        check("clave = hora de inicio de la franja", "08:30" in (p or {}), True)

    with con_query(perfil(13)):
        check("franja peor cubierta por debajo del mínimo → None",
              storage.get_house_power_profile(CFG, 30, 14), None)

    # Una sola franja infra-muestreada invalida el perfil entero (criterio
    # conservador, igual que el resto de dinámicos).
    def q_desigual(cfg, flux):
        recs = []
        for i in range(20):
            d = utc(2026, 8, 1) + timedelta(days=i)
            recs.append(_Rec(time=d.replace(hour=8, minute=0), value=0.3))
        for i in range(3):
            d = utc(2026, 8, 1) + timedelta(days=i)
            recs.append(_Rec(time=d.replace(hour=14, minute=0), value=1.2))
        return tabla(recs)

    with con_query(q_desigual):
        check("una franja con 3 días invalida el perfil completo",
              storage.get_house_power_profile(CFG, 30, 14), None)

    # La mediana, no la media: un horno de 20 min no debe mover el perfil.
    def q_atipico(cfg, flux):
        recs = []
        for i, v in enumerate([0.3] * 14 + [9.0]):
            d = utc(2026, 8, 1) + timedelta(days=i)
            recs.append(_Rec(time=d.replace(hour=8, minute=0), value=v))
        return tabla(recs)

    with con_query(q_atipico):
        p = storage.get_house_power_profile(CFG, 30, 14)
        check("un valor atípico no arrastra la mediana", (p or {}).get("08:00"), 0.3)

    with con_query(lambda cfg, flux: tabla([])):
        check("sin datos → None", storage.get_house_power_profile(CFG, 30, 14), None)


# ── Último día con dato (detección del hueco del backfill) ─────────────────
def test_last_real_solar_date():
    print("\n=== get_last_real_solar_date: por campo, no global ===")
    pedidos = []

    def q(cfg, flux):
        pedidos.append(flux)
        # `real_kwh` existe desde antiguo; `house_kwh` se añadió en v1.73, así que
        # mirando solo el primero los días viejos parecerían completos y el campo
        # nuevo no se rellenaría nunca.
        if "house_kwh" in flux:
            return tabla([_Rec(time=utc(2026, 8, 10), value=1.0)])
        return tabla([_Rec(time=utc(2026, 8, 25), value=1.0)])

    with con_query(q):
        check("real_kwh", storage.get_last_real_solar_date(CFG, "real_kwh"), date(2026, 8, 25))
        check("house_kwh va más atrasado",
              storage.get_last_real_solar_date(CFG, "house_kwh"), date(2026, 8, 10))
    check("el campo pedido llega a la consulta", "house_kwh" in pedidos[-1], True)

    with con_query(lambda cfg, flux: tabla([])):
        check("sin datos → None", storage.get_last_real_solar_date(CFG, "real_kwh"), None)


# ── Escrituras: el punto que se manda a InfluxDB ───────────────────────────
def con_write(captura):
    class _Ctx:
        def __enter__(self):
            self.real = storage._write_points
            storage._write_points = lambda cfg, points: captura.extend(points)
        def __exit__(self, *a):
            storage._write_points = self.real
    return _Ctx()


def test_write_daily_stats():
    print("\n=== write_daily_stats: el timestamp que hace posible el JOIN ===")
    from app.logger_reader import DailyStats
    stats = DailyStats(
        date=date(2026, 8, 21), device_id="ABC123", solar_kwh=30.0,
        grid_consumed_kwh=4.0, grid_exported_kwh=10.0, consumption_kwh=19.0,
        night_consumption_kwh=4.8, soc_start_pct=40.0, soc_end_pct=90.0,
        peak_soc_pct=95.0, battery_charged_kwh=16.0, records=1440,
        half_hour_solar_kwh=[0.0] * 48, half_hour_house_kwh=[0.0] * 48,
        half_hour_grid_import_kwh=[0.0] * 48, half_hour_grid_export_kwh=[0.0] * 48,
    )
    puntos = []
    with con_write(puntos):
        storage.write_daily_stats(CFG, stats)
    check("un punto", len(puntos), 1)
    p = puntos[0]
    check("measurement", p["measurement"], "stats_diarias")
    check("tag device_id", p["tags"]["device_id"], "ABC123")
    # Medianoche UTC del día de los datos: es lo que permite que el JOIN con
    # ciclo_carga (forecast_date = ciclo_UTC.date()+1) encuentre el día correcto.
    check("timestamp = medianoche UTC del día", p["time"], "2026-08-21T00:00:00+00:00")
    check("night_consumption_kwh", p["fields"]["night_consumption_kwh"], 4.8)
    check("peak_soc_pct (v1.78)", p["fields"]["peak_soc_pct"], 95.0)
    check("battery_charged_kwh (v1.78)", p["fields"]["battery_charged_kwh"], 16.0)
    check("records va como float", isinstance(p["fields"]["records"], float), True)

    puntos.clear()
    with con_write(puntos):
        storage.write_daily_stats(CFG_OFF, stats)
    check("InfluxDB deshabilitado → no escribe", puntos, [])


def test_write_charge_current():
    print("\n=== write_charge_current: registro de cambios de corriente ===")
    from types import SimpleNamespace as NS
    estado = NS(soc_pct=55.0, battery_temp_c=34.0, battery_voltage_v=51.2)
    modo = "SOLAR (limitada por sol — más A no captan más)"
    puntos = []
    with con_write(puntos):
        storage.write_charge_current(
            CFG, current_a=22, previous_a=66, mode=modo, state=estado,
            now=datetime(2026, 8, 21, 14, 3, 7), calculated_a=16,
        )
    check("un punto", len(puntos), 1)
    p = puntos[0]
    check("measurement", p["measurement"], "corriente_carga")
    # El tag es el primer token para poder agrupar por modo en los informes; el
    # texto completo va al campo `detail`.
    check("tag mode = primer token", p["tags"]["mode"], "SOLAR")
    check("detail = modo completo", p["fields"]["detail"], modo)
    check("current_a = el acotado que se escribió", p["fields"]["current_a"], 22.0)
    check("calculated_a = el crudo del algoritmo (v1.67)", p["fields"]["calculated_a"], 16.0)
    check("delta_a = actual − previo", p["fields"]["delta_a"], -44.0)
    check("soc del momento", p["fields"]["soc_pct"], 55.0)
    check("verified por defecto", p["fields"]["verified"], True)
    check("dry_run por defecto", p["fields"]["dry_run"], False)
    # Hora local etiquetada como UTC: los componentes se conservan tal cual, que
    # es lo que espera /api/charge_current_today al pintar la hora.
    check("timestamp conserva la hora local", p["fields"] and p["time"],
          "2026-08-21T14:03:07+00:00")

    # Sin calculated_a explícito se guarda el mismo valor que current_a (los
    # registros anteriores a v1.67 no lo tenían).
    puntos.clear()
    with con_write(puntos):
        storage.write_charge_current(
            CFG, current_a=30, previous_a=30, mode="VALLE", state=estado,
            now=datetime(2026, 8, 21, 3, 0, 0), dry_run=True, verified=False,
        )
    check("calculated_a por defecto = current_a", puntos[0]["fields"]["calculated_a"], 30.0)
    check("dry_run se registra", puntos[0]["fields"]["dry_run"], True)
    check("verified=False se registra", puntos[0]["fields"]["verified"], False)


def main():
    test_avg_field()
    test_join_forecast_real()
    test_risk_factor()
    test_solar_bias()
    test_house_profile()
    test_last_real_solar_date()
    test_write_daily_stats()
    test_write_charge_current()

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        return 1
    print(f"✓ Todos los tests de storage.py pasaron ({passed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
