"""
server.py — interfaz web con FastAPI.

Endpoints:
  GET  /              → dashboard principal
  GET  /api/status    → estado actual del inversor (JSON)
  GET  /api/forecast  → forecast solar por hora (Solcast, con caché)
  POST /api/run/{test} → ejecutar un test
  GET  /api/stream/{job_id} → stream de logs via SSE
  GET  /api/solar_history → historial forecast vs real (día/semana/mes)
  GET  /api/charge_current_today → cambios de corriente de carga de hoy
  GET  /api/logs      → últimas líneas del log
  POST /api/cycle     → ejecutar ciclo completo manual
"""

import asyncio
import logging
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import os

from app.config import AppConfig, get_host_hostname
from app.logger_reader import house_power_w
from app.version import VERSION

_HOSTNAME = get_host_hostname()

logger = logging.getLogger(__name__)


# ── Edición de config.yaml desde la pestaña Configuración ─────────────────
# Lista blanca de campos editables desde la UI. Para cada campo:
#   (tipo destino, nombre de variable de entorno que lo sobreescribe o None)
# Los secretos (api_key, password, smtp_password, INFLUXDB_TOKEN, web_api_key,
# etc.) NO están aquí — siguen únicamente en .env. `tariff.periods` tampoco:
# es la única estructura anidada del fichero y no cambia nunca.
_EDITABLE_FIELDS: dict[str, dict[str, tuple[str, str | None]]] = {
    "solcast": {
        "base_url":         ("str_required", None),
        "forecast_hours":   ("int",   None),
        "cache_ttl_hours":  ("int",   None),
    },
    "inverter": {
        "modbus_port":             ("int", None),
        "modbus_slave":            ("int", None),
        "browser_timeout_seconds": ("int", None),
    },
    "installation": {
        "battery_capacity_kwh":          ("float", "BATTERY_CAPACITY_KWH"),
        "average_daily_consumption_kwh": ("float", "DAILY_CONSUMPTION_KWH"),
        "peak_power_kwp":                ("float", None),
    },
    "tariff": {
        "schedule_at":         ("time_hhmm",    None),
        "schedule_recheck_at": ("str_nullable", None),
        "night_cutoff_hour":   ("int",          None),
        "weekend_days":        ("int_list",     None),
        "holidays":            ("date_list",    None),
    },
    "charging": {
        "risk_factor":                   ("float", "RISK_FACTOR"),
        "min_soc_pct":                   ("float", "MIN_SOC_PCT"),
        "max_soc_pct":                   ("float", "MAX_SOC_PCT"),
        "safety_margin_kwh":             ("float", "SAFETY_MARGIN_KWH"),
        "night_consumption_kwh":               ("float", "NIGHT_CONSUMPTION_KWH"),
        "night_consumption_min_days_in_window": ("int",   None),
        "night_consumption_window_days":       ("int",   None),
        "risk_factor_min_days_in_window":      ("int",   None),
        "risk_factor_window_days":             ("int",   None),
        "solar_bias_factor":                   ("float", "SOLAR_BIAS_FACTOR"),
        "solar_bias_min_days_in_window":       ("int",   None),
        "solar_bias_window_days":              ("int",   None),
        "daily_consumption_min_days_in_window": ("int",  None),
        "daily_consumption_window_days":        ("int",  None),
    },
    "charge_current": {
        "enabled":                          ("bool",  None),
        "interval_min":                     ("int",   None),
        "floor_a":                          ("int",   None),
        "max_a":                            ("int",   None),
        "margin":                           ("float", None),
        "house_power_window_min":           ("int",   None),
        "house_power_cache_min":            ("int",   None),
        "house_profile_window_days":        ("int",   None),
        "house_profile_min_days_in_window": ("int",   None),
        "temp_gate_enabled":                ("bool",  None),
        "hot_threshold_c":                  ("float", None),
        "productive_window_pct":            ("int",   None),
        "productive_window_end_hour":       ("float", None),
        "battery_balance":                  ("bool",  None),
        "balance_soc_pct":                  ("float", None),
        "balance_soc_pct_2":                ("float", None),
        "balance_floor_a":                  ("int",   None),
    },
    # Backup externo por SCP. No hay secretos: la autenticación es por clave
    # SSH montada como fichero, así que la sección se expone entera.
    "backup": {
        "enabled":                  ("bool", None),
        "schedule_at":              ("time_hhmm", None),
        "host":                     ("str",  None),
        "port":                     ("int",  None),
        "user":                     ("str",  None),
        "remote_dir":               ("str",  None),
        "ssh_key_path":             ("str",  None),
        "strict_host_key_checking": ("bool", None),
        "known_hosts_path":         ("str",  None),
        "retention":                ("int",  None),
        "timeout_seconds":          ("int",  None),
    },
    "system": {
        "log_level":   ("str_required", "LOG_LEVEL"),
        "log_file":    ("str_required", None),
        "dry_run":     ("bool", "DRY_RUN"),
        "timezone":    ("str_required", None),
        "web_port":    ("int",  None),
        "web_enabled": ("bool", None),
    },
    # email vive bajo system.email.* en el YAML pero se expone como
    # sección hermana en la API para simplificar el form de la web.
    "email": {
        "enabled":     ("bool", None),
        "smtp_port":   ("int",  "SMTP_PORT"),
        "use_tls":     ("bool", None),
        "use_ssl":     ("bool", None),
        "verify_ssl":  ("bool", None),
        "mail_from":   ("str",  "MAIL_FROM"),
        "mail_to":     ("str",  "MAIL_TO"),
    },
    "influxdb": {
        "enabled": ("bool", None),
        "url":     ("str_required", "INFLUXDB_URL"),
        "org":     ("str_required", "INFLUXDB_ORG"),
        "bucket":  ("str_required", "INFLUXDB_BUCKET"),
    },
}


def _cast_value(raw, typ: str):
    """Convierte un valor recibido por JSON al tipo destino del YAML.

    La cadena vacía es un valor legítimo para los campos de texto: `mail_from`,
    `mail_to` o `backup.host` están vacíos en el YAML cuando el valor real vive
    en .env o la función no se usa. Rechazarla obligaba a que un guardado que
    no tocaba esos campos fallara entero (el form enviaba todos los campos, no
    solo los modificados). Sigue rechazándose en int/float/bool, donde "" no
    significa nada.
    """
    if typ == "str_nullable":
        if raw is None or raw == "":
            return None
        return str(raw)
    if typ == "str":
        return "" if raw is None else str(raw)
    if typ in ("int_list", "date_list"):
        return _cast_list(raw, typ)
    if raw is None or raw == "":
        raise ValueError("valor vacío")
    if typ == "time_hhmm":
        s = str(raw).strip()
        try:
            hh, mm = s.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except ValueError:
            raise ValueError(
                f"{s!r} no es una hora válida; se esperaba HH:MM"
            ) from None
        return f"{int(hh):02d}:{int(mm):02d}"
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw)
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"no booleano: {raw!r}")
    return str(raw)


def _yaml_list(items: list, flow: bool):
    """Envuelve una lista en un CommentedSeq con el estilo deseado.

    Sin esto ruamel escribe siempre en bloque y `weekend_days: [5, 6]` pasaría
    a ocupar tres líneas al guardar desde la web.
    """
    from ruamel.yaml.comments import CommentedSeq

    seq = CommentedSeq(items)
    if flow:
        seq.fa.set_flow_style()
    else:
        seq.fa.set_block_style()
    return seq


def _cast_list(raw, typ: str) -> list:
    """Convierte una lista recibida por JSON (o CSV) a la lista del YAML.

    `int_list`  → tariff.weekend_days: enteros 0..6 (0=lunes), ordenados y sin
                  duplicados.
    `date_list` → tariff.holidays: fechas "YYYY-MM-DD" ordenadas y sin
                  duplicados. Se validan aquí para que un festivo mal escrito
                  no llegue al YAML: Pydantic solo las coacciona a str, así que
                  "2026-13-99" pasaría su validación y rompería is_valley_day
                  en silencio (nunca coincidiría con ninguna fecha real).
    """
    from datetime import date as _date

    if raw is None or raw == "":
        return []
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")

    out: list = []
    for item in items:
        s = str(item).strip()
        if not s:
            continue
        if typ == "int_list":
            try:
                n = int(s)
            except ValueError:
                raise ValueError(f"{s!r} no es un número entero") from None
            if not 0 <= n <= 6:
                raise ValueError(f"día de la semana fuera de rango 0-6: {n}")
            if n not in out:
                out.append(n)
        else:
            try:
                _date.fromisoformat(s)
            except ValueError:
                raise ValueError(
                    f"{s!r} no es una fecha válida; se esperaba YYYY-MM-DD"
                ) from None
            if s not in out:
                out.append(s)
    return sorted(out)


# Modelo Pydantic que respalda cada sección de _EDITABLE_FIELDS, para poder
# resolver el valor por defecto de una clave ausente del YAML (ver
# _section_defaults). "email" vive anidada bajo system.email en el fichero.
_SECTION_MODELS: dict[str, str] = {
    "solcast":        "SolcastConfig",
    "inverter":       "InverterConfig",
    "installation":   "InstallationConfig",
    "tariff":         "TariffConfig",
    "charging":       "ChargingConfig",
    "charge_current": "ChargeCurrentConfig",
    "backup":         "BackupConfig",
    "system":         "SystemConfig",
    "email":          "EmailConfig",
    "influxdb":       "InfluxDBConfig",
}


def _section_defaults(section: str) -> dict:
    """Valores por defecto del modelo Pydantic de una sección.

    Permite que GET /api/config devuelva el valor EFECTIVO de un campo ausente
    del YAML en vez de null. Con null el form pintaba un input vacío que ni
    mostraba lo que hace el sistema ni se podía guardar ("valor numérico
    inválido"), y los parámetros añadidos después del último `config.yaml`
    (charge_current.temp_gate_enabled, house_profile_*, …) quedaban invisibles
    aunque estuvieran gobernando el comportamiento.
    """
    import app.config as _cfgmod
    from pydantic_core import PydanticUndefined

    model = getattr(_cfgmod, _SECTION_MODELS.get(section, ""), None)
    if model is None:
        return {}
    out = {}
    for name, field in model.model_fields.items():
        if field.default is not PydanticUndefined:
            out[name] = field.default
        elif field.default_factory is not None:
            try:
                out[name] = field.default_factory()
            except Exception:
                continue
    return out


def _current_solar_bias(cfg: AppConfig) -> float:
    """Factor de calibración solar a aplicar en los totales mostrados:
    dinámico desde InfluxDB si hay suficientes días, fallback al config."""
    from app.storage import get_dynamic_solar_bias
    try:
        bias = get_dynamic_solar_bias(
            cfg.influxdb,
            window_days=cfg.charging.solar_bias_window_days,
            min_days=cfg.charging.solar_bias_min_days_in_window,
        )
    except Exception:
        bias = None
    return bias if bias is not None else cfg.charging.solar_bias_factor

app = FastAPI(title="solar-manager", docs_url=None, redoc_url=None)

# Buffer de jobs en ejecución: job_id → deque de líneas de log
_jobs: dict[str, deque] = {}
_job_status: dict[str, str] = {}  # running | ok | error


def create_app(cfg: AppConfig) -> FastAPI:
    """Crea y configura la aplicación FastAPI."""

    def _require_api_key(x_api_key: str = Header(default="")):
        key = cfg.system.web_api_key
        if key and x_api_key != key:
            raise HTTPException(status_code=403, detail="API key inválida o ausente")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        html = Path(__file__).parent / "templates" / "index.html"
        return (html.read_text(encoding="utf-8")
                .replace("{{VERSION}}", VERSION)
                .replace("{{HOSTNAME}}", _HOSTNAME))

    @app.get("/api/version")
    async def version():
        return {"version": VERSION, "hostname": _HOSTNAME}

    @app.get("/api/status")
    async def status():
        """Lee el estado actual del inversor vía MODBUS."""
        try:
            from app.inverter import read_inverter_state
            state = read_inverter_state(cfg.inverter)
            return {
                "ok": True,
                "inverter_status": state.inverter_status,
                "battery_status": state.battery_status,
                "soc_pct": state.soc_pct,
                "soh_pct": state.soh_pct,
                "battery_power_w": state.battery_power_w,
                "battery_voltage_v": state.battery_voltage_v,
                "battery_temp_c": state.battery_temp_c,
                "charge_current_max_a": state.charge_current_max_a,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    @app.get("/api/forecast")
    async def forecast():
        """
        Devuelve el forecast solar del día siguiente con resolución horaria.

        Usa la caché de Solcast (misma que usa main.py) para no consumir
        llamadas extra a la API. Agrupa los intervalos de 30 min por hora
        y devuelve p10/p50/p90 en kW para cada hora del día.

        Respuesta:
          {
            "ok": true,
            "date": "2026-04-14",
            "hours":  [6, 7, 8, ..., 20],
            "p10_kw": [0.05, 0.2, ...],   # kW promedio esa hora
            "p50_kw": [0.1, 0.4, ...],
            "p90_kw": [0.15, 0.6, ...],
            "total_p10_kwh": 8.2,
            "total_p50_kwh": 14.5,
            "total_p90_kwh": 20.1,
            "cached_at": "2026-04-13T23:55:01",
            "intervals": 48                # número de intervalos de 30min usados
          }
        """
        import json
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        from pathlib import Path

        CACHE_PATH = Path("/app/logs/solcast_cache.json")

        try:
            # ── 1. Leer caché (misma ruta que usa solcast.py) ──────────
            if not CACHE_PATH.exists():
                # No hay caché: hacer llamada real y guardar
                from app.solcast import _fetch_forecasts, _save_cache
                raw = _fetch_forecasts(cfg.solcast)
                _save_cache(raw)
                cached_at = datetime.now().isoformat()
            else:
                cache_data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                raw = cache_data.get("raw", {})
                cached_at = cache_data.get("cached_at", "")

            forecasts = raw.get("forecasts", [])
            if not forecasts:
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "error": "Caché de Solcast vacía o sin datos"},
                )

            # ── 2. Filtrar intervalos del día siguiente ─────────────────
            tz = ZoneInfo(cfg.system.timezone)
            today = datetime.now(tz).date()
            tomorrow = today + timedelta(days=1)

            # Agrupar por hora: hora_local → lista de (p10, p50, p90)
            by_hour: dict[int, list[tuple[float, float, float]]] = {}
            used_intervals = 0

            for item in forecasts:
                try:
                    ts_str = item["period_end"].rstrip("Z").split(".")[0] + "+00:00"
                    ts_utc = datetime.fromisoformat(ts_str)
                    ts_local = ts_utc.astimezone(tz)
                    if ts_local.date() != tomorrow:
                        continue
                    hour = ts_local.hour
                    by_hour.setdefault(hour, []).append((
                        float(item.get("pv_estimate10", 0.0)),
                        float(item.get("pv_estimate",   0.0)),
                        float(item.get("pv_estimate90", 0.0)),
                    ))
                    used_intervals += 1
                except (KeyError, ValueError):
                    continue

            if not by_hour:
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "error": f"Sin datos para mañana ({tomorrow}) en la caché"},
                )

            # ── 3. Construir series horarias (solo horas con producción) ─
            # Intervalo de interés: de la primera a la última hora con datos
            active_hours = sorted(by_hour.keys())

            hours_out, p10_out, p50_out, p90_out = [], [], [], []
            for h in active_hours:
                intervals_h = by_hour[h]
                # Cada intervalo es de 30 min → promedio = (suma / n) * n / n
                # Para el gráfico queremos kW promedio de la hora
                n = len(intervals_h)
                p10_out.append(round(sum(v[0] for v in intervals_h) / n, 3))
                p50_out.append(round(sum(v[1] for v in intervals_h) / n, 3))
                p90_out.append(round(sum(v[2] for v in intervals_h) / n, 3))
                hours_out.append(h)

            # Totales en kWh: cada intervalo de 30 min = kW × 0.5 h
            INTERVAL_H = 0.5
            total_p10 = round(sum(v[0] for vals in by_hour.values() for v in vals) * INTERVAL_H, 2)
            total_p50 = round(sum(v[1] for vals in by_hour.values() for v in vals) * INTERVAL_H, 2)
            total_p90 = round(sum(v[2] for vals in by_hour.values() for v in vals) * INTERVAL_H, 2)

            bias = _current_solar_bias(cfg)
            total_p50_calibrated = round(total_p50 * bias, 2)

            return {
                "ok": True,
                "date": tomorrow.isoformat(),
                "hours":  hours_out,
                "p10_kw": p10_out,
                "p50_kw": p50_out,
                "p90_kw": p90_out,
                "total_p10_kwh": total_p10,
                "total_p50_kwh": total_p50,
                "total_p50_calibrated_kwh": total_p50_calibrated,
                "total_p90_kwh": total_p90,
                "solar_bias_factor": round(bias, 4),
                "cached_at": cached_at,
                "intervals": used_intervals,
            }

        except Exception as e:
            logger.exception("Error en /api/forecast")
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    @app.post("/api/run/{test_name}", dependencies=[Depends(_require_api_key)])
    async def run_test(test_name: str):
        """Lanza un test en background y devuelve un job_id para seguir el stream."""
        allowed = {
            "inverter":   "app.test_inverter",
            "solcast":    "app.test_solcast",
            "decision":   "app.test_decision",
            "config":     "app.test_config",
            "config_web": "app.test_config_web",
            "automation": "app.test_automation",
            "main":       "app.test_main",
            "logger_reader": "app.test_logger_reader",
            "charge_current": "app.test_charge_current",
            "charge_current_scenarios": "app.test_charge_current_scenarios",
            "simulate_current": "app.simulate_charge_current",
        }
        if test_name not in allowed:
            return JSONResponse(status_code=400,
                                content={"error": f"Test desconocido: {test_name}"})

        job_id = str(uuid.uuid4())[:8]
        _jobs[job_id] = deque(maxlen=500)
        _job_status[job_id] = "running"

        def _run():
            try:
                module = allowed[test_name]
                proc = subprocess.Popen(
                    [sys.executable, "-m", module],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    _jobs[job_id].append(line.rstrip())
                proc.wait()
                _job_status[job_id] = "ok" if proc.returncode == 0 else "error"
            except Exception as e:
                _jobs[job_id].append(f"ERROR: {e}")
                _job_status[job_id] = "error"

        threading.Thread(target=_run, daemon=True).start()
        return {"job_id": job_id}

    @app.post("/api/cycle", dependencies=[Depends(_require_api_key)])
    async def run_cycle(dry_run: bool = True):
        """Ejecuta el ciclo completo manualmente."""
        job_id = str(uuid.uuid4())[:8]
        _jobs[job_id] = deque(maxlen=500)
        _job_status[job_id] = "running"

        def _run():
            try:
                args = [sys.executable, "-m", "app.test_main"]
                if not dry_run:
                    args.append("--write")
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    _jobs[job_id].append(line.rstrip())
                proc.wait()
                _job_status[job_id] = "ok" if proc.returncode == 0 else "error"
            except Exception as e:
                _jobs[job_id].append(f"ERROR: {e}")
                _job_status[job_id] = "error"

        threading.Thread(target=_run, daemon=True).start()
        return {"job_id": job_id}

    @app.get("/api/stream/{job_id}")
    async def stream_logs(job_id: str):
        """Stream de logs de un job via Server-Sent Events."""
        if job_id not in _jobs:
            return JSONResponse(status_code=404, content={"error": "Job no encontrado"})

        async def _generate() -> AsyncGenerator[str, None]:
            sent = 0
            while True:
                lines = list(_jobs[job_id])
                for line in lines[sent:]:
                    yield f"data: {line}\n\n"
                    sent += 1
                if _job_status.get(job_id) != "running" and sent >= len(lines):
                    yield f"data: __DONE__:{_job_status.get(job_id, 'ok')}\n\n"
                    break
                await asyncio.sleep(0.2)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/params")
    async def params():
        """Parámetros dinámicos actuales del algoritmo (consumo nocturno y risk factor)."""
        import asyncio

        def _compute():
            from app.storage import (
                get_avg_night_consumption, get_dynamic_risk_factor, get_dynamic_solar_bias,
            )
            night = get_avg_night_consumption(
                cfg.influxdb,
                window_days=cfg.charging.night_consumption_window_days,
                min_days=cfg.charging.night_consumption_min_days_in_window,
            )
            rf = get_dynamic_risk_factor(
                cfg.influxdb,
                window_days=cfg.charging.risk_factor_window_days,
                min_days=cfg.charging.risk_factor_min_days_in_window,
            )
            bias = get_dynamic_solar_bias(
                cfg.influxdb,
                window_days=cfg.charging.solar_bias_window_days,
                min_days=cfg.charging.solar_bias_min_days_in_window,
            )

            night_count = 0
            rf_count = 0
            bias_count = 0
            if cfg.influxdb.enabled:
                try:
                    from influxdb_client import InfluxDBClient
                    wd_n = cfg.charging.night_consumption_window_days
                    wd_r = cfg.charging.risk_factor_window_days + 2
                    wd_b = cfg.charging.solar_bias_window_days + 2
                    q_n = f'''from(bucket:"{cfg.influxdb.bucket}")
  |> range(start: -{wd_n}d)
  |> filter(fn:(r) => r._measurement=="stats_diarias" and r._field=="night_consumption_kwh")
  |> filter(fn:(r) => r._value > 0.5)
  |> count()'''
                    q_r = f'''from(bucket:"{cfg.influxdb.bucket}")
  |> range(start: -{wd_r}d)
  |> filter(fn:(r) => r._measurement=="stats_diarias" and r._field=="solar_kwh")
  |> filter(fn:(r) => r._value > 0.5)
  |> count()'''
                    q_b = f'''from(bucket:"{cfg.influxdb.bucket}")
  |> range(start: -{wd_b}d)
  |> filter(fn:(r) => r._measurement=="stats_diarias" and r._field=="solar_kwh")
  |> filter(fn:(r) => r._value > 0.5)
  |> count()'''
                    with InfluxDBClient(url=cfg.influxdb.url, token=cfg.influxdb.token,
                                        org=cfg.influxdb.org) as client:
                        api = client.query_api()
                        for t in api.query(q_n, org=cfg.influxdb.org):
                            for rec in t.records:
                                night_count = int(rec.get_value())
                        for t in api.query(q_r, org=cfg.influxdb.org):
                            for rec in t.records:
                                rf_count = int(rec.get_value())
                        for t in api.query(q_b, org=cfg.influxdb.org):
                            for rec in t.records:
                                bias_count = int(rec.get_value())
                except Exception:
                    pass

            return night, rf, bias, night_count, rf_count, bias_count

        try:
            night, rf, bias, night_count, rf_count, bias_count = await asyncio.to_thread(_compute)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

        return {
            "ok": True,
            "night_consumption_kwh": night if night is not None else cfg.charging.night_consumption_kwh,
            "night_dynamic": night is not None,
            "night_config_kwh": cfg.charging.night_consumption_kwh,
            "night_min_days": cfg.charging.night_consumption_min_days_in_window,
            "night_window_days": cfg.charging.night_consumption_window_days,
            "night_valid_days": night_count,
            "risk_factor": rf if rf is not None else cfg.charging.risk_factor,
            "risk_dynamic": rf is not None,
            "risk_config": cfg.charging.risk_factor,
            "risk_min_days": cfg.charging.risk_factor_min_days_in_window,
            "risk_window_days": cfg.charging.risk_factor_window_days,
            "risk_valid_days": rf_count,
            "solar_bias_factor": bias if bias is not None else cfg.charging.solar_bias_factor,
            "solar_bias_dynamic": bias is not None,
            "solar_bias_config": cfg.charging.solar_bias_factor,
            "solar_bias_min_days": cfg.charging.solar_bias_min_days_in_window,
            "solar_bias_window_days": cfg.charging.solar_bias_window_days,
            "solar_bias_valid_days": bias_count,
        }

    @app.get("/api/today_solar")
    async def today_solar():
        """Producción solar, consumo casa y flujo de red de hoy (datalogger del inversor, minuto a minuto)."""
        import asyncio
        import requests as _req
        from datetime import date as _date

        today = _date.today()
        date_str = today.isoformat()
        host = cfg.inverter.get_modbus_host()
        device_id = cfg.inverter.device_id
        if not device_id:
            return JSONResponse(status_code=503, content={"ok": False, "error": "INVERTER_DEVICE_ID no configurado"})

        url = f"http://{host}/inverter/log/{device_id}/{date_str}"

        def _fetch():
            r = _req.get(url, auth=(cfg.inverter.username, cfg.inverter.password), timeout=15)
            r.raise_for_status()
            return r.json()

        try:
            data = await asyncio.to_thread(_fetch)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

        records = [entry["val"] for entry in data.get("data", [])]
        if not records:
            return {"ok": True, "date": date_str, "hours": [], "solar_kw": [],
                    "current_solar_w": 0, "total_solar_kwh": 0.0,
                    "current_grid_w": 0, "current_house_w": 0}

        last = records[-1]
        current_solar_w = round(last.get("Pdc1", 0) + last.get("Pdc2", 0))
        current_grid_w = round(last.get("PacMeter", 0))
        # casa = PacGrid + PacMeter, no PacGrid a secas: ese es la salida AC del
        # inversor e incluye lo exportado (a mediodía marcaba 1468 W con la casa
        # consumiendo 434 W). Ver `house_power_w`.
        current_house_w = round(house_power_w(last))

        total_solar_kwh = round(
            sum(r.get("Pdc1", 0) + r.get("Pdc2", 0) for r in records) / 1000 / 60, 2
        )

        # Agrupar por hora: registro i → hora local i//60 (datalogger en hora local)
        by_hour: dict[int, list] = {}
        for i, r in enumerate(records):
            by_hour.setdefault(i // 60, []).append(r.get("Pdc1", 0) + r.get("Pdc2", 0))

        hours_out, solar_kw_out = [], []
        for h in sorted(by_hour):
            avg_w = sum(by_hour[h]) / len(by_hour[h])
            hours_out.append(h)
            solar_kw_out.append(round(avg_w / 1000, 3))

        return {
            "ok": True,
            "date": date_str,
            "hours": hours_out,
            "solar_kw": solar_kw_out,
            "current_solar_w": current_solar_w,
            "total_solar_kwh": total_solar_kwh,
            "current_grid_w": current_grid_w,
            "current_house_w": current_house_w,
        }

    @app.get("/api/charge_current_today")
    async def charge_current_today():
        """Cambios de la corriente máxima de carga registrados hoy (measurement corriente_carga)."""
        import asyncio as _asyncio
        from app.storage import get_charge_current_changes, StorageError
        try:
            changes = await _asyncio.to_thread(get_charge_current_changes, cfg.influxdb)
        except StorageError as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
        return {"ok": True, "changes": changes, "count": len(changes)}

    @app.get("/api/solar_history")
    async def solar_history(date: str, view: str = "day"):
        """Historial de producción solar vs forecast desde InfluxDB (día / semana / mes)."""
        import asyncio as _asyncio
        from datetime import date as _date, timedelta as _td

        if not cfg.influxdb.enabled:
            return JSONResponse(status_code=503, content={"ok": False, "error": "InfluxDB no habilitado"})

        def _compute():
            from app.storage import get_solar_history_day, get_solar_history_range
            _MONTHS = ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"]

            try:
                d = _date.fromisoformat(date)
            except ValueError:
                return None, "Fecha inválida"

            today = _date.today()

            def _day_label(dt: _date) -> str:
                if dt == today:             return "hoy"
                if dt == today - _td(1):   return "ayer"
                return f"{dt.day} {_MONTHS[dt.month - 1]}"

            def _short(dt: _date) -> str:
                return f"{dt.day} {_MONTHS[dt.month - 1]}"

            if view == "day":
                data = get_solar_history_day(cfg.influxdb, date)

                # Fallback: si InfluxDB no tiene forecast para este día, leer caché Solcast
                if not data.get("has_forecast"):
                    try:
                        import json as _json
                        from pathlib import Path as _Path
                        from zoneinfo import ZoneInfo as _ZoneInfo
                        from datetime import datetime as _dt

                        _cache = _Path("/app/logs/solcast_cache.json")
                        if _cache.exists():
                            _raw = _json.loads(_cache.read_text(encoding="utf-8"))
                            _fcs = _raw.get("raw", {}).get("forecasts", [])
                            _tz  = _ZoneInfo(cfg.system.timezone)
                            _tgt = _date.fromisoformat(date)
                            _bh: dict = {}
                            for _item in _fcs:
                                try:
                                    _ts_str = _item["period_end"].rstrip("Z").split(".")[0] + "+00:00"
                                    _ts_loc = _dt.fromisoformat(_ts_str).astimezone(_tz)
                                    if _ts_loc.date() != _tgt:
                                        continue
                                    _h = _ts_loc.hour
                                    _bh.setdefault(_h, []).append((
                                        float(_item.get("pv_estimate10", 0.0)),
                                        float(_item.get("pv_estimate",   0.0)),
                                        float(_item.get("pv_estimate90", 0.0)),
                                    ))
                                except (KeyError, ValueError):
                                    continue
                            if _bh:
                                _hrs = sorted(_bh.keys())
                                _real = dict(zip(data.get("hours", []), data.get("real_kw", [])))
                                data = {
                                    **data,
                                    "hours":         _hrs,
                                    "p50_kw":        [round(sum(v[1] for v in _bh[h]) / len(_bh[h]), 3) for h in _hrs],
                                    "p10_kw":        [round(sum(v[0] for v in _bh[h]) / len(_bh[h]), 3) for h in _hrs],
                                    "p90_kw":        [round(sum(v[2] for v in _bh[h]) / len(_bh[h]), 3) for h in _hrs],
                                    "real_kw":       [_real.get(h) for h in _hrs],
                                    "total_p50_kwh": round(sum(v[1] for vs in _bh.values() for v in vs) * 0.5, 2),
                                    "has_forecast":  True,
                                }
                            else:
                                logger.debug(f"Solcast cache sin intervalos para {date}")
                    except Exception as _e:
                        logger.warning(f"Fallback Solcast para {date} falló: {_e}")

                return {**data, "ok": True, "date": date, "view": view,
                        "label": _day_label(d)}, None

            elif view == "week":
                ws = d - _td(days=d.weekday())      # lunes de la semana
                we = ws + _td(days=7)
                data = get_solar_history_range(cfg.influxdb, ws.isoformat(), we.isoformat())
                label = f"{_short(ws)} – {_short(ws + _td(6))}"
                return {**data, "ok": True, "date": date, "view": view, "label": label,
                        "range_start": ws.isoformat(),
                        "range_end": (we - _td(1)).isoformat()}, None

            elif view == "month":
                ms = d.replace(day=1)
                me = (ms.replace(month=ms.month + 1) if ms.month < 12
                      else ms.replace(year=ms.year + 1, month=1))
                data = get_solar_history_range(cfg.influxdb, ms.isoformat(), me.isoformat())
                label = f"{_MONTHS[ms.month - 1].capitalize()} {ms.year}"
                return {**data, "ok": True, "date": date, "view": view, "label": label,
                        "range_start": ms.isoformat(),
                        "range_end": (me - _td(1)).isoformat()}, None

            return None, f"Vista desconocida: {view}"

        try:
            result, error = await _asyncio.to_thread(_compute)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

        if error:
            return JSONResponse(status_code=400, content={"ok": False, "error": error})

        # Versión calibrada del total estimado (multiplica el forecast crudo
        # por el bias dinámico actual). El dato persistido sigue siendo el
        # forecast Solcast crudo; la calibración se aplica solo al mostrar.
        bias = _current_solar_bias(cfg)
        raw_total = result.get("total_p50_kwh") or 0
        result["total_p50_calibrated_kwh"] = round(raw_total * bias, 2)
        result["solar_bias_factor"] = round(bias, 4)
        return result

    @app.get("/api/config")
    async def get_config():
        """Devuelve el contenido editable de config.yaml + flags de overrides por .env.

        Respuesta:
          {
            "ok": true,
            "values": { "<seccion>": { "<key>": <valor>, ... }, ... },
            "env_overrides": [ {"section": "...", "key": "...", "env_var": "..."}, ... ],
            "path": "/app/config.yaml"
          }
        """
        try:
            from app.config import _resolve_config_path
            from ruamel.yaml import YAML

            from app.config import DEPRECATED_CONFIG_KEYS
            # (sección, nombre_canónico) -> nombre_obsoleto, para leer el valor
            # si el YAML aún tiene la clave con el nombre viejo.
            canon_to_legacy = {(s, c): lg for (s, lg), c in DEPRECATED_CONFIG_KEYS.items()}

            path = _resolve_config_path(None)
            yaml = YAML(typ="safe")
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.load(f) or {}

            values: dict[str, dict] = {}
            env_overrides: list[dict] = []
            legacy_keys: list[dict] = []
            default_keys: list[dict] = []
            for section, fields in _EDITABLE_FIELDS.items():
                if section == "email":
                    src = (data.get("system") or {}).get("email") or {}
                else:
                    src = data.get(section) or {}
                defaults = _section_defaults(section)
                values[section] = {}
                for key, (_typ, env_var) in fields.items():
                    val = src.get(key)
                    legacy = canon_to_legacy.get((section, key))
                    if val is None and legacy and src.get(legacy) is not None:
                        val = src.get(legacy)
                        legacy_keys.append({
                            "section": section, "key": key, "legacy_key": legacy,
                        })
                    # Clave ausente del YAML: mostrar el valor por defecto del
                    # modelo, que es el que el sistema está usando de verdad.
                    if key not in src and key in defaults:
                        val = defaults[key]
                        default_keys.append({"section": section, "key": key})
                    if isinstance(val, date):
                        val = val.isoformat()
                    elif isinstance(val, list):
                        val = [d.isoformat() if isinstance(d, date) else d for d in val]
                    values[section][key] = val
                    if env_var and os.environ.get(env_var) is not None:
                        env_overrides.append({
                            "section": section,
                            "key": key,
                            "env_var": env_var,
                        })

            return {
                "ok": True,
                "values": values,
                "env_overrides": env_overrides,
                "legacy_keys": legacy_keys,
                "default_keys": default_keys,
                "path": str(path),
            }
        except Exception as e:
            logger.exception("Error en GET /api/config")
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    @app.post("/api/config", dependencies=[Depends(_require_api_key)])
    async def post_config(payload: dict):
        """Aplica los `values` recibidos a config.yaml preservando comentarios.

        Cuerpo:
          { "values": { "<seccion>": { "<key>": <valor>, ... }, ... } }

        Valida el resultado con el modelo Pydantic AppConfig antes de escribir.
        Los cambios surten efecto tras reiniciar el contenedor.
        """
        from app.config import _resolve_config_path, AppConfig, DEPRECATED_CONFIG_KEYS
        from ruamel.yaml import YAML
        import json as _json

        canon_to_legacy = {(s, c): lg for (s, lg), c in DEPRECATED_CONFIG_KEYS.items()}

        updates = (payload or {}).get("values") or {}
        if not isinstance(updates, dict) or not updates:
            return JSONResponse(status_code=400, content={
                "ok": False, "errors": ["payload.values vacío o inválido"]})

        path = _resolve_config_path(None)

        # Round-trip preserva comentarios, orden y comillas.
        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        with open(path, "r", encoding="utf-8") as f:
            data = yaml_rt.load(f)
        if data is None:
            return JSONResponse(status_code=500, content={
                "ok": False, "errors": [f"config.yaml vacío en {path}"]})

        # Aplicar updates respetando la lista blanca y convirtiendo tipos.
        errors: list[str] = []
        applied = 0
        for section, fields in updates.items():
            if section not in _EDITABLE_FIELDS:
                errors.append(f"Sección desconocida: {section}")
                continue
            if not isinstance(fields, dict):
                errors.append(f"Sección {section}: se esperaba objeto")
                continue
            if section == "email":
                if "system" not in data or data["system"] is None:
                    data["system"] = {}
                if "email" not in data["system"] or data["system"]["email"] is None:
                    data["system"]["email"] = {}
                target = data["system"]["email"]
            else:
                if section not in data or data[section] is None:
                    data[section] = {}
                target = data[section]
            for key, raw_val in fields.items():
                if key not in _EDITABLE_FIELDS[section]:
                    errors.append(f"Campo no editable: {section}.{key}")
                    continue
                typ, _env = _EDITABLE_FIELDS[section][key]
                try:
                    cast_val = _cast_value(raw_val, typ)
                except Exception as e:
                    errors.append(f"{section}.{key}: {e}")
                    continue
                if isinstance(cast_val, list):
                    # weekend_days cabe en una línea; holidays crece cada año,
                    # así que en bloque se lee (y se diffea en git) mejor.
                    cast_val = _yaml_list(cast_val, flow=(typ == "int_list"))
                target[key] = cast_val
                # Migración: si el YAML aún tenía la clave con el nombre obsoleto,
                # eliminarla al escribir la canónica (evita dejar las dos).
                legacy = canon_to_legacy.get((section, key))
                if legacy and legacy in target:
                    del target[legacy]
                applied += 1

        if errors:
            return JSONResponse(status_code=400, content={"ok": False, "errors": errors})

        # Validar el resultado completo contra el modelo Pydantic. Serializamos
        # a JSON y de vuelta para normalizar dates de holidays y CommentedMap.
        try:
            plain = _json.loads(_json.dumps(data, default=str))
            if "tariff" in plain and "holidays" in plain["tariff"]:
                plain["tariff"]["holidays"] = [
                    str(d) for d in (plain["tariff"]["holidays"] or [])
                ]
            AppConfig(**plain)
        except Exception as e:
            return JSONResponse(status_code=400, content={
                "ok": False, "errors": [f"Validación de la configuración resultante: {e}"]})

        # Escribir preservando comentarios.
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml_rt.dump(data, f)
        except Exception as e:
            logger.exception("Error escribiendo config.yaml")
            return JSONResponse(status_code=500, content={
                "ok": False, "errors": [f"No se pudo escribir {path}: {e}"]})

        logger.info(f"config.yaml actualizado vía web: {applied} campos modificados")
        return {
            "ok": True,
            "applied": applied,
            "path": str(path),
            "note": "Cambios escritos. Reinicia el contenedor para que surtan efecto.",
        }

    @app.get("/api/db/export", dependencies=[Depends(_require_api_key)])
    async def db_export():
        """Exporta un backup consistente de InfluxDB como .tar.gz.

        Usa `influx backup` (online, sin parar el contenedor) sobre el bucket
        configurado y empaqueta el resultado en un único .tar.gz descargable.
        El token se pasa por variable de entorno (no en argv) para no exponerlo.
        """
        import shutil
        import tarfile
        import tempfile
        from fastapi.responses import FileResponse
        from starlette.background import BackgroundTask

        if not cfg.influxdb.enabled:
            raise HTTPException(status_code=503, detail="InfluxDB no habilitado")

        def _build() -> tuple[str, str]:
            tmp = tempfile.mkdtemp(prefix="influx-backup-")
            backup_dir = os.path.join(tmp, "backup")
            cmd = [
                "influx", "backup", backup_dir,
                "--host", cfg.influxdb.url,
                "--org", cfg.influxdb.org,
                "--bucket", cfg.influxdb.bucket,
            ]
            env = {**os.environ, "INFLUX_TOKEN": cfg.influxdb.token}
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=600,
            )
            if proc.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError(proc.stderr.strip() or "influx backup falló")
            archive = os.path.join(tmp, "archive.tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(backup_dir, arcname="backup")
            return tmp, archive

        try:
            tmp, archive = await asyncio.to_thread(_build)
        except Exception as e:
            logger.error(f"Export de InfluxDB falló: {e}")
            raise HTTPException(status_code=500, detail=f"Export falló: {e}")

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        fname = f"influxdb-{cfg.influxdb.bucket}-{ts}.tar.gz"
        logger.info(f"Export de InfluxDB generado: {fname}")
        return FileResponse(
            archive,
            media_type="application/gzip",
            filename=fname,
            background=BackgroundTask(lambda: shutil.rmtree(tmp, ignore_errors=True)),
        )

    @app.post("/api/backup/run", dependencies=[Depends(_require_api_key)])
    async def backup_run():
        """Lanza una copia de seguridad externa por SCP bajo demanda.

        Empaqueta DB + logs + config y la sube al servidor remoto configurado en
        la sección `backup` de config.yaml, rotando los backups antiguos.
        """
        from app import backup as backup_mod

        if not cfg.backup.enabled:
            raise HTTPException(status_code=503, detail="Backup externo no habilitado")
        try:
            fname = await asyncio.to_thread(backup_mod.run_backup, cfg)
        except Exception as e:
            logger.error(f"Backup externo falló: {e}")
            raise HTTPException(status_code=500, detail=f"Backup falló: {e}")
        return {"status": "ok", "file": fname}

    @app.get("/api/logs")
    async def get_logs(lines: int = 100):
        """Devuelve las últimas N líneas del fichero de log."""
        log_file = Path(cfg.system.log_file)
        if not log_file.exists():
            return {"lines": []}
        all_lines = log_file.read_text(encoding="utf-8").splitlines()
        return {"lines": all_lines[-lines:]}

    return app
