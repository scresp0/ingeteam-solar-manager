"""
storage.py — almacenamiento de datos en InfluxDB 2.x.

Guarda tres tipos de puntos por ciclo nocturno:

1. measurement: ciclo_carga
   — decisión tomada por el algoritmo + estado del inversor en ese momento

2. measurement: stats_diarias
   — acumulados del día anterior leídos del datalogger del inversor

3. measurement: solar_media_hora
   — producción solar y forecast en intervalos de 30 min
     forecast_p50/p10/p90_kwh: escritos en el ciclo nocturno para mañana
     real_kwh: escritos al día siguiente con los datos del datalogger
     Timestamps en "hora local española etiquetada como UTC" (igual que stats_diarias)
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import InfluxDBConfig
from app.decision import DecisionInput, ChargeDecision
from app.inverter import InverterState
from app.logger_reader import DailyStats

logger = logging.getLogger(__name__)


class StorageError(Exception):
    pass


def write_cycle(
    cfg: InfluxDBConfig,
    inp: DecisionInput,
    result: ChargeDecision,
    state: InverterState,
    solcast_error: bool = False,
    automation_ok: bool = True,
    timestamp: datetime | None = None,
) -> None:
    """
    Guarda el resultado del ciclo nocturno en InfluxDB.

    Args:
        cfg:           configuración de InfluxDB
        inp:           input del algoritmo de decisión
        result:        resultado del algoritmo
        state:         estado del inversor leído vía MODBUS
        solcast_error: True si Solcast falló y se usó fallback
        automation_ok: True si se escribió correctamente en el inversor
        timestamp:     timestamp del punto (por defecto: ahora)
    """
    if not cfg.enabled:
        return

    ts = timestamp or datetime.now(timezone.utc)

    point = {
        "measurement": "ciclo_carga",
        "time": ts.isoformat(),
        "fields": {
            # Estado del inversor
            "soc_actual_pct":       state.soc_pct,
            "soh_pct":              state.soh_pct,
            "battery_power_w":      float(state.battery_power_w),
            "battery_voltage_v":    state.battery_voltage_v,
            "battery_temp_c":       state.battery_temp_c,
            # Previsión Solcast
            "forecast_p10_kwh":     inp.forecast_day1.p10,
            "forecast_p50_kwh":     inp.forecast_day1.p50,
            "forecast_p90_kwh":     inp.forecast_day1.p90,
            "solar_effective_kwh":  result.solar_effective_kwh,
            # Cálculo
            "energy_stored_kwh":    result.energy_stored_kwh,
            "energy_at_dawn_kwh":   result.energy_at_dawn_kwh,
            "deficit_kwh":          result.deficit_kwh,
            # Decisión
            "charge_needed":        result.charge_needed,
            "target_soc_pct":       result.target_soc_pct,
            "target_kwh":           result.target_kwh,
            "valley_day_skip":      result.valley_day_skip,
            # Metadatos
            "solcast_error":        solcast_error,
            "automation_ok":        automation_ok,
            "dry_run":              result.dry_run,
        }
    }

    _write_point(cfg, point)
    logger.info(f"Ciclo guardado en InfluxDB (charge_needed={result.charge_needed}, soc={state.soc_pct}%)")


def write_daily_stats(cfg: InfluxDBConfig, stats: DailyStats) -> None:
    """
    Guarda los acumulados diarios del inversor en InfluxDB.

    Args:
        cfg:   configuración de InfluxDB
        stats: acumulados calculados por logger_reader
    """
    if not cfg.enabled:
        return

    # Timestamp = medianoche del día de los datos (en UTC)
    ts = datetime.combine(stats.date, datetime.min.time()).replace(tzinfo=timezone.utc)

    point = {
        "measurement": "stats_diarias",
        "time": ts.isoformat(),
        "tags": {
            "device_id": stats.device_id,
        },
        "fields": {
            "solar_kwh":              stats.solar_kwh,
            "grid_consumed_kwh":      stats.grid_consumed_kwh,
            "grid_exported_kwh":      stats.grid_exported_kwh,
            "consumption_kwh":        stats.consumption_kwh,
            "night_consumption_kwh":  stats.night_consumption_kwh,
            "soc_start_pct":          stats.soc_start_pct,
            "soc_end_pct":            stats.soc_end_pct,
            "records":                float(stats.records),
        }
    }

    _write_point(cfg, point)
    logger.info(
        f"Stats {stats.date} guardadas en InfluxDB "
        f"(solar={stats.solar_kwh} kWh, red={stats.grid_consumed_kwh} kWh)"
    )


def get_avg_night_consumption(
    cfg: InfluxDBConfig,
    window_days: int = 30,
    min_days: int = 14,
) -> float | None:
    """
    Devuelve el consumo nocturno medio (kWh, 00:00–07:59) de los últimos
    window_days días almacenados en InfluxDB.

    Devuelve None si:
    - InfluxDB no está habilitado
    - Hay menos de min_days registros válidos en la ventana
    - La consulta falla (se loguea como warning)
    """
    if not cfg.enabled:
        return None

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{window_days}d)
  |> filter(fn: (r) => r._measurement == "stats_diarias" and r._field == "night_consumption_kwh")
  |> filter(fn: (r) => r._value > 0.5)
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            values = [rec.get_value() for table in tables for rec in table.records]

        if len(values) < min_days:
            logger.debug(
                f"Consumo nocturno dinámico: solo {len(values)} días válidos "
                f"(mínimo {min_days}) — usando fallback"
            )
            return None

        avg = sum(values) / len(values)
        logger.debug(f"Consumo nocturno dinámico: {len(values)} días, media={avg:.3f} kWh")
        return round(avg, 3)

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo consultar consumo nocturno dinámico en InfluxDB: {e}")
        return None


def get_dynamic_risk_factor(
    cfg: InfluxDBConfig,
    window_days: int = 30,
    min_days: int = 14,
) -> float | None:
    """
    Calcula el risk_factor óptimo comparando predicciones históricas de Solcast
    con la producción solar real almacenada en InfluxDB.

    Fórmula por día: rf_óptimo = (solar_real - p50) / (p10 - p50), clamp [0,1]

    Devuelve None si:
    - InfluxDB no está habilitado
    - Hay menos de min_days pares válidos en la ventana
    - La consulta falla
    """
    if not cfg.enabled:
        return None

    from datetime import date as date_type, timedelta

    fetch_days = window_days + 2  # margen para desfase UTC+1/+2

    q_ciclo = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{fetch_days}d)
  |> filter(fn: (r) => r._measurement == "ciclo_carga" and
     (r._field == "forecast_p10_kwh" or r._field == "forecast_p50_kwh"))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
"""

    q_stats = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{fetch_days}d)
  |> filter(fn: (r) => r._measurement == "stats_diarias" and r._field == "solar_kwh")
  |> filter(fn: (r) => r._value > 0.5)
"""

    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            api = client.query_api()
            ciclo_tables = api.query(q_ciclo, org=cfg.org)
            stats_tables = api.query(q_stats, org=cfg.org)

        # date -> solar_kwh (timestamp de stats_diarias = medianoche UTC del día)
        solar_by_date: dict = {}
        for table in stats_tables:
            for rec in table.records:
                solar_by_date[rec.get_time().date()] = rec.get_value()

        rf_values = []
        for table in ciclo_tables:
            for rec in table.records:
                p10 = rec.values.get("forecast_p10_kwh")
                p50 = rec.values.get("forecast_p50_kwh")
                if p10 is None or p50 is None or p10 >= p50:
                    continue
                # ciclo corre ~22-23h UTC → forecast es para el día siguiente UTC
                forecast_date = rec.get_time().date() + timedelta(days=1)
                solar_real = solar_by_date.get(forecast_date)
                if solar_real is None:
                    continue
                rf = (solar_real - p50) / (p10 - p50)
                rf_values.append(max(0.0, min(1.0, rf)))

        if len(rf_values) < min_days:
            logger.debug(
                f"Risk factor dinámico: solo {len(rf_values)} días válidos "
                f"(mínimo {min_days}) — usando fallback"
            )
            return None

        avg_rf = sum(rf_values) / len(rf_values)
        logger.debug(f"Risk factor dinámico: {len(rf_values)} días, media={avg_rf:.3f}")
        return round(avg_rf, 3)

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo calcular risk_factor dinámico desde InfluxDB: {e}")
        return None


def write_half_hour_solar(cfg: InfluxDBConfig, stats: DailyStats) -> None:
    """
    Guarda la producción solar real (ayer) en resolución de 30 min.

    Escribe 48 puntos en solar_media_hora con el campo real_kwh.
    Timestamps en "hora local española etiquetada como UTC" (igual que stats_diarias).
    """
    if not cfg.enabled:
        return

    midnight_utc = datetime.combine(stats.date, datetime.min.time()).replace(tzinfo=timezone.utc)
    points = [
        {
            "measurement": "solar_media_hora",
            "time": (midnight_utc + timedelta(minutes=slot * 30)).isoformat(),
            "fields": {"real_kwh": kwh},
        }
        for slot, kwh in enumerate(stats.half_hour_solar_kwh)
    ]
    _write_points(cfg, points)
    logger.info(f"Solar media hora {stats.date} guardado en InfluxDB ({len(points)} slots)")


def write_half_hour_forecast(
    cfg: InfluxDBConfig,
    intervals: list[dict],
    tz_name: str = "Europe/Madrid",
) -> None:
    """
    Guarda el forecast de Solcast para mañana en resolución de 30 min.

    Escribe hasta 48 puntos en solar_media_hora con campos forecast_p50/p10/p90_kwh.
    Convierte period_end UTC a hora local y etiqueta como UTC para alinear con real_kwh.
    """
    if not cfg.enabled or not intervals:
        return

    from zoneinfo import ZoneInfo
    _tz = ZoneInfo(tz_name)

    points = []
    for item in intervals:
        try:
            end_str = item["period_end"].rstrip("Z").split(".")[0] + "+00:00"
            end_utc = datetime.fromisoformat(end_str)
            end_local = end_utc.astimezone(_tz)
            start_local = end_local - timedelta(minutes=30)
            # "local labeled UTC" — mismo convenio que stats_diarias
            ts = datetime(
                start_local.year, start_local.month, start_local.day,
                start_local.hour, start_local.minute, 0,
                tzinfo=timezone.utc,
            )
            points.append({
                "measurement": "solar_media_hora",
                "time": ts.isoformat(),
                "fields": {
                    "forecast_p50_kwh": round(item.get("pv_estimate",   0.0) * 0.5, 4),
                    "forecast_p10_kwh": round(item.get("pv_estimate10", 0.0) * 0.5, 4),
                    "forecast_p90_kwh": round(item.get("pv_estimate90", 0.0) * 0.5, 4),
                },
            })
        except (KeyError, ValueError) as e:
            logger.warning(f"Intervalo Solcast con formato inesperado, ignorado: {e}")

    if not points:
        return

    _write_points(cfg, points)
    logger.info(f"Forecast media hora guardado en InfluxDB ({len(points)} slots)")


def get_solar_history_day(cfg: InfluxDBConfig, date_str: str) -> dict:
    """Historial de producción solar y forecast para un día (YYYY-MM-DD)."""
    if not cfg.enabled:
        return {}
    stop = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return _solar_history(cfg, date_str, stop)


def get_solar_history_range(cfg: InfluxDBConfig, start_str: str, end_str: str) -> dict:
    """Media horaria de producción solar y forecast en un rango de fechas (end exclusivo)."""
    if not cfg.enabled:
        return {}
    return _solar_history(cfg, start_str, end_str)


def _solar_history(cfg: InfluxDBConfig, start_str: str, end_str: str) -> dict:
    """Consulta solar_media_hora y devuelve medias horarias de real y forecast."""
    from collections import defaultdict

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: {start_str}T00:00:00Z, stop: {end_str}T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "solar_media_hora")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            recs = [rec.values for table in tables for rec in table.records]
    except Exception as e:
        logger.warning(f"Error consultando solar_media_hora {start_str}→{end_str}: {e}")
        return {}

    # Acumular por (día, hora) — luego promediamos entre días
    by_day: dict = defaultdict(lambda: defaultdict(lambda: {
        "real": 0.0, "p50": 0.0, "p10": 0.0, "p90": 0.0,
        "has_real": False, "has_fc": False,
    }))

    for v in recs:
        ts = v.get("_time")
        if ts is None:
            continue
        h   = ts.hour
        day = ts.date().isoformat()
        s   = by_day[day][h]

        real = v.get("real_kwh")
        p50  = v.get("forecast_p50_kwh")
        p10  = v.get("forecast_p10_kwh")
        p90  = v.get("forecast_p90_kwh")

        if real is not None and real >= 0:
            s["real"] += real
            s["has_real"] = True
        if p50 is not None:
            s["p50"] += p50
            s["p10"] += (p10 or 0.0)
            s["p90"] += (p90 or 0.0)
            s["has_fc"] = True

    all_hours: set = set()
    for day_data in by_day.values():
        all_hours.update(day_data.keys())
    all_hours_sorted = sorted(all_hours)

    if not all_hours_sorted:
        return {
            "hours": [], "p50_kw": [], "p10_kw": [], "p90_kw": [], "real_kw": [],
            "total_p50_kwh": 0.0, "total_real_kwh": 0.0,
            "has_real": False, "has_forecast": False, "days_with_data": 0,
        }

    days_with_real = {d for d, dh in by_day.items() if any(s["has_real"] for s in dh.values())}
    days_with_fc   = {d for d, dh in by_day.items() if any(s["has_fc"]   for s in dh.values())}

    p50_kw, p10_kw, p90_kw, real_kw = [], [], [], []
    for h in all_hours_sorted:
        fc_slots = [by_day[d][h] for d in by_day if by_day[d][h]["has_fc"]]
        re_slots = [by_day[d][h] for d in by_day if by_day[d][h]["has_real"]]

        if fc_slots:
            n = len(fc_slots)
            p50_kw.append(round(sum(s["p50"] for s in fc_slots) / n, 4))
            p10_kw.append(round(sum(s["p10"] for s in fc_slots) / n, 4))
            p90_kw.append(round(sum(s["p90"] for s in fc_slots) / n, 4))
        else:
            p50_kw.append(None); p10_kw.append(None); p90_kw.append(None)

        real_kw.append(
            round(sum(s["real"] for s in re_slots) / len(re_slots), 4) if re_slots else None
        )

    total_p50  = round(sum(v for v in p50_kw  if v is not None), 3)
    total_real = round(sum(v for v in real_kw if v is not None), 3)

    return {
        "hours":          all_hours_sorted,
        "p50_kw":         p50_kw,
        "p10_kw":         p10_kw,
        "p90_kw":         p90_kw,
        "real_kw":        real_kw,
        "total_p50_kwh":  total_p50,
        "total_real_kwh": total_real,
        "has_real":       len(days_with_real) > 0,
        "has_forecast":   len(days_with_fc) > 0,
        "days_with_data": max(len(days_with_real), len(days_with_fc)),
    }


def _write_points(cfg: InfluxDBConfig, points: list[dict]) -> None:
    """Escribe una lista de puntos en InfluxDB en un único batch."""
    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            write_api = client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=cfg.bucket, record=points)

    except ImportError:
        raise StorageError(
            "influxdb-client no está instalado. "
            "Añade 'influxdb-client' al requirements.txt"
        )
    except Exception as e:
        raise StorageError(f"Error escribiendo en InfluxDB: {e}") from e


def _write_point(cfg: InfluxDBConfig, point: dict) -> None:
    _write_points(cfg, [point])
