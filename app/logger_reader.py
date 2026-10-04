"""
logger_reader.py — lee el datalogger del inversor Ingeteam via HTTP.

Endpoints (Basic Auth con las credenciales del inversor):
  GET http://{host}/inverter/log/{device_id}/{YYYY-MM-DD}
      Datalogger de la tarjeta de comunicaciones. Dejó de grabar el 2026-10-03 a las
      21:40, al actualizarse el firmware del DSP de ABH1006AB a ABH1006AC.
  GET http://{host}/inverter/sdodatalogger/read/{modbus_slave}/{YYYYMMDD}/0/-1
      Datalogger "SDO", el que graba desde entonces (y el que muestra la web del
      inversor en Logger). Mismo minuto a minuto, otras claves — ver `_SDO_FIELDS`.

`_fetch_records` consulta los dos y los une por hora, así que un día partido entre
ambos (el propio 2026-10-03) sale completo y los días antiguos siguen leyéndose.

Calcula acumulados diarios a partir de los datos minuto a minuto:
  - solar_kwh        : producción solar total (Pdc1 + Pdc2)
  - grid_consumed_kwh: energía consumida de red (PacMeter > 0)
  - grid_exported_kwh: energía exportada a red (EPvToGrid delta; sin ese contador,
                       que el datalogger SDO no trae, ∫PacMeter < 0)
  - soc_start_pct    : SOC al inicio del día (00:00)
  - soc_end_pct      : SOC al final del día (23:59)
  - peak_soc_pct     : SOC máximo alcanzado en el día (pico tras la carga)
  - battery_charged_kwh : energía neta cargada en la batería en el día (∫Pbatt < 0)
  - consumption_kwh  : consumo de la vivienda (∫ `house_power_w`)
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

import requests

from app.config import InverterConfig

logger = logging.getLogger(__name__)

LOGGER_PATH = "/inverter/log"
SDO_LOGGER_PATH = "/inverter/sdodatalogger/read"

# Datalogger SDO: las claves son los ids de texto del mapa del inversor
# (`GET /inverter/map/1` → `sdodata` + `langs`), no nombres. Se traducen a las claves
# del datalogger antiguo para que el resto del módulo no distinga el origen.
# No trae `Pac` ni `EPvToGrid`; a cambio trae el consumo de la vivienda ya calculado.
_SDO_FIELDS = {
    "L-165": "Pdc1",      # FV 1. Potencia
    "L-168": "Pdc2",      # FV 2. Potencia
    "L-133": "Pbatt",     # Batería. Potencia
    "L-135": "Sbatt",     # Batería. SOC
    "L-183": "PacGrid",   # Vatímetro Interno Red. Potencia Activa
    "L-660": "PacMeter",  # Vatímetro Externo Red. Potencia Activa
    "L-709": "Pload",     # Cargas Totales. Potencia Activa
}


def house_power_w(record: dict) -> float:
    """Potencia instantánea de la vivienda (W) a partir de un registro del datalogger.

    ⚠️ NO es `PacGrid`. `PacGrid` y `PacMeter` son dos medidas redundantes del MISMO
    flujo de red (una interna del inversor, otra del contador), con signos opuestos:
    `PacGrid + PacMeter ≈ 0` casi siempre, sea cual sea el consumo real de la casa —
    la fórmula `PacGrid + PacMeter` (usada hasta v1.79) daba ≈0 W de casa en cualquier
    momento sin flujo de red significativo (de noche con la batería cubriendo la casa,
    o justo ahora con "Red: 0W"). `Pac` sí es la salida AC real del inversor hacia la
    vivienda/red:

        casa = Pac + PacMeter        (PacMeter: + importando, − exportando)

    Corregido en v1.80. Verificado contra el datalogger real (2026-08-22), comparando
    tres regímenes del mismo día/día anterior:
      · ahora mismo, sin flujo de red: Pac 332.9 W, PacMeter 1.2 W → casa 334 W
        (≈ 337 W del monitor del inversor; con PacGrid+PacMeter salía **0 W**)
      · mediodía exportando (21-ago 13:01): Pac 4011.7 W, PacMeter −3744.9 W → casa 267 W
      · madrugada, batería cubriendo la casa (21-ago 00:00): Pac 347.4 W, PacMeter −0.5 W
        → casa 347 W (≈ los 366.8 W que entregaba la batería esa hora)
    El ejemplo con `PacGrid` de la versión anterior (verificado el 2026-08-17) no se
    ha podido reproducir: en los tres regímenes de arriba `PacGrid` no se parece a
    `Pac`, se parece a `−PacMeter`.

    El suelo en 0 cubre desincronizaciones puntuales entre las dos medidas.

    Los registros del datalogger SDO (desde el 2026-10-03) no traen `Pac` sino
    `Pload`: "Cargas Totales. Potencia Activa", el consumo que calcula el propio
    inversor (input register 30079) y el que enseña su web. Comparado en vivo por
    MODBUS el 2026-10-04 contra `Pac + PacMeter`: 283/287, 275/265, 277/281,
    277/276 W — la misma magnitud sin el ruido de sumar dos medidas.
    ⚠️ Comprobado solo SIN flujo de red (|PacMeter| < 10 W); falta verlo exportando.
    """
    if "Pload" in record:
        return max(0.0, record["Pload"])
    return max(0.0, record.get("Pac", 0) + record.get("PacMeter", 0))


_NIGHT_MINUTES = 480  # 00:00–07:59 (8 h × 60 min)

# Caché de `get_recent_house_power`: (instante monotónico, vatios). El controlador
# de corriente corre en un hilo de APScheduler, de ahí el lock.
_house_power_cache: tuple[float, float] | None = None
_house_power_lock = threading.Lock()


@dataclass
class DailyStats:
    """Acumulados diarios calculados a partir del datalogger."""
    date: date
    device_id: str
    solar_kwh: float
    grid_consumed_kwh: float
    grid_exported_kwh: float
    consumption_kwh: float
    night_consumption_kwh: float  # consumo 00:00–07:59 de la vivienda
    soc_start_pct: float
    soc_end_pct: float
    peak_soc_pct: float          # SOC máximo del día — cuánto se acercó al objetivo de carga
    battery_charged_kwh: float   # kWh netos cargados en la batería (valle + solar, ∫Pbatt < 0)
    records: int                            # número de registros del día (max 1440)
    half_hour_solar_kwh: list[float]        # 48 slots × 30 min, kWh por slot
    half_hour_house_kwh: list[float]        # consumo de la vivienda por slot
    half_hour_grid_import_kwh: list[float]  # energía tomada de red por slot
    half_hour_grid_export_kwh: list[float]  # energía vertida a red por slot


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


def _sdo_to_legacy(val: dict) -> dict:
    """Traduce un registro del datalogger SDO a las claves del datalogger antiguo.

    Una clave ausente lanza `LoggerReaderError` en vez de quedarse en 0: si un
    firmware renumera los ids de texto, un día entero de producción o consumo a
    cero es un dato falso pero plausible que acabaría en InfluxDB.
    """
    missing = [k for k in _SDO_FIELDS if k not in val]
    if missing:
        raise LoggerReaderError(
            f"El datalogger SDO no trae los campos {missing} — ¿ha cambiado el mapa "
            f"del inversor? Revisar `_SDO_FIELDS` contra GET /inverter/map/1"
        )
    return {name: val[key] for key, name in _SDO_FIELDS.items()}


def _get_logger_json(cfg: InverterConfig, url: str) -> dict:
    logger.debug(f"Leyendo logger: {url}")
    try:
        response = requests.get(url, auth=(cfg.username, cfg.password), timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        raise LoggerReaderError(f"Error HTTP al leer logger: {e}") from e
    except requests.exceptions.ConnectionError as e:
        raise LoggerReaderError(f"No se pudo conectar al inversor: {e}") from e
    except Exception as e:
        raise LoggerReaderError(f"Error inesperado leyendo logger: {e}") from e


def _merge_loggers(legacy: list[dict], sdo: list[dict]) -> list[dict]:
    """Une las entradas `{time, val}` de los dos dataloggers en una lista de registros.

    Del SDO solo se toman las posteriores a la última del antiguo: en el día del
    cambio el antiguo cubre hasta las 21:40 y el SDO desde las 21:45.
    """
    last = legacy[-1]["time"] if legacy else ""
    return [e["val"] for e in legacy] + [
        _sdo_to_legacy(e["val"]) for e in sdo if e["time"] > last
    ]


def _fetch_records(cfg: InverterConfig, target_date: date) -> tuple[list[dict], str]:
    """Descarga los registros minuto a minuto de un día. Devuelve (registros, device_id).

    Consulta el datalogger antiguo y el SDO (ver docstring del módulo). El que no
    tiene el día responde 200 con `{"code": "error"}` en unos bytes, así que pedir
    los dos no cuesta una segunda descarga.
    """
    host = cfg.get_modbus_host()
    date_str = target_date.isoformat()

    # Usar device_id configurado o autodescubrir
    if cfg.device_id:
        device_id = cfg.device_id
        logger.debug(f"Usando device_id configurado: {device_id}")
    else:
        device_id = _get_device_id(cfg, host, date_str)

    legacy = _get_logger_json(cfg, f"http://{host}{LOGGER_PATH}/{device_id}/{date_str}")
    sdo = _get_logger_json(
        cfg,
        f"http://{host}{SDO_LOGGER_PATH}/{cfg.modbus_slave}/{target_date:%Y%m%d}/0/-1",
    )

    records = _merge_loggers(
        legacy.get("data", []) if legacy.get("code") == "ok" else [],
        sdo.get("data", []) if sdo.get("code") == "ok" else [],
    )
    if not records:
        raise LoggerReaderError(
            f"No hay datos en el logger para {date_str} "
            f"(antiguo: {legacy.get('code')}, SDO: {sdo.get('code')})"
        )

    return records, device_id


def get_recent_house_power(
    cfg: InverterConfig, minutes: int = 60, cache_min: int = 0,
) -> float | None:
    """Potencia típica (W) que la vivienda ha consumido en los últimos `minutes`.

    Lee el datalogger de HOY (permitido: es una lectura en tiempo real, no una stat
    diaria) y devuelve la MEDIANA de `house_power_w` sobre los últimos registros.

    Mediana y no media a propósito: un horno o una lavadora de 20 min desplazarían
    la media y, extrapolada a las horas de sol restantes, dejarían el excedente
    previsto casi a cero. La mediana de 60 muestras ignora esos picos cortos.

    `cache_min` > 0 reutiliza la última lectura durante ese número de minutos. Cada
    llamada descarga el día COMPLETO del datalogger (~1 MB y creciendo conforme
    avanza el día), porque el endpoint no admite rangos; con el controlador de
    corriente a 3 min eso son ~480 descargas y ~450 MB/día contra un equipo
    empotrado. El caché lo corta sin renunciar a un lazo de control rápido: el SOC
    se sigue leyendo por MODBUS en cada tick, que es barato.

    Un fallo NO se cachea: si el datalogger no responde se devuelve None y el
    siguiente tick reintenta, en vez de arrastrar el fallo durante `cache_min`.

    Devuelve None si el datalogger no responde o no hay registros — el llamante debe
    tener un fallback.
    """
    global _house_power_cache

    if cache_min > 0:
        with _house_power_lock:
            cached = _house_power_cache
        if cached is not None:
            age_s = time.monotonic() - cached[0]
            if age_s < cache_min * 60:
                logger.debug(
                    f"Consumo de casa: {cached[1]} W de caché ({age_s / 60:.1f} min, "
                    f"válido {cache_min} min)"
                )
                return cached[1]

    try:
        records, _ = _fetch_records(cfg, date.today())
    except LoggerReaderError as e:
        logger.warning(f"No se pudo leer el consumo reciente de la vivienda: {e}")
        return None

    recent = records[-minutes:]
    if not recent:
        return None

    value = round(median(house_power_w(r) for r in recent), 1)
    with _house_power_lock:
        _house_power_cache = (time.monotonic(), value)
    return value


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
        f"SOC {stats.soc_start_pct}% → {stats.soc_end_pct}% (pico {stats.peak_soc_pct}%) | "
        f"Cargado en batería={stats.battery_charged_kwh:.2f} kWh"
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

    # Energía exportada a red: diferencia del contador EPvToGrid (en Wh). El
    # datalogger SDO no trae ese contador: sin él en los dos extremos del día se
    # integra PacMeter, igual que el perfil de media hora (`grid_export_kwh`).
    if "EPvToGrid" in records[0] and "EPvToGrid" in records[-1]:
        grid_exported_kwh = max(0, records[-1]["EPvToGrid"] - records[0]["EPvToGrid"]) / 1000
    else:
        grid_exported_kwh = sum(
            max(0, -r.get("PacMeter", 0)) * INTERVAL_H for r in records
        ) / 1000

    # Consumo total de la vivienda. Ver `house_power_w`: la fórmula PacGrid+PacMeter
    # usada hasta v1.79 daba ≈0 siempre que no había flujo de red significativo
    # (corregido en v1.80, ver docstring de `house_power_w`).
    consumption_kwh = sum(house_power_w(r) * INTERVAL_H for r in records) / 1000

    # Consumo nocturno 00:00–07:59: primeros _NIGHT_MINUTES registros.
    night_recs = records[:_NIGHT_MINUTES]
    if night_recs:
        raw_night = sum(house_power_w(r) * INTERVAL_H for r in night_recs) / 1000
        # Prorratear si el día tiene menos registros de los esperados
        if len(night_recs) < _NIGHT_MINUTES:
            raw_night *= _NIGHT_MINUTES / len(night_recs)
        night_consumption_kwh = max(0.0, raw_night)
    else:
        night_consumption_kwh = 0.0

    # SOC inicio y fin
    soc_start = records[0].get("Sbatt", 0)
    soc_end   = records[-1].get("Sbatt", 0)
    # SOC máximo del día: de noche el SOC solo baja (descarga), así que el pico
    # coincide con el momento justo tras la carga de valle+solar — sirve para medir
    # cuánto se acercó al objetivo (`charging.max_soc_pct`) sin necesidad de acotar
    # una ventana horaria. Pensado como base para calibrar `charge_current.margin`
    # más adelante (ver CLAUDE.md, backtest del 2026-08-18).
    peak_soc_pct = max((r.get("Sbatt", 0) for r in records), default=0.0)

    # Energía neta cargada en la batería: Pbatt negativo = cargando (ver gotcha
    # MODBUS en CLAUDE.md — mismo convenio en el datalogger). Incluye valle Y solar;
    # no se separan porque ambas cuentan para el mismo objetivo (`max_soc_pct`).
    battery_charged_kwh = sum(
        max(0, -r.get("Pbatt", 0)) * INTERVAL_H for r in records
    ) / 1000

    # Perfil por slot de 30 min (48 slots, "local labeled UTC"). El flujo de red se
    # integra de PacMeter (+ importando, − exportando), coherente con el balance
    # verificado; NO del contador EPvToGrid que usa `grid_exported_kwh` diario.
    half_hour: list[float] = []
    half_hour_house: list[float] = []
    half_hour_import: list[float] = []
    half_hour_export: list[float] = []
    for slot in range(48):
        slot_recs = records[slot * SLOT_MINUTES : (slot + 1) * SLOT_MINUTES]
        half_hour.append(round(sum(
            (r.get("Pdc1", 0) + r.get("Pdc2", 0)) * INTERVAL_H for r in slot_recs) / 1000, 4))
        half_hour_house.append(round(sum(
            house_power_w(r) * INTERVAL_H for r in slot_recs) / 1000, 4))
        half_hour_import.append(round(sum(
            max(0, r.get("PacMeter", 0)) * INTERVAL_H for r in slot_recs) / 1000, 4))
        half_hour_export.append(round(sum(
            max(0, -r.get("PacMeter", 0)) * INTERVAL_H for r in slot_recs) / 1000, 4))

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
        peak_soc_pct=peak_soc_pct,
        battery_charged_kwh=round(battery_charged_kwh, 3),
        records=len(records),
        half_hour_solar_kwh=half_hour,
        half_hour_house_kwh=half_hour_house,
        half_hour_grid_import_kwh=half_hour_import,
        half_hour_grid_export_kwh=half_hour_export,
    )
