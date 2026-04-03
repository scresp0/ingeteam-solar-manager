"""
solcast.py — cliente para la API de Solcast.

Obtiene la previsión de producción solar para el día siguiente
y devuelve los valores p10, p50 y p90 agregados en kWh.

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


def get_tomorrow_forecast(cfg: SolcastConfig, timezone: str = "Europe/Madrid") -> SolarForecast:
    """
    Obtiene la previsión de producción solar para mañana.

    Solicita las próximas `forecast_hours` horas, filtra los intervalos
    que caen en el día de mañana (en la zona horaria configurada) y
    suma la energía de cada percentil.

    Args:
        cfg:      configuración de Solcast (api_key, resource_id, etc.)
        timezone: zona horaria para determinar qué es "mañana"

    Returns:
        SolarForecast con p10, p50, p90 en kWh para el día siguiente

    Raises:
        SolcastError: si la API falla o la respuesta no tiene datos para mañana
    """
    raw = _fetch_forecasts(cfg)
    forecasts = raw.get("forecasts", [])
    if not forecasts:
        raise SolcastError("La API de Solcast devolvió una lista de previsiones vacía")

    tomorrow = _get_tomorrow(timezone)
    logger.debug(f"Filtrando previsiones para: {tomorrow} (tz={timezone})")

    intervals = _filter_by_date(forecasts, tomorrow, timezone)
    if not intervals:
        raise SolcastError(
            f"No hay previsiones para mañana ({tomorrow}) en la respuesta de Solcast. "
            f"Comprueba que forecast_hours ({cfg.forecast_hours}) es suficiente."
        )

    forecast = _aggregate_kwh(intervals)
    logger.info(
        f"Previsión Solcast para {tomorrow}: "
        f"p10={forecast.p10} kWh, p50={forecast.p50} kWh, p90={forecast.p90} kWh "
        f"({len(intervals)} intervalos)"
    )
    return forecast


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
    """
    Filtra los intervalos de previsión que corresponden a target_date.

    Solcast devuelve period_end en UTC con formato ISO 8601.
    Convertimos a la zona horaria local antes de filtrar.
    """
    tz = ZoneInfo(timezone)
    result = []
    for item in forecasts:
        try:
            period_end_str = item["period_end"]
            # Solcast usa formato: "2024-01-15T14:00:00.0000000Z"
            period_end_str = period_end_str.rstrip("Z").split(".")[0] + "+00:00"
            period_end_utc = datetime.fromisoformat(period_end_str)
            period_end_local = period_end_utc.astimezone(tz)
            if period_end_local.date() == target_date:
                result.append(item)
        except (KeyError, ValueError) as e:
            logger.warning(f"Intervalo de Solcast con formato inesperado, ignorado: {e}")
    return result


def _aggregate_kwh(intervals: list[dict]) -> SolarForecast:
    """
    Suma la energía de todos los intervalos para obtener el total del día.

    Solcast devuelve pv_estimate (p50), pv_estimate10 (p10) y
    pv_estimate90 (p90) en kW como potencia media del intervalo.
    Cada intervalo es de 30 minutos → multiplicamos por 0.5 para obtener kWh.
    """
    INTERVAL_HOURS = 0.5  # intervalos de 30 minutos

    p10_kwh = sum(i.get("pv_estimate10", 0.0) * INTERVAL_HOURS for i in intervals)
    p50_kwh = sum(i.get("pv_estimate",   0.0) * INTERVAL_HOURS for i in intervals)
    p90_kwh = sum(i.get("pv_estimate90", 0.0) * INTERVAL_HOURS for i in intervals)

    return SolarForecast(
        p10=round(p10_kwh, 2),
        p50=round(p50_kwh, 2),
        p90=round(p90_kwh, 2),
    )
