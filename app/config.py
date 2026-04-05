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
    device_id: str = ""   # serial del inversor para el logger HTTP (INVERTER_DEVICE_ID en .env)

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


class EmailConfig(BaseModel):
    enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_to: str = ""
    use_tls: bool = True             # STARTTLS en puerto 587
    use_ssl: bool = False            # SSL directo en puerto 465
    verify_ssl: bool = True          # False para servidores con cert autofirmado


class InfluxDBConfig(BaseModel):
    enabled: bool = False
    url: str = "http://localhost:8086"
    org: str = "solar"
    bucket: str = "solar-manager"
    token: str = ""              # INFLUXDB_TOKEN en .env


class SystemConfig(BaseModel):
    log_level: str = "INFO"
    log_file: str = "/app/logs/solar-manager.log"
    dry_run: bool = False
    timezone: str = "Europe/Madrid"
    email: EmailConfig = EmailConfig()
    web_port: int = 8080          # puerto de la interfaz web
    web_enabled: bool = True      # false para deshabilitar la interfaz web

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
    influxdb: InfluxDBConfig = InfluxDBConfig()

    @property
    def email(self) -> EmailConfig:
        return self.system.email


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
        ("inverter", "device_id"):                           "INVERTER_DEVICE_ID",
        ("installation", "battery_capacity_kwh"):            "BATTERY_CAPACITY_KWH",
        ("installation", "average_daily_consumption_kwh"):   "DAILY_CONSUMPTION_KWH",
        ("charging", "risk_factor"):                         "RISK_FACTOR",
        ("system", "dry_run"):                               "DRY_RUN",
        ("system", "log_level"):                             "LOG_LEVEL",
        ("system", "email", "smtp_port"):                   "SMTP_PORT",
        ("charging", "risk_factor"):                         "RISK_FACTOR",
        ("charging", "min_soc_pct"):                         "MIN_SOC_PCT",
        ("charging", "max_soc_pct"):                         "MAX_SOC_PCT",
        ("charging", "safety_margin_kwh"):                   "SAFETY_MARGIN_KWH",
        ("charging", "night_consumption_kwh"):               "NIGHT_CONSUMPTION_KWH",
        ("influxdb", "token"):                               "INFLUXDB_TOKEN",
        ("influxdb", "org"):                                 "INFLUXDB_ORG",
        ("influxdb", "bucket"):                              "INFLUXDB_BUCKET",
        ("influxdb", "url"):                                 "INFLUXDB_URL",
    }
    # Overrides para email (anidados bajo system.email)
    email_overrides = {
        "smtp_host":     "SMTP_HOST",
        "smtp_user":     "SMTP_USER",
        "smtp_password": "SMTP_PASSWORD",
        "mail_from":     "MAIL_FROM",
        "mail_to":       "MAIL_TO",
    }
    for key, env_var in email_overrides.items():
        value = os.environ.get(env_var)
        if value is not None:
            if "system" not in data:
                data["system"] = {}
            if "email" not in data["system"]:
                data["system"]["email"] = {}
            data["system"]["email"][key] = value
    for (section, key), env_var in overrides.items():
        value = os.environ.get(env_var)
        if value is not None:
            if section not in data:
                data[section] = {}
            if key in {"dry_run"}:
                data[section][key] = value.lower() in ("true", "1", "yes")
            elif key in {
                "battery_capacity_kwh", "average_daily_consumption_kwh",
                "risk_factor", "min_soc_pct", "max_soc_pct",
                "safety_margin_kwh", "night_consumption_kwh",
            }:
                data[section][key] = float(value)
            elif key in {"smtp_port", "web_port"}:
                data[section][key] = int(value)
            else:
                data[section][key] = value
    return data
