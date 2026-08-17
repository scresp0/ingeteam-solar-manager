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
from typing import List, Optional

import yaml
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Modelos de configuración
# ---------------------------------------------------------------------------

class SolcastConfig(BaseModel):
    api_key: str
    resource_id: str
    base_url: str = "https://api.solcast.com.au"
    forecast_hours: int = 48
    cache_ttl_hours: int = 4   # horas de validez de la caché local


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
    # Re-evaluaciones adicionales (HH:MM) que recalculan la decisión con el SOC
    # del momento y reconfiguran el inversor si difiere de lo ya programado.
    # No escriben ciclo_carga en InfluxDB ni leen stats del día anterior.
    # Acepta una hora ("03:00"), varias separadas por coma ("19:00, 03:00")
    # o una lista YAML. Vacío/null = desactivado. Se normaliza a lista ordenada.
    schedule_recheck_at: List[str] = []
    weekend_days: List[int] = [5, 6]   # 0=lunes..6=domingo
    holidays: List[str] = []           # ["YYYY-MM-DD", ...]
    periods: TariffPeriods
    # Hora límite (exclusive) antes de la cual se considera que
    # el programa se ejecuta en la "noche anterior".
    # Ej: ejecución a las 00:30 del viernes → actúa como si fuera
    # la noche del jueves. Debe ser <= hora de fin del valle (08:00).
    night_cutoff_hour: int = Field(default=8, ge=0, le=12)

    @field_validator("holidays", mode="before")
    @classmethod
    def coerce_holidays_to_str(cls, v):
        return [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in (v or [])]

    @field_validator("schedule_recheck_at", mode="before")
    @classmethod
    def coerce_recheck_times(cls, v):
        """Normaliza a lista de "HH:MM" ordenada, sin duplicados.

        Un formato inválido aborta el arranque en vez de desactivarse en
        silencio: una re-evaluación que no corre no deja ninguna traza.
        """
        if v is None or v == "":
            return []
        items = v if isinstance(v, (list, tuple)) else str(v).split(",")

        out = []
        for raw in items:
            s = str(raw).strip()
            if not s:
                continue
            try:
                h, m = s.split(":")
                hh, mm = int(h), int(m)
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    raise ValueError
            except ValueError:
                raise ValueError(
                    f"tariff.schedule_recheck_at: {s!r} no es una hora válida; "
                    f"se esperaba HH:MM (ej. '03:00' o '19:00, 03:00')"
                )
            norm = f"{hh:02d}:{mm:02d}"
            if norm not in out:
                out.append(norm)
        return sorted(out)

    def is_valley_day(self, d: date) -> bool:
        if d.weekday() in self.weekend_days:
            return True
        return d.isoformat() in self.holidays

    def get_valley_intervals(self, d: date) -> List[TariffInterval]:
        if self.is_valley_day(d):
            return [TariffInterval(start="00:00", end="24:00")]
        return self.periods.get_intervals("valley")


class ChargingConfig(BaseModel):
    risk_factor: float = Field(default=0.7, ge=0.0, le=1.0)
    min_soc_pct: float = Field(default=15.0, ge=0.0, le=100.0)
    max_soc_pct: float = Field(default=95.0, ge=0.0, le=100.0)
    safety_margin_kwh: float = Field(default=1.0, ge=0.0)
    # Fallback usado mientras no haya suficientes días históricos en InfluxDB
    night_consumption_kwh: float = Field(default=3.5, ge=0.0)
    # Consumo nocturno dinámico. window_days = periodo que se promedia;
    # min_days_in_window = días con dato que debe haber DENTRO de esa ventana
    # para fiarse (por eso window_days >= min_days_in_window). El alias acepta
    # el nombre antiguo `night_consumption_min_days` de configs previas.
    night_consumption_min_days_in_window: int = Field(
        default=14, ge=1,
        validation_alias=AliasChoices(
            "night_consumption_min_days_in_window", "night_consumption_min_days"),
    )
    night_consumption_window_days: int = Field(default=30, ge=7)
    # Risk factor dinámico: calibrado desde desviación histórica Solcast vs real
    risk_factor_min_days_in_window: int = Field(
        default=14, ge=1,
        validation_alias=AliasChoices(
            "risk_factor_min_days_in_window", "risk_factor_min_days"),
    )
    risk_factor_window_days: int = Field(default=30, ge=7)
    # Factor de calibración del forecast Solcast: solar_real / forecast_p50 medio.
    # Compensa el sesgo sistemático del forecast para esta instalación.
    # 1.0 = sin corrección. Se calcula dinámicamente desde InfluxDB cuando hay
    # suficientes días; el valor de aquí es fallback.
    solar_bias_factor: float = Field(default=1.0, ge=0.5, le=1.5)
    solar_bias_min_days_in_window: int = Field(
        default=14, ge=1,
        validation_alias=AliasChoices(
            "solar_bias_min_days_in_window", "solar_bias_min_days"),
    )
    solar_bias_window_days: int = Field(default=30, ge=7)
    # Consumo diario dinámico: media de stats_diarias.consumption_kwh. Calibra
    # installation.average_daily_consumption_kwh, que es el fallback. La ventana
    # deslizante hace que el valor siga la estación (verano con AC vs invierno).
    daily_consumption_min_days_in_window: int = Field(default=14, ge=1)
    daily_consumption_window_days: int = Field(default=30, ge=7)

    @field_validator("max_soc_pct")
    @classmethod
    def max_must_exceed_min(cls, v, info):
        if "min_soc_pct" in info.data and v <= info.data["min_soc_pct"]:
            raise ValueError("max_soc_pct debe ser mayor que min_soc_pct")
        return v

    @model_validator(mode="after")
    def windows_must_cover_min_days(self):
        """Cada ventana deslizante debe ser >= su mínimo de días.

        Si `window_days < min_days`, la consulta a InfluxDB solo mira una ventana
        más corta que el umbral, así que el contador de días válidos nunca puede
        alcanzar el mínimo → el parámetro dinámico jamás se activa y queda clavado
        en el fallback de config de forma silenciosa.
        """
        for win, mn, name in (
            (self.night_consumption_window_days, self.night_consumption_min_days_in_window, "night_consumption"),
            (self.risk_factor_window_days,       self.risk_factor_min_days_in_window,       "risk_factor"),
            (self.solar_bias_window_days,        self.solar_bias_min_days_in_window,        "solar_bias"),
            (self.daily_consumption_window_days, self.daily_consumption_min_days_in_window, "daily_consumption"),
        ):
            if win < mn:
                raise ValueError(
                    f"{name}_window_days ({win}) debe ser >= {name}_min_days_in_window ({mn}): "
                    f"con una ventana menor que el mínimo el valor dinámico nunca se "
                    f"activaría (imposible encontrar {mn} días con dato en una ventana de {win})."
                )
        return self


class ChargeCurrentConfig(BaseModel):
    """Control dinámico de la corriente máxima de carga de batería (holding 40087).

    Baja los amperios al mínimo necesario para reducir el calor del inversor y las
    baterías, garantizando que la batería llegue al tope a tiempo: en el valle
    nocturno (carga de red) o dentro de la ventana solar productiva del día. El
    calor es ∝ corriente para una misma energía, así que cargar despacio (cuando
    sobra tiempo) minimiza el calentamiento. Si la batería está fría (invierno)
    se carga a tope para captar los picos solares intermitentes.
    """
    enabled: bool = True
    interval_min: int = Field(default=15, ge=1)                      # frecuencia del controlador
    temp_gate_enabled: bool = True                                   # SOLAR: si True solo carga suave con temp > umbral; si False carga suave siempre que la solar baste
    hot_threshold_c: float = Field(default=30.0)                     # temp batería > umbral → carga suave; si no → max_a (solo si temp_gate_enabled)
    house_power_window_min: int = Field(default=60, ge=5, le=240)    # minutos de datalogger para estimar el consumo de casa (mediana)
    productive_window_pct: int = Field(default=90, ge=10, le=100)    # % del total diario real (fallback de fin de ventana)
    productive_window_end_hour: float = Field(default=17.0, ge=0.0, le=24.0)  # fallback si no hay forecast ni histórico
    floor_a: int = Field(default=15, ge=1, le=66)                    # corriente mínima (para que la carga termine)
    max_a: int = Field(default=66, ge=1, le=66)                      # tope máximo (66 = máximo del inversor)
    margin: float = Field(default=1.2, ge=1.0, le=3.0)              # margen sobre el mínimo teórico
    battery_balance: bool = False                                    # cerca del tope, carga muy suave para balancear celdas del stack
    balance_soc_pct: float = Field(default=98.0, ge=0.0, le=100.0)   # SOC a partir del cual entra el modo BALANCE
    balance_soc_pct_2: float = Field(default=99.0, ge=0.0, le=100.0) # 2ª etapa: SOC a partir del cual la corriente de balanceo baja a la mitad
    balance_floor_a: int = Field(default=10, ge=1, le=66)            # corriente mínima durante el balanceo (< floor_a normal)

    @model_validator(mode="after")
    def floor_not_above_max(self):
        if self.floor_a > self.max_a:
            raise ValueError(
                f"charge_current.floor_a ({self.floor_a}) no puede superar max_a ({self.max_a})"
            )
        if self.balance_floor_a > self.max_a:
            raise ValueError(
                f"charge_current.balance_floor_a ({self.balance_floor_a}) no puede superar max_a ({self.max_a})"
            )
        if self.balance_soc_pct_2 < self.balance_soc_pct:
            raise ValueError(
                f"charge_current.balance_soc_pct_2 ({self.balance_soc_pct_2}) no puede ser menor "
                f"que balance_soc_pct ({self.balance_soc_pct})"
            )
        return self


class BackupConfig(BaseModel):
    """Copia de seguridad periódica a un servidor externo por SCP.

    Empaqueta en un único .tar.gz el backup online de InfluxDB, el directorio de
    logs y config.yaml, y lo sube por SCP (clave SSH) al `remote_dir` del host
    remoto. Tras subir, rota los backups antiguos manteniendo `retention`.
    No contiene secretos: la autenticación es por clave SSH (fichero montado).
    """
    enabled: bool = False
    schedule_at: str = "04:00"                 # HH:MM diario
    host: str = ""                             # servidor destino
    port: int = Field(default=22, ge=1, le=65535)
    user: str = ""                             # usuario SSH en el destino
    remote_dir: str = ""                       # directorio destino en el remoto
    ssh_key_path: str = "/root/.ssh/id_backup" # clave privada montada en el contenedor
    # Verificación de la clave del host remoto. True (recomendado) exige que el
    # host esté en known_hosts; False la desactiva (inseguro, útil solo para un
    # primer arranque hasta poblar known_hosts).
    strict_host_key_checking: bool = True
    known_hosts_path: str = "/root/.ssh/known_hosts"
    retention: int = Field(default=7, ge=1)    # nº de backups a mantener en el remoto
    timeout_seconds: int = Field(default=600, ge=30)  # timeout de scp/ssh

    @model_validator(mode="after")
    def required_when_enabled(self):
        if self.enabled:
            missing = [
                name for name, val in (
                    ("host", self.host),
                    ("user", self.user),
                    ("remote_dir", self.remote_dir),
                ) if not str(val).strip()
            ]
            if missing:
                raise ValueError(
                    "backup habilitado pero faltan campos obligatorios: "
                    + ", ".join(missing)
                )
        return self


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
    web_api_key: str = ""         # clave para endpoints de escritura (vacío = sin auth)

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
    charge_current: ChargeCurrentConfig = ChargeCurrentConfig()
    system: SystemConfig
    influxdb: InfluxDBConfig = InfluxDBConfig()
    backup: BackupConfig = BackupConfig()

    @model_validator(mode="after")
    def balance_soc_reachable(self):
        # El balanceo solo actúa entre balance_soc_pct y max_soc_pct; si el umbral
        # iguala o supera el tope de carga nunca se alcanza y el modo BALANCE queda
        # muerto silenciosamente. Para balancear de verdad conviene max_soc = 100.
        if (self.charge_current.battery_balance
                and self.charge_current.balance_soc_pct >= self.charging.max_soc_pct):
            raise ValueError(
                f"charge_current.balance_soc_pct ({self.charge_current.balance_soc_pct}%) "
                f"debe ser menor que charging.max_soc_pct ({self.charging.max_soc_pct}%) "
                "para que el balanceo pueda actuar"
            )
        return self

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
    # YAML parsea fechas como datetime.date — convertir a string antes de Pydantic
    if "tariff" in data and "holidays" in data["tariff"]:
        data["tariff"]["holidays"] = [
            d.isoformat() if hasattr(d, "isoformat") else str(d)
            for d in (data["tariff"]["holidays"] or [])
        ]
    try:
        return AppConfig(**data)
    except Exception as e:
        raise ValueError(f"Error de configuración: {e}") from e


# Claves de config renombradas. {(sección, nombre_obsoleto): nombre_canónico}.
# El modelo sigue aceptando el nombre obsoleto vía AliasChoices; esta tabla
# permite detectarlo para avisar (log + UI) de que conviene migrar al nuevo.
DEPRECATED_CONFIG_KEYS: dict[tuple[str, str], str] = {
    ("charging", "night_consumption_min_days"): "night_consumption_min_days_in_window",
    ("charging", "risk_factor_min_days"):       "risk_factor_min_days_in_window",
    ("charging", "solar_bias_min_days"):        "solar_bias_min_days_in_window",
}


def find_deprecated_config_keys(config_path: str | Path | None = None) -> list[dict]:
    """Detecta claves con nombre obsoleto presentes en el YAML.

    Devuelve [{section, legacy_key, canonical_key, value}] por cada clave
    obsoleta encontrada. El valor se sigue aplicando (vía alias Pydantic);
    esto solo sirve para avisar de que conviene renombrarla.
    """
    try:
        path = _resolve_config_path(config_path)
        data = _load_yaml(path) or {}
    except Exception:
        return []
    found: list[dict] = []
    for (section, legacy), canonical in DEPRECATED_CONFIG_KEYS.items():
        src = data.get(section) or {}
        if isinstance(src, dict) and legacy in src:
            found.append({
                "section": section,
                "legacy_key": legacy,
                "canonical_key": canonical,
                "value": src.get(legacy),
            })
    return found


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
        ("charging", "min_soc_pct"):                         "MIN_SOC_PCT",
        ("charging", "max_soc_pct"):                         "MAX_SOC_PCT",
        ("charging", "safety_margin_kwh"):                   "SAFETY_MARGIN_KWH",
        ("charging", "night_consumption_kwh"):               "NIGHT_CONSUMPTION_KWH",
        ("charging", "solar_bias_factor"):                   "SOLAR_BIAS_FACTOR",
        ("influxdb", "token"):                               "INFLUXDB_TOKEN",
        ("influxdb", "org"):                                 "INFLUXDB_ORG",
        ("influxdb", "bucket"):                              "INFLUXDB_BUCKET",
        ("influxdb", "url"):                                 "INFLUXDB_URL",
        ("system",   "web_api_key"):                         "WEB_API_KEY",
    }
    # Overrides para email (anidados bajo system.email)
    email_overrides = {
        "smtp_host":     "SMTP_HOST",
        "smtp_port":     "SMTP_PORT",
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
            data["system"]["email"][key] = int(value) if key == "smtp_port" else value
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
                "solar_bias_factor",
            }:
                data[section][key] = float(value)
            elif key in {"smtp_port", "web_port"}:
                data[section][key] = int(value)
            else:
                data[section][key] = value
    return data
