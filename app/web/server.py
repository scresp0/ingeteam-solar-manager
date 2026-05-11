"""
server.py — interfaz web con FastAPI.

Endpoints:
  GET  /              → dashboard principal
  GET  /api/status    → estado actual del inversor (JSON)
  GET  /api/forecast  → forecast solar por hora (Solcast, con caché)
  POST /api/run/{test} → ejecutar un test
  GET  /api/stream/{job_id} → stream de logs via SSE
  GET  /api/solar_history → historial forecast vs real (día/semana/mes)
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
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import os
import socket

from app.config import AppConfig
from app.version import VERSION

_HOSTNAME = os.environ.get("HOST_HOSTNAME") or socket.gethostname()

logger = logging.getLogger(__name__)

app = FastAPI(title="solar-manager", docs_url=None, redoc_url=None)

# Buffer de jobs en ejecución: job_id → deque de líneas de log
_jobs: dict[str, deque] = {}
_job_status: dict[str, str] = {}  # running | ok | error


def create_app(cfg: AppConfig) -> FastAPI:
    """Crea y configura la aplicación FastAPI."""

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

            return {
                "ok": True,
                "date": tomorrow.isoformat(),
                "hours":  hours_out,
                "p10_kw": p10_out,
                "p50_kw": p50_out,
                "p90_kw": p90_out,
                "total_p10_kwh": total_p10,
                "total_p50_kwh": total_p50,
                "total_p90_kwh": total_p90,
                "cached_at": cached_at,
                "intervals": used_intervals,
            }

        except Exception as e:
            logger.exception("Error en /api/forecast")
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    @app.post("/api/run/{test_name}")
    async def run_test(test_name: str):
        """Lanza un test en background y devuelve un job_id para seguir el stream."""
        allowed = {
            "inverter":   "app.test_inverter",
            "solcast":    "app.test_solcast",
            "decision":   "app.test_decision",
            "config":     "app.test_config",
            "automation": "app.test_automation",
            "main":       "app.test_main",
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

    @app.post("/api/cycle")
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
                min_days=cfg.charging.night_consumption_min_days,
            )
            rf = get_dynamic_risk_factor(
                cfg.influxdb,
                window_days=cfg.charging.risk_factor_window_days,
                min_days=cfg.charging.risk_factor_min_days,
            )
            bias = get_dynamic_solar_bias(
                cfg.influxdb,
                window_days=cfg.charging.solar_bias_window_days,
                min_days=cfg.charging.solar_bias_min_days,
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
            "night_min_days": cfg.charging.night_consumption_min_days,
            "night_window_days": cfg.charging.night_consumption_window_days,
            "night_valid_days": night_count,
            "risk_factor": rf if rf is not None else cfg.charging.risk_factor,
            "risk_dynamic": rf is not None,
            "risk_config": cfg.charging.risk_factor,
            "risk_min_days": cfg.charging.risk_factor_min_days,
            "risk_window_days": cfg.charging.risk_factor_window_days,
            "risk_valid_days": rf_count,
            "solar_bias_factor": bias if bias is not None else cfg.charging.solar_bias_factor,
            "solar_bias_dynamic": bias is not None,
            "solar_bias_config": cfg.charging.solar_bias_factor,
            "solar_bias_min_days": cfg.charging.solar_bias_min_days,
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
        current_house_w = round(last.get("PacGrid", 0))

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
        return result

    @app.get("/api/logs")
    async def get_logs(lines: int = 100):
        """Devuelve las últimas N líneas del fichero de log."""
        log_file = Path(cfg.system.log_file)
        if not log_file.exists():
            return {"lines": []}
        all_lines = log_file.read_text(encoding="utf-8").splitlines()
        return {"lines": all_lines[-lines:]}

    return app
