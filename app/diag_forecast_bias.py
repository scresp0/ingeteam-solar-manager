"""
diag_forecast_bias.py — diagnóstico ad-hoc del sesgo Solcast vs realidad.

Ejecutar dentro del contenedor (para resolver `influxdb:8086`):
  docker compose run --rm solar-manager python -m app.diag_forecast_bias

Saca de InfluxDB todos los pares (forecast_p10/p50/p90, solar_real) usando
el mismo JOIN que get_dynamic_risk_factor (forecast_date = ciclo_UTC.date() + 1)
y reporta:

  - factor_calib = real / p50   (lo que sugerí en la conversación)
  - bias_abs     = real - p50   (kWh)
  - estadísticas: media, mediana, std, min, max, n
  - desglose por mes si hay >= 60 días
  - distribución del rf óptimo por día (para visualizar el sesgo del clamp)
"""
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import load_config


WINDOW_DAYS = 90  # ventana de búsqueda generosa para diagnóstico


def main() -> None:
    cfg = load_config("config.yaml")
    if not cfg.influxdb.enabled:
        print("InfluxDB deshabilitado en config — nada que hacer.")
        return

    fetch_days = WINDOW_DAYS + 2

    q_ciclo = f"""
from(bucket: "{cfg.influxdb.bucket}")
  |> range(start: -{fetch_days}d)
  |> filter(fn: (r) => r._measurement == "ciclo_carga" and
     (r._field == "forecast_p10_kwh" or r._field == "forecast_p50_kwh"
      or r._field == "forecast_p90_kwh"))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
"""

    q_stats = f"""
from(bucket: "{cfg.influxdb.bucket}")
  |> range(start: -{fetch_days}d)
  |> filter(fn: (r) => r._measurement == "stats_diarias" and r._field == "solar_kwh")
  |> filter(fn: (r) => r._value > 0.5)
"""

    from influxdb_client import InfluxDBClient
    with InfluxDBClient(url=cfg.influxdb.url, token=cfg.influxdb.token,
                        org=cfg.influxdb.org) as client:
        api = client.query_api()
        ciclo_tables = api.query(q_ciclo, org=cfg.influxdb.org)
        stats_tables = api.query(q_stats, org=cfg.influxdb.org)

    solar_by_date: dict = {}
    for table in stats_tables:
        for rec in table.records:
            solar_by_date[rec.get_time().date()] = rec.get_value()

    pairs = []  # (forecast_date, p10, p50, p90, real)
    for table in ciclo_tables:
        for rec in table.records:
            p10 = rec.values.get("forecast_p10_kwh")
            p50 = rec.values.get("forecast_p50_kwh")
            p90 = rec.values.get("forecast_p90_kwh")
            if p10 is None or p50 is None or p50 <= 0:
                continue
            fdate = rec.get_time().date() + timedelta(days=1)
            real = solar_by_date.get(fdate)
            if real is None:
                continue
            pairs.append((fdate, p10, p50, p90, real))

    pairs.sort(key=lambda x: x[0])

    if not pairs:
        print("No hay pares (ciclo, stats_diarias) que cruzar. Necesitas datos "
              "históricos en ambas measurement.")
        return

    print(f"\n=== Diagnóstico de sesgo Solcast vs real ===")
    print(f"Ventana de búsqueda: {WINDOW_DAYS} días")
    print(f"Pares encontrados  : {len(pairs)}")
    print(f"Rango fechas       : {pairs[0][0]} → {pairs[-1][0]}")

    # ---- Métricas globales --------------------------------------------------
    factors    = [real / p50 for _, _, p50, _, real in pairs]
    bias_abs   = [real - p50 for _, _, p50, _, real in pairs]
    rf_optimos = []
    rf_clamped = []
    for _, p10, p50, _, real in pairs:
        if p10 >= p50:
            continue
        rf = (real - p50) / (p10 - p50)
        rf_optimos.append(rf)
        rf_clamped.append(max(0.0, min(1.0, rf)))

    def stats(name: str, xs: list[float], fmt: str = ".3f") -> None:
        if not xs:
            print(f"  {name:18s}: (vacío)")
            return
        med = statistics.median(xs)
        mean = statistics.mean(xs)
        std = statistics.stdev(xs) if len(xs) > 1 else 0.0
        print(f"  {name:18s}: media={mean:{fmt}}  mediana={med:{fmt}}  "
              f"std={std:{fmt}}  min={min(xs):{fmt}}  max={max(xs):{fmt}}  n={len(xs)}")

    print("\n-- Factor de calibración (real / p50) --")
    stats("factor_calib",  factors)
    print("\n-- Sesgo absoluto (real - p50, kWh) --")
    stats("bias_abs_kwh",  bias_abs)
    print("\n-- Risk factor óptimo por día (sin clamp) --")
    stats("rf_optimo_raw", rf_optimos)
    print("\n-- Risk factor óptimo por día (con clamp [0,1] — como en producción) --")
    stats("rf_optimo_clp", rf_clamped)

    # ---- Cuántos días saturan el clamp -------------------------------------
    n_under_p10 = sum(1 for r in rf_optimos if r > 1.0)   # real < p10
    n_over_p50  = sum(1 for r in rf_optimos if r < 0.0)   # real > p50
    n_between   = len(rf_optimos) - n_under_p10 - n_over_p50
    print(f"\n-- Distribución del rf óptimo (n={len(rf_optimos)}) --")
    print(f"  real > p50 (rf<0,  optimista clamped a 0): {n_over_p50:3d}  "
          f"({100*n_over_p50/len(rf_optimos):.0f}%)")
    print(f"  p10 ≤ real ≤ p50 (rf∈[0,1], dentro del rango): {n_between:3d}  "
          f"({100*n_between/len(rf_optimos):.0f}%)")
    print(f"  real < p10 (rf>1,  pesimista clamped a 1): {n_under_p10:3d}  "
          f"({100*n_under_p10/len(rf_optimos):.0f}%)")

    # ---- Desglose por mes si hay suficientes datos -------------------------
    if len(pairs) >= 60:
        by_month: dict = defaultdict(list)
        for fdate, _, p50, _, real in pairs:
            by_month[(fdate.year, fdate.month)].append((real / p50, real - p50))
        print(f"\n-- Desglose mensual --")
        print(f"  {'mes':10s}  {'n':>3s}  {'factor':>8s}  {'bias_kwh':>9s}")
        for (y, m), vals in sorted(by_month.items()):
            facs   = [f for f, _ in vals]
            biases = [b for _, b in vals]
            print(f"  {y}-{m:02d}     {len(vals):3d}  "
                  f"{statistics.mean(facs):8.3f}  "
                  f"{statistics.mean(biases):+9.2f}")
    else:
        print(f"\n(Menos de 60 días — sin desglose mensual; añade datos y vuelve a correr)")

    # ---- Lectura del resultado ---------------------------------------------
    print("\n=== Interpretación ===")
    mean_factor = statistics.mean(factors)
    mean_bias   = statistics.mean(bias_abs)
    if 0.95 <= mean_factor <= 1.05:
        verdict = "Solcast bien calibrado para tu instalación. El mecanismo actual basta."
    elif mean_factor < 0.95:
        verdict = (f"Solcast sobreestima ~{(1-mean_factor)*100:.0f}% de media. "
                   f"Un factor de calibración explícito tendría sentido.")
    else:
        verdict = (f"Solcast subestima ~{(mean_factor-1)*100:.0f}% de media. "
                   f"(Inusual — verifica que solar_kwh no incluye exportación negativa.)")
    print(f"  Factor medio real/p50 : {mean_factor:.3f}")
    print(f"  Sesgo medio (kWh/día) : {mean_bias:+.2f}")
    print(f"  → {verdict}")
    print()


if __name__ == "__main__":
    main()
