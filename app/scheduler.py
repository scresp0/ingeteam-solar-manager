"""
scheduler.py — cron interno con APScheduler.

Ejecuta el ciclo completo cada noche a tariff.schedule_at (por defecto 23:55)
y, opcionalmente, una re-evaluación por cada hora de tariff.schedule_recheck_at
(p.ej. "19:00, 03:00") que recalcula la decisión y reconfigura el inversor si difiere.

También ejecuta un ciclo inmediatamente al arrancar si RUN_ON_START=true,
útil para pruebas sin esperar a la hora programada.
"""

import logging
import os
import signal
import sys

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

    # Re-evaluaciones: una por cada hora de tariff.schedule_recheck_at. El formato
    # ya lo validó TariffConfig al cargar la config, así que aquí no puede fallar.
    for i, recheck_at in enumerate(cfg.tariff.schedule_recheck_at, start=1):
        rh, rm = recheck_at.split(":")
        scheduler.add_job(
            func=_run_recheck_job,
            trigger=CronTrigger(hour=int(rh), minute=int(rm), timezone=timezone),
            args=[cfg],
            id=f"charge_recheck_{i}",
            name=f"Re-evaluación de la decisión ({recheck_at})",
            misfire_grace_time=300,
            replace_existing=True,
        )
    if cfg.tariff.schedule_recheck_at:
        logger.info(
            f"Re-evaluación de la decisión programada a las "
            f"{', '.join(cfg.tariff.schedule_recheck_at)} ({timezone}) — "
            f"reconfigura el inversor solo si la decisión cambia"
        )

    # Backfill diario de producción histórica: a las 00:30 rellena el día que
    # acaba de terminar, de modo que "ayer" esté disponible en el gráfico durante
    # todo el día sin esperar al ciclo de las 23:45.
    scheduler.add_job(
        func=_run_backfill_job,
        trigger=CronTrigger(hour=0, minute=30, timezone=timezone),
        args=[cfg],
        id="solar_backfill",
        name="Backfill de producción histórica (00:30)",
        misfire_grace_time=3600,
        replace_existing=True,
    )
    logger.info(f"Backfill de producción histórica programado a las 00:30 ({timezone})")

    # Control de corriente máxima de carga: ajusta los amperios al mínimo necesario
    # (menos calor) garantizando llegar al tope a tiempo (valle de red / ventana solar).
    if cfg.charge_current.enabled:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from apscheduler.triggers.interval import IntervalTrigger
        interval = cfg.charge_current.interval_min
        # IntervalTrigger no dispara al arrancar (la 1ª ejecución es un intervalo
        # después del start). Forzamos un primer tick ~20s tras el arranque para
        # que un restart no deje el inversor con el tope anterior hasta ≤interval min.
        first_run = datetime.now(ZoneInfo(timezone)) + timedelta(seconds=20)
        scheduler.add_job(
            func=_run_charge_current_job,
            trigger=IntervalTrigger(minutes=interval, timezone=timezone),
            args=[cfg],
            id="charge_current",
            name=f"Control corriente de carga (cada {interval} min)",
            misfire_grace_time=120,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=first_run,
        )
        logger.info(
            f"Control de corriente de carga programado cada {interval} min "
            f"({timezone}); primer tick a las {first_run:%H:%M:%S}"
        )

    # Copia de seguridad externa por SCP: empaqueta DB + logs + config y la sube
    # a un servidor remoto, rotando los backups antiguos.
    if cfg.backup.enabled:
        try:
            bh, bm = cfg.backup.schedule_at.split(":")
            scheduler.add_job(
                func=_run_backup_job,
                trigger=CronTrigger(hour=int(bh), minute=int(bm), timezone=timezone),
                args=[cfg],
                id="external_backup",
                name=f"Backup externo por SCP ({cfg.backup.schedule_at})",
                misfire_grace_time=3600,
                replace_existing=True,
            )
            logger.info(
                f"Backup externo programado a las {cfg.backup.schedule_at} ({timezone}) "
                f"→ {cfg.backup.user}@{cfg.backup.host}:{cfg.backup.remote_dir}"
            )
        except ValueError:
            logger.error(
                f"backup.schedule_at inválido ({cfg.backup.schedule_at!r}); "
                f"se esperaba formato HH:MM. Backup externo desactivado."
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


def _run_recheck_job(cfg: AppConfig) -> None:
    """Re-evaluación de la decisión — llamado por el scheduler a cada schedule_recheck_at."""
    from app.main import run_recheck
    logger.info("Scheduler: iniciando re-evaluación de la decisión")
    try:
        success = run_recheck(cfg)
        if not success:
            logger.error("Scheduler: la re-evaluación terminó con errores")
    except Exception as e:
        logger.exception(f"Scheduler: error inesperado en la re-evaluación: {e}")


def _run_backfill_job(cfg: AppConfig) -> None:
    """Backfill de huecos de producción histórica — llamado por el scheduler a las 00:30."""
    from app.main import backfill_solar_history
    logger.info("Scheduler: iniciando backfill de producción histórica")
    try:
        backfill_solar_history(cfg)
    except Exception as e:
        logger.exception(f"Scheduler: error inesperado en el backfill: {e}")


def _run_charge_current_job(cfg: AppConfig) -> None:
    """Control de corriente máxima de carga — llamado periódicamente por el scheduler."""
    from app.main import run_charge_current_controller
    try:
        run_charge_current_controller(cfg)
    except Exception as e:
        logger.exception(f"Scheduler: error inesperado en el control de corriente de carga: {e}")


def _run_backup_job(cfg: AppConfig) -> None:
    """Copia de seguridad externa por SCP — llamado por el scheduler a backup.schedule_at."""
    from app.backup import run_backup
    logger.info("Scheduler: iniciando copia de seguridad externa")
    try:
        run_backup(cfg)
    except Exception as e:
        logger.exception(f"Scheduler: error inesperado en la copia de seguridad externa: {e}")
