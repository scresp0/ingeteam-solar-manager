"""
solcast.py — cliente para la API de Solcast.

Obtiene la previsión de producción solar para los próximos 2 días
y devuelve los valores p10, p50 y p90 agregados en kWh por día.

Endpoint usado:
  GET https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts?format=json
"""

import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests

from app.config import SolcastConfig
from app.decision import SolarForecast

logger = logging.getLogger(__name__)


class SolcastError(Exception):
    """Error al obtener o procesar la previsión de Solcast."""


def get_two_day_forecast(
    cfg: SolcastConfig, timezone: str = "Europe/Madrid"
) -> tuple[SolarForecast, SolarForecast]:
    """
    Obtiene la previsión solar para los próximos 2 días.

    Returns:
        (forecast_day1, forecast_day2) — día 1 = mañana, día 2 = pasado mañana
    """
    raw = _fetch_forecasts(cfg)
    forecasts = raw.get("forecasts", [])
    if not forecasts:
        raise SolcastError("La API de Solcast devolvió una lista de previsiones vacía")

    tz = ZoneInfo(timezone)
    today = datetime.now(tz).date()
    day1 = today + timedelta(days=1)
    day2 = today + timedelta(days=2)

    intervals_day1 = _filter_by_date(forecasts, day1, timezone)
    intervals_day2 = _filter_by_date(forecasts, day2, timezone)

    if not intervals_day1:
        raise SolcastError(
            f"No hay previsiones para mañana ({day1}) en la respuesta de Solcast."
        )

    forecast_day1 = _aggregate_kwh(intervals_day1)
    forecast_day2 = _aggregate_kwh(intervals_day2) if intervals_day2 else SolarForecast(0.0, 0.0, 0.0)

    logger.info(
        f"Previsión Solcast día 1 ({day1}): "
        f"p10={forecast_day1.p10} p50={forecast_day1.p50} p90={forecast_day1.p90} kWh "
        f"({len(intervals_day1)} intervalos)"
    )
    logger.info(
        f"Previsión Solcast día 2 ({day2}): "
        f"p10={forecast_day2.p10} p50={forecast_day2.p50} p90={forecast_day2.p90} kWh "
        f"({len(intervals_day2)} intervalos)"
    )
    return forecast_day1, forecast_day2


def get_tomorrow_forecast(cfg: SolcastConfig, timezone: str = "Europe/Madrid") -> SolarForecast:
    """Compatibilidad — devuelve solo el día 1."""
    day1, _ = get_two_day_forecast(cfg, timezone)
    return day1


def _fetch_forecasts(cfg: SolcastConfig) -> dict:
    """Llama a la API de Solcast y devuelve el JSON completo."""
    url = (
        f"{cfg.base_url}/rooftop_sites/{cfg.resource_id}/forecasts"
        f"?format=json&hours={cfg.forecast_hours}"
    )
    logger.debug(f"Llamando a Solcast: {url}")
    try:
        response = requests.get(
            url,
            params={"api_key": cfg.api_key},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 401:
            raise SolcastError("API key de Solcast inválida o sin permisos (401)") from e
        if status == 404:
            raise SolcastError(f"Resource ID no encontrado en Solcast (404): {cfg.resource_id}") from e
        if status == 429:
            raise SolcastError("Límite de llamadas a la API de Solcast alcanzado (429)") from e
        raise SolcastError(f"Error HTTP {status} al llamar a Solcast: {e}") from e
    except requests.exceptions.ConnectionError as e:
        raise SolcastError(f"No se pudo conectar con la API de Solcast: {e}") from e
    except requests.exceptions.Timeout:
        raise SolcastError("Timeout al llamar a la API de Solcast (>30s)") from e


def _get_tomorrow(timezone: str) -> date:
    """Devuelve la fecha de mañana en la zona horaria indicada."""
    tz = ZoneInfo(timezone)
    return (datetime.now(tz) + timedelta(days=1)).date()


def _filter_by_date(forecasts: list[dict], target_date: date, timezone: str) -> list[dict]:
    """Filtra los intervalos de previsión que corresponden a target_date."""
    tz = ZoneInfo(timezone)
    result = []
    for item in forecasts:
        try:
            period_end_str = item["period_end"]
            period_end_str = period_end_str.rstrip("Z").split(".")[0] + "+00:00"
            period_end_utc = datetime.fromisoformat(period_end_str)
            period_end_local = period_end_utc.astimezone(tz)
            if period_end_local.date() == target_date:
                result.append(item)
        except (KeyError, ValueError) as e:
            logger.warning(f"Intervalo de Solcast con formato inesperado, ignorado: {e}")
    return result


def _aggregate_kwh(intervals: list[dict]) -> SolarForecast:
    """Suma la energía de todos los intervalos (intervalos de 30 min → × 0.5h)."""
    INTERVAL_HOURS = 0.5
    p10 = sum(i.get("pv_estimate10", 0.0) * INTERVAL_HOURS for i in intervals)
    p50 = sum(i.get("pv_estimate",   0.0) * INTERVAL_HOURS for i in intervals)
    p90 = sum(i.get("pv_estimate90", 0.0) * INTERVAL_HOURS for i in intervals)
    return SolarForecast(p10=round(p10, 2), p50=round(p50, 2), p90=round(p90, 2))
