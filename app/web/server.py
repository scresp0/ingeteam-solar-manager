"""
server.py — interfaz web con FastAPI.

Endpoints:
  GET  /              → dashboard principal
  GET  /api/status    → estado actual del inversor (JSON)
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
        return html.read_text(encoding="utf-8")

    @app.get("/api/status")
    async def status():
        """Lee el estado del inversor + forecast de Solcast."""
        try:
            from app.inverter import read_inverter_state
            state = read_inverter_state(cfg.inverter)

            # Obtener forecast real (mismo que usa el main)
            from app.solcast import get_two_day_forecast
            try:
                forecast_day1, _ = get_two_day_forecast(cfg.solcast, cfg.system.timezone)
                # Convertir a formato que entiende el frontend
                forecast_data = {
                    "hours": list(range(6, 21)),   # 6h a 20h
                    "kwh": [forecast_day1.p50] * 15  # valor p50 repetido (simple)
                }
            except:
                forecast_data = None

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
                "forecast": forecast_data
            }
        except Exception as e:
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