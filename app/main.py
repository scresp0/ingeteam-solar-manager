"""
main.py — punto de entrada del contenedor.

Flujo completo:
  1. Cargar configuración
  2. Obtener previsión solar de Solcast (p10/p50/p90 para mañana)
  3. Leer SOC actual del inversor vía MODBUS
  4. Calcular nivel de carga óptimo (decision.py)
  5. Programar la carga en la web del inversor (automation.py)
  6. Registrar todo en el log
"""

import logging
import sys
from pathlib import Path

from app.config import load_config, AppConfig
from app.solcast import get_tomorrow_forecast, SolcastError
from app.inverter import read_inverter_state, InverterError
from app.decision import calculate_charge_target, decision_summary, DecisionInput
from app.automation import set_charge_schedule, AutomationError
from app.notifier import CycleEmailNotifier
from app.logger_reader import get_yesterday_stats, LoggerReaderError
from app.storage import write_cycle, write_daily_stats, StorageError


def setup_logging(cfg: AppConfig) -> None:
    """Configura el sistema de logging a fichero y consola."""
    log_file = Path(cfg.system.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    level = getattr(logging, cfg.system.log_level, logging.INFO)

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def run(cfg: AppConfig) -> bool:
    """
    Ejecuta el ciclo completo de gestión de carga.

    Returns:
        True si todo fue bien, False si hubo algún error no fatal.
    """
    logger = logging.getLogger(__name__)

    # Iniciar notifier de email — captura logs desde este momento
    notifier = CycleEmailNotifier(cfg.system.email)
    notifier.attach()

    logger.info("=== Iniciando ciclo de gestión de carga ===")

    if cfg.system.dry_run:
        logger.info("Modo DRY RUN activo — no se modificará el inversor")

    # 1. Previsión solar
    # En dry_run usamos valores ficticios para no consumir cuota de Solcast
    if cfg.system.dry_run:
        from app.decision import SolarForecast
        forecast = SolarForecast(p10=10.0, p50=20.0, p90=30.0)
        logger.info("DRY RUN: usando previsión ficticia p10=10, p50=20, p90=30 kWh")
    else:
        try:
            logger.info("Obteniendo previsión solar de Solcast...")
            forecast = get_tomorrow_forecast(cfg.solcast, cfg.system.timezone)
            logger.info(
                f"Previsión mañana: p10={forecast.p10} kWh, "
                f"p50={forecast.p50} kWh, p90={forecast.p90} kWh"
            )
        except SolcastError as e:
            logger.error(f"Error al obtener previsión solar: {e}")
            logger.warning("Usando previsión conservadora de 0 kWh como fallback")
            from app.decision import SolarForecast
            forecast = SolarForecast(p10=0.0, p50=0.0, p90=0.0)

    # 2. Estado actual del inversor vía MODBUS (siempre, dry_run o no)
    state = None
    try:
        logger.info("Leyendo estado del inversor vía MODBUS...")
        state = read_inverter_state(cfg.inverter)
        logger.info(
            f"Inversor: {state.inverter_status} | "
            f"Batería: {state.battery_status} | "
            f"SOC: {state.soc_pct}%"
        )
        soc_actual = state.soc_pct
    except InverterError as e:
        host = cfg.inverter.get_modbus_host()
        port = cfg.inverter.modbus_port
        logger.error(
            f"No se pudo conectar al inversor en {host}:{port} — "
            f"comprueba que el inversor está encendido y accesible en la red. "
            f"Detalle: {e}"
        )
        if not cfg.system.dry_run:
            logger.warning("Usando SOC=50% como fallback conservador")
        soc_actual = 50.0

    # 3. Calcular nivel de carga óptimo
    inp = DecisionInput(
        forecast=forecast,
        soc_actual_pct=soc_actual,
        battery_capacity_kwh=cfg.installation.battery_capacity_kwh,
        daily_consumption_kwh=cfg.installation.average_daily_consumption_kwh,
        night_consumption_kwh=cfg.charging.night_consumption_kwh,
        risk_factor=cfg.charging.risk_factor,
        min_soc_pct=cfg.charging.min_soc_pct,
        max_soc_pct=cfg.charging.max_soc_pct,
        safety_margin_kwh=cfg.charging.safety_margin_kwh,
    )
    result = calculate_charge_target(inp, dry_run=cfg.system.dry_run)
    logger.info("\n" + decision_summary(inp, result))

    # 4. Programar la carga en el inversor
    try:
        logger.info("Programando carga en el inversor...")
        set_charge_schedule(
            cfg=cfg.inverter,
            charge_needed=result.charge_needed,
            target_soc_pct=result.target_soc_pct,
            dry_run=cfg.system.dry_run,
        )
        logger.info(
            f"{'[DRY RUN] ' if cfg.system.dry_run else ''}"
            f"Carga programada correctamente: SOC objetivo = {result.target_soc_pct}%"
        )
    except AutomationError as e:
        logger.error(f"Error al programar la carga en el inversor: {e}")
        notifier.send(success=False)
        return False

    # 5. Leer stats del día anterior y guardar en InfluxDB
    try:
        stats = get_yesterday_stats(cfg.inverter)
        write_daily_stats(cfg.influxdb, stats)
    except (LoggerReaderError, StorageError) as e:
        logger.warning(f"No se pudieron guardar stats diarias: {e}")

    # 6. Guardar decisión del ciclo en InfluxDB
    try:
        write_cycle(
            cfg=cfg.influxdb,
            inp=inp,
            result=result,
            state=state if isinstance(state, object) else None,
            solcast_error=(forecast.p10 == 0.0 and forecast.p50 == 0.0),
            automation_ok=True,
        )
    except StorageError as e:
        logger.warning(f"No se pudo guardar ciclo en InfluxDB: {e}")

    logger.info("=== Ciclo completado correctamente ===")
    notifier.send(success=True)
    return True


def main() -> None:
    """Punto de entrada — carga config, arranca el scheduler y la interfaz web."""
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR de configuración: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(cfg)
    logger = logging.getLogger(__name__)
    logger.info(f"solar-manager arrancando (dry_run={cfg.system.dry_run})")

    # Arrancar interfaz web en thread separado
    if cfg.system.web_enabled:
        import threading
        import uvicorn
        from app.web.server import create_app
        web_app = create_app(cfg)
        def _run_web():
            uvicorn.run(web_app, host="0.0.0.0", port=cfg.system.web_port,
                        log_level="warning")
        t = threading.Thread(target=_run_web, daemon=True)
        t.start()
        logger.info(f"Interfaz web disponible en http://0.0.0.0:{cfg.system.web_port}")

    # Arrancar scheduler (bloqueante)
    from app.scheduler import start_scheduler
    start_scheduler(cfg)


if __name__ == "__main__":
    main()
