"""
logger_reader.py — lee el datalogger del inversor Ingeteam via HTTP.

Endpoint:
  GET http://{host}/inverter/log/{device_id}/{fecha}
  Autenticación: Basic Auth con las credenciales del inversor

Calcula acumulados diarios a partir de los datos minuto a minuto:
  - solar_kwh        : producción solar total (Pdc1 + Pdc2)
  - grid_consumed_kwh: energía consumida de red (PacMeter > 0)
  - grid_exported_kwh: energía exportada a red (EPvToGrid delta)
  - soc_start_pct    : SOC al inicio del día (00:00)
  - soc_end_pct      : SOC al final del día (23:59)
  - consumption_kwh  : consumo total estimado (Pac integral)
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from app.config import InverterConfig

logger = logging.getLogger(__name__)

LOGGER_PATH = "/inverter/log"


@dataclass
class DailyStats:
    """Acumulados diarios calculados a partir del datalogger."""
    date: date
    device_id: str
    solar_kwh: float
    grid_consumed_kwh: float
    grid_exported_kwh: float
    consumption_kwh: float
    soc_start_pct: float
    soc_end_pct: float
    records: int             # número de registros del día (max 1440)


class LoggerReaderError(Exception):
    pass


def get_yesterday_stats(cfg: InverterConfig) -> DailyStats:
    """
    Obtiene los acumulados del día anterior desde el datalogger del inversor.

    Returns:
        DailyStats con los acumulados calculados

    Raises:
        LoggerReaderError: si no se puede obtener o procesar el log
    """
    yesterday = date.today() - timedelta(days=1)
    return get_daily_stats(cfg, yesterday)


def get_daily_stats(cfg: InverterConfig, target_date: date) -> DailyStats:
    """Obtiene los acumulados de un día concreto."""
    host = cfg.get_modbus_host()
    date_str = target_date.isoformat()

    # Usar device_id configurado o autodescubrir
    if cfg.device_id:
        device_id = cfg.device_id
        logger.debug(f"Usando device_id configurado: {device_id}")
    else:
        device_id = _get_device_id(cfg, host, date_str)

    url = f"http://{host}{LOGGER_PATH}/{device_id}/{date_str}"
    logger.debug(f"Leyendo logger: {url}")

    try:
        response = requests.get(
            url,
            auth=(cfg.username, cfg.password),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        raise LoggerReaderError(f"Error HTTP al leer logger: {e}") from e
    except requests.exceptions.ConnectionError as e:
        raise LoggerReaderError(f"No se pudo conectar al inversor: {e}") from e
    except Exception as e:
        raise LoggerReaderError(f"Error inesperado leyendo logger: {e}") from e

    if data.get("code") != "ok":
        raise LoggerReaderError(f"Logger devolvió error: {data}")

    records = [entry["val"] for entry in data.get("data", [])]
    if not records:
        raise LoggerReaderError(f"No hay datos en el logger para {date_str}")

    stats = _calculate_stats(records, target_date, device_id)
    logger.info(
        f"Logger {date_str}: {stats.records} registros | "
        f"Solar={stats.solar_kwh:.2f} kWh | "
        f"Red consumida={stats.grid_consumed_kwh:.2f} kWh | "
        f"Red exportada={stats.grid_exported_kwh:.2f} kWh | "
        f"SOC {stats.soc_start_pct}% → {stats.soc_end_pct}%"
    )
    return stats


def _get_device_id(cfg: InverterConfig, host: str, date_str: str) -> str:
    """
    Obtiene el device_id del inversor haciendo una llamada al logger.
    El serial viene en el campo 'serial' de la respuesta JSON.
    """
    # Intentar con un device_id temporal para obtener el serial real
    # El inversor devuelve el serial en la respuesta aunque el device_id sea incorrecto
    url = f"http://{host}{LOGGER_PATH}/probe/{date_str}"
    try:
        r = requests.get(url, auth=(cfg.username, cfg.password), timeout=10)
        data = r.json()
        if "serial" in data:
            return data["serial"]
    except Exception:
        pass

    # Fallback: usar el device_id configurado o buscar en la respuesta real
    # Hacemos una llamada con el serial de la URL de la web
    # La URL de la web es: /#/embeddedinverter/config/local/1/-1
    # y el logger es: /inverter/log/{serial}/{fecha}
    # Intentamos obtenerlo de la página principal
    try:
        r = requests.get(
            f"http://{host}/inverter/log/{date_str}",
            auth=(cfg.username, cfg.password),
            timeout=10,
        )
        data = r.json()
        if "serial" in data:
            return data["serial"]
    except Exception:
        pass

    raise LoggerReaderError(
        "No se pudo obtener el device_id del inversor. "
        "Añade INVERTER_DEVICE_ID al .env"
    )


def _calculate_stats(records: list[dict], target_date: date, device_id: str) -> DailyStats:
    """Calcula los acumulados diarios a partir de los registros minuto a minuto."""
    INTERVAL_H = 1 / 60  # cada registro = 1 minuto = 1/60 hora

    # Producción solar (Pdc1 + Pdc2 en W → kWh)
    solar_kwh = sum(
        (r.get("Pdc1", 0) + r.get("Pdc2", 0)) * INTERVAL_H
        for r in records
    ) / 1000

    # Energía consumida de red: PacMeter positivo = importando de red
    grid_consumed_kwh = sum(
        max(0, r.get("PacMeter", 0)) * INTERVAL_H
        for r in records
    ) / 1000

    # Energía exportada a red: diferencia del contador EPvToGrid (en Wh)
    epv_start = records[0].get("EPvToGrid", 0)
    epv_end   = records[-1].get("EPvToGrid", 0)
    grid_exported_kwh = max(0, epv_end - epv_start) / 1000

    # Consumo total estimado (PacGrid = potencia hacia cargas + red)
    consumption_kwh = sum(
        r.get("PacGrid", 0) * INTERVAL_H
        for r in records
    ) / 1000

    # SOC inicio y fin
    soc_start = records[0].get("Sbatt", 0)
    soc_end   = records[-1].get("Sbatt", 0)

    return DailyStats(
        date=target_date,
        device_id=device_id,
        solar_kwh=round(solar_kwh, 3),
        grid_consumed_kwh=round(grid_consumed_kwh, 3),
        grid_exported_kwh=round(grid_exported_kwh, 3),
        consumption_kwh=round(consumption_kwh, 3),
        soc_start_pct=soc_start,
        soc_end_pct=soc_end,
        records=len(records),
    )
