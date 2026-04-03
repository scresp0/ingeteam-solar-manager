"""
config.py — carga y valida la configuración desde config.yaml y variables de entorno.

Jerarquía de valores (mayor prioridad gana):
  1. Variables de entorno (ej. SOLCAST_API_KEY)
  2. config.yaml
  3. Valores por defecto definidos en los modelos Pydantic
"""

import os
from datetime import date
from pathlib import Path
from typing import List

import yaml
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Modelos de configuración
# ---------------------------------------------------------------------------

class SolcastConfig(BaseModel):
    api_key: str
    resource_id: str
    base_url: str = "https://api.solcast.com.au"
    forecast_hours: int = 48


class InverterConfig(BaseModel):
    web_url: str
    username: str
    password: str
    modbus_host: str = ""
    modbus_port: int = 502
    modbus_slave: int = 1
    browser_timeout_seconds: int = 30

    def get_modbus_host(self) -> str:
        """Devuelve el host MODBUS, usando web_url como fallback."""
        if self.modbus_host:
            return self.modbus_host
        url = self.web_url.strip()
        for prefix in ("https://", "http://"):
            if url.startswith(prefix):
                url = url[len(prefix):]
        return url.rstrip("/").split("/")[0]


class InstallationConfig(BaseModel):
    battery_capacity_kwh: float
    average_daily_consumption_kwh: float
    peak_power_kwp: float = 0.0


class TariffInterval(BaseModel):
    start: str   # "HH:MM"
    end: str     # "HH:MM"


class TariffPeriods(BaseModel):
    valley: dict   # {"intervals": [...]}
    flat: dict
    peak: dict

    def get_intervals(self, period: str) -> List[TariffInterval]:
        data = getattr(self, period)
        return [TariffInterval(**i) for i in data.get("intervals", [])]


class TariffConfig(BaseModel):
    schedule_at: str = "23:30"
    weekend_days: List[int] = [5, 6]   # 0=lunes..6=domingo
    holidays: List[str] = []           # ["YYYY-MM-DD", ...]
    periods: TariffPeriods

    def is_valley_day(self, d: date) -> bool:
        """True si el día es fin de semana o festivo (todo el día es valle)."""
        if d.weekday() in self.weekend_days:
            return True
        return d.isoformat() in self.holidays

    def get_valley_intervals(self, d: date) -> List[TariffInterval]:
        """Devuelve los intervalos valle para un día concreto."""
        if self.is_valley_day(d):
            return [TariffInterval(start="00:00", end="24:00")]
        return self.periods.get_intervals("valley")


class ChargingConfig(BaseModel):
    risk_factor: float = Field(default=0.7, ge=0.0, le=1.0)
    min_soc_pct: float = Field(default=15.0, ge=0.0, le=100.0)
    max_soc_pct: float = Field(default=95.0, ge=0.0, le=100.0)
    safety_margin_kwh: float = Field(default=1.0, ge=0.0)
    night_consumption_kwh: float = Field(default=3.5, ge=0.0)

    @field_validator("max_soc_pct")
    @classmethod
    def max_must_exceed_min(cls, v, info):
        if "min_soc_pct" in info.data and v <= info.data["min_soc_pct"]:
            raise ValueError("max_soc_pct debe ser mayor que min_soc_pct")
        return v


class SystemConfig(BaseModel):
    log_level: str = "INFO"
    log_file: str = "/app/logs/solar-manager.log"
    dry_run: bool = False
    timezone: str = "Europe/Madrid"

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, v):
        valid = {"DEBUG", "INFO", "WARNING", "ERROR"}
        if v.upper() not in valid:
            raise ValueError(f"log_level debe ser uno de {valid}")
        return v.upper()


class AppConfig(BaseModel):
    solcast: SolcastConfig
    inverter: InverterConfig
    installation: InstallationConfig
    tariff: TariffConfig
    charging: ChargingConfig
    system: SystemConfig


# ---------------------------------------------------------------------------
# Carga de configuración
# ---------------------------------------------------------------------------

def load_config(config_path: str | Path | None = None) -> AppConfig:
    path = _resolve_config_path(config_path)
    data = _load_yaml(path)
    data = _apply_env_overrides(data)
    try:
        return AppConfig(**data)
    except Exception as e:
        raise ValueError(f"Error de configuración: {e}") from e


def _resolve_config_path(config_path: str | Path | None) -> Path:
    if config_path:
        return Path(config_path)
    if env_path := os.environ.get("CONFIG_PATH"):
        return Path(env_path)
    for candidate in [Path("/app/config.yaml"), Path("config.yaml")]:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "No se encontró config.yaml. "
        "Copia config.example.yaml como config.yaml y rellena tus valores."
    )


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"El fichero {path} no contiene un YAML válido")
    return data


def _apply_env_overrides(data: dict) -> dict:
    overrides = {
        ("solcast", "api_key"):                              "SOLCAST_API_KEY",
        ("solcast", "resource_id"):                          "SOLCAST_RESOURCE_ID",
        ("inverter", "web_url"):                             "INVERTER_WEB_URL",
        ("inverter", "username"):                            "INVERTER_USERNAME",
        ("inverter", "password"):                            "INVERTER_PASSWORD",
        ("inverter", "modbus_host"):                         "INVERTER_MODBUS_HOST",
        ("installation", "battery_capacity_kwh"):            "BATTERY_CAPACITY_KWH",
        ("installation", "average_daily_consumption_kwh"):   "DAILY_CONSUMPTION_KWH",
        ("charging", "risk_factor"):                         "RISK_FACTOR",
        ("system", "dry_run"):                               "DRY_RUN",
        ("system", "log_level"):                             "LOG_LEVEL",
    }
    for (section, key), env_var in overrides.items():
        value = os.environ.get(env_var)
        if value is not None:
            if section not in data:
                data[section] = {}
            if key in {"dry_run"}:
                data[section][key] = value.lower() in ("true", "1", "yes")
            elif key in {"battery_capacity_kwh", "average_daily_consumption_kwh", "risk_factor"}:
                data[section][key] = float(value)
            else:
                data[section][key] = value
    return data
