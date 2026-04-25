"""
server.py — interfaz web con FastAPI.

Endpoints:
  GET  /              → dashboard principal
  GET  /api/status    → estado actual del inversor (JSON)
  GET  /api/forecast  → forecast solar por hora (Solcast, con caché)
  POST /api/run/{test} → ejecutar un test
  GET  /api/stream/{job_id} → stream de logs via SSE
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

from app.config import AppConfig
from app.version import VERSION

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
        return html.read_text(encoding="utf-8").replace("{{VERSION}}", VERSION)

    @app.get("/api/version")
    async def version():
        return {"version": VERSION}

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

    @app.get("/api/logs")
    async def get_logs(lines: int = 100):
        """Devuelve las últimas N líneas del fichero de log."""
        log_file = Path(cfg.system.log_file)
        if not log_file.exists():
            return {"lines": []}
        all_lines = log_file.read_text(encoding="utf-8").splitlines()
        return {"lines": all_lines[-lines:]}

    return app