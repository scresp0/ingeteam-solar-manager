"""
scheduler.py — cron interno con APScheduler.

Ejecuta el ciclo completo cada noche a la hora configurada en
tariff.schedule_at (por defecto 23:30).

También ejecuta un ciclo inmediatamente al arrancar si RUN_ON_START=true,
útil para pruebas sin esperar a la hora programada.
"""

import logging
import os
import signal
import sys
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import AppConfig

logger = logging.getLogger(__name__)


def start_scheduler(cfg: AppConfig) -> None:
    """
    Arranca el scheduler bloqueante.
    El proceso vive indefinidamente hasta recibir SIGTERM o SIGINT.
    """
    schedule_at = cfg.tariff.schedule_at   # "HH:MM"
    hour, minute = schedule_at.split(":")
    timezone = cfg.system.timezone

    scheduler = BlockingScheduler(timezone=timezone)

    scheduler.add_job(
        func=_run_job,
        trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=timezone),
        args=[cfg],
        id="charge_schedule",
        name=f"Programación de carga nocturna ({schedule_at})",
        misfire_grace_time=300,   # tolera hasta 5 min de retraso
        replace_existing=True,
    )

    logger.info(
        f"Scheduler iniciado — próxima ejecución programada a las {schedule_at} "
        f"({timezone})"
    )

    # Ejecutar inmediatamente si se pide (útil para pruebas)
    if os.environ.get("RUN_ON_START", "").lower() in ("true", "1", "yes"):
        logger.info("RUN_ON_START activo — ejecutando ciclo ahora")
        _run_job(cfg)

    # Manejar señales de parada limpia
    def _shutdown(signum, frame):
        logger.info("Señal de parada recibida, cerrando scheduler...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler detenido")


def _run_job(cfg: AppConfig) -> None:
    """Ejecuta un ciclo completo — llamado por el scheduler."""
    from app.main import run
    logger.info("Scheduler: iniciando ciclo programado")
    try:
        success = run(cfg)
        if not success:
            logger.error("Scheduler: el ciclo terminó con errores")
    except Exception as e:
        logger.exception(f"Scheduler: error inesperado en el ciclo: {e}")
