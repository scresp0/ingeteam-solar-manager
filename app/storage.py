"""
storage.py — almacenamiento de datos en InfluxDB 2.x.

Guarda dos tipos de puntos por ciclo nocturno:

1. measurement: ciclo_carga
   — decisión tomada por el algoritmo + estado del inversor en ese momento

2. measurement: stats_diarias
   — acumulados del día anterior leídos del datalogger del inversor
"""

import logging
from datetime import datetime, timezone

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
            "solar_kwh":           stats.solar_kwh,
            "grid_consumed_kwh":   stats.grid_consumed_kwh,
            "grid_exported_kwh":   stats.grid_exported_kwh,
            "consumption_kwh":     stats.consumption_kwh,
            "soc_start_pct":       stats.soc_start_pct,
            "soc_end_pct":         stats.soc_end_pct,
            "records":             float(stats.records),
        }
    }

    _write_point(cfg, point)
    logger.info(
        f"Stats {stats.date} guardadas en InfluxDB "
        f"(solar={stats.solar_kwh} kWh, red={stats.grid_consumed_kwh} kWh)"
    )


def _write_point(cfg: InfluxDBConfig, point: dict) -> None:
    """Escribe un punto en InfluxDB usando la API HTTP v2."""
    try:
        from influxdb_client import InfluxDBClient, WriteOptions
        from influxdb_client.client.write_api import SYNCHRONOUS

        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            write_api = client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=cfg.bucket, record=point)

    except ImportError:
        raise StorageError(
            "influxdb-client no está instalado. "
            "Añade 'influxdb-client' al requirements.txt"
        )
    except Exception as e:
        raise StorageError(f"Error escribiendo en InfluxDB: {e}") from e
