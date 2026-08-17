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
from statistics import median

import requests

from app.config import InverterConfig

logger = logging.getLogger(__name__)

LOGGER_PATH = "/inverter/log"


def house_power_w(record: dict) -> float:
    """Potencia instantánea de la vivienda (W) a partir de un registro del datalogger.

    ⚠️ NO es `PacGrid`. `PacGrid` ≈ `Pac` = potencia de SALIDA AC del inversor, que
    incluye lo que se está exportando a la red. El consumo real de la casa es lo que
    sale del inversor MÁS lo que entra de la red:

        casa = PacGrid + PacMeter        (PacMeter: + importando, − exportando)

    Verificado contra el balance energético del datalogger (2026-08-17):
      · mediodía exportando → PacGrid 1468 W, PacMeter −1034 W → casa 434 W
      · noche con batería a min_soc → PacGrid −23 W, PacMeter +538 W → casa 515 W
    Con `PacGrid` a secas, ese segundo caso da un consumo NEGATIVO.

    El suelo en 0 cubre desincronizaciones puntuales entre las dos medidas.
    """
    return max(0.0, record.get("PacGrid", 0) + record.get("PacMeter", 0))


_NIGHT_MINUTES = 480  # 00:00–07:59 (8 h × 60 min)


@dataclass
class DailyStats:
    """Acumulados diarios calculados a partir del datalogger."""
    date: date
    device_id: str
    solar_kwh: float
    grid_consumed_kwh: float
    grid_exported_kwh: float
    consumption_kwh: float
    night_consumption_kwh: float  # consumo 00:00–07:59 (PacGrid)
    soc_start_pct: float
    soc_end_pct: float
    records: int                      # número de registros del día (max 1440)
    half_hour_solar_kwh: list[float]  # 48 slots × 30 min, kWh por slot


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


def _fetch_records(cfg: InverterConfig, target_date: date) -> tuple[list[dict], str]:
    """Descarga los registros minuto a minuto de un día. Devuelve (registros, device_id)."""
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

    return records, device_id


def get_recent_house_power(cfg: InverterConfig, minutes: int = 60) -> float | None:
    """Potencia típica (W) que la vivienda ha consumido en los últimos `minutes`.

    Lee el datalogger de HOY (permitido: es una lectura en tiempo real, no una stat
    diaria) y devuelve la MEDIANA de `house_power_w` sobre los últimos registros.

    Mediana y no media a propósito: un horno o una lavadora de 20 min desplazarían
    la media y, extrapolada a las horas de sol restantes, dejarían el excedente
    previsto casi a cero. La mediana de 60 muestras ignora esos picos cortos.

    Devuelve None si el datalogger no responde o no hay registros — el llamante debe
    tener un fallback.
    """
    try:
        records, _ = _fetch_records(cfg, date.today())
    except LoggerReaderError as e:
        logger.warning(f"No se pudo leer el consumo reciente de la vivienda: {e}")
        return None

    recent = records[-minutes:]
    if not recent:
        return None
    return round(median(house_power_w(r) for r in recent), 1)


def get_daily_stats(cfg: InverterConfig, target_date: date) -> DailyStats:
    """Obtiene los acumulados de un día concreto."""
    date_str = target_date.isoformat()
    records, device_id = _fetch_records(cfg, target_date)

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
    SLOT_MINUTES = 30

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

    # Consumo nocturno 00:00–07:59: primeros _NIGHT_MINUTES registros
    # PacGrid a esa hora ≈ potencia a cargas (sin producción solar)
    night_recs = records[:_NIGHT_MINUTES]
    if night_recs:
        raw_night = sum(r.get("PacGrid", 0) * INTERVAL_H for r in night_recs) / 1000
        # Prorratear si el día tiene menos registros de los esperados
        if len(night_recs) < _NIGHT_MINUTES:
            raw_night *= _NIGHT_MINUTES / len(night_recs)
        night_consumption_kwh = max(0.0, raw_night)
    else:
        night_consumption_kwh = 0.0

    # SOC inicio y fin
    soc_start = records[0].get("Sbatt", 0)
    soc_end   = records[-1].get("Sbatt", 0)

    # Producción solar por slot de 30 min (48 slots, "local labeled UTC")
    half_hour: list[float] = []
    for slot in range(48):
        slot_recs = records[slot * SLOT_MINUTES : (slot + 1) * SLOT_MINUTES]
        kwh = sum((r.get("Pdc1", 0) + r.get("Pdc2", 0)) * INTERVAL_H for r in slot_recs) / 1000
        half_hour.append(round(kwh, 4))

    return DailyStats(
        date=target_date,
        device_id=device_id,
        solar_kwh=round(solar_kwh, 3),
        grid_consumed_kwh=round(grid_consumed_kwh, 3),
        grid_exported_kwh=round(grid_exported_kwh, 3),
        consumption_kwh=round(consumption_kwh, 3),
        night_consumption_kwh=round(night_consumption_kwh, 3),
        soc_start_pct=soc_start,
        soc_end_pct=soc_end,
        records=len(records),
        half_hour_solar_kwh=half_hour,
    )
