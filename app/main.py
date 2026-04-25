"""
main.py — punto de entrada del contenedor.

Flujo completo:
  1. Cargar configuración
  2. Obtener previsión solar de Solcast (día 1 y día 2)
  3. Leer SOC actual del inversor vía MODBUS
  4. decide_charge  → ¿cargar batería esta noche?
  5. decide_discharge → ¿bloquear descarga mañana?
  6. Configurar inversor (6.3.1 carga + 6.3.2 descarga)
  7. Leer stats del día anterior y guardar en InfluxDB
  8. Enviar email de notificación
"""

import logging
import sys
from pathlib import Path

from app.version import VERSION
from app.config import load_config, AppConfig
from app.solcast import get_two_day_forecast, SolcastError
from app.inverter import read_inverter_state, InverterError
from app.decision import (
    DecisionInput, decide_charge, decide_discharge,
    charge_summary, discharge_summary, SolarForecast
)
from app.automation import set_charge_schedule, set_discharge_schedule, AutomationError
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

    notifier = CycleEmailNotifier(cfg.system.email)
    notifier.attach()

    logger.info("=== Iniciando ciclo de gestión de carga ===")
    if cfg.system.dry_run:
        logger.info("Modo DRY RUN activo — no se modificará el inversor")

    # 1. Previsión solar — día 1 y día 2
    if cfg.system.dry_run:
        forecast_day1 = SolarForecast(p10=10.0, p50=20.0, p90=30.0)
        forecast_day2 = SolarForecast(p10=8.0, p50=18.0, p90=28.0)
        logger.info("DRY RUN: previsión ficticia día1=10/20/30 kWh, día2=8/18/28 kWh")
    else:
        try:
            logger.info("Obteniendo previsión solar de Solcast...")
            forecast_day1, forecast_day2 = get_two_day_forecast(cfg.solcast, cfg.system.timezone)
        except SolcastError as e:
            logger.error(f"Error al obtener previsión solar: {e}")
            logger.warning("Usando previsión conservadora de 0 kWh como fallback")
            forecast_day1 = SolarForecast(p10=0.0, p50=0.0, p90=0.0)
            forecast_day2 = SolarForecast(p10=0.0, p50=0.0, p90=0.0)

    # 2. Estado actual del inversor vía MODBUS
    state = None
    soc_actual = 50.0
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
        logger.error(
            f"No se pudo conectar al inversor en {host}:{cfg.inverter.modbus_port} — "
            f"comprueba que está encendido y accesible. Detalle: {e}"
        )
        logger.warning("Usando SOC=50% como fallback conservador")

    # min_soc: del inversor si disponible, si no del config
    min_soc = cfg.charging.min_soc_pct
    if state is not None and state.min_soc_pct > 0:
        min_soc = state.min_soc_pct
        logger.info(f"SOC mínimo leído del inversor: {min_soc}% (config: {cfg.charging.min_soc_pct}%)")

    # 3. Construir input compartido por ambas funciones de decisión
    inp = DecisionInput(
        forecast_day1=forecast_day1,
        forecast_day2=forecast_day2,
        soc_actual_pct=soc_actual,
        battery_capacity_kwh=cfg.installation.battery_capacity_kwh,
        daily_consumption_kwh=cfg.installation.average_daily_consumption_kwh,
        night_consumption_kwh=cfg.charging.night_consumption_kwh,
        risk_factor=cfg.charging.risk_factor,
        min_soc_pct=min_soc,
        max_soc_pct=cfg.charging.max_soc_pct,
        safety_margin_kwh=cfg.charging.safety_margin_kwh,
        weekend_days=cfg.tariff.weekend_days,
        holidays=cfg.tariff.holidays,
    )

    # 4. Decisiones independientes
    cutoff    = cfg.tariff.night_cutoff_hour

    charge    = decide_charge(inp, dry_run=cfg.system.dry_run,
                          night_cutoff_hour=cutoff)
    discharge = decide_discharge(inp, night_cutoff_hour=cutoff)

    logger.info("\n" + charge_summary(inp, charge, night_cutoff_hour=cutoff))
    logger.info("\n" + discharge_summary(inp, discharge, night_cutoff_hour=cutoff))

    # 5. Configurar inversor — 6.3.1 carga
    try:
        logger.info("Programando carga en el inversor (6.3.1)...")
        set_charge_schedule(
            cfg=cfg.inverter,
            charge_needed=charge.charge_needed,
            target_soc_pct=charge.target_soc_pct,
            dry_run=cfg.system.dry_run,
        )
        logger.info(
            f"{'[DRY RUN] ' if cfg.system.dry_run else ''}"
            f"6.3.1 configurado: SOC objetivo = {charge.target_soc_pct}%"
        )
    except AutomationError as e:
        logger.error(f"Error al configurar carga (6.3.1): {e}")
        notifier.send(success=False)
        return False

    # 6. Configurar inversor — 6.3.2 descarga
    try:
        logger.info("Programando descarga en el inversor (6.3.2)...")
        set_discharge_schedule(
            cfg=cfg.inverter,
            discharge_blocked=discharge.discharge_blocked,
            dry_run=cfg.system.dry_run,
        )
        logger.info(
            f"{'[DRY RUN] ' if cfg.system.dry_run else ''}"
            f"6.3.2 configurado: discharge_blocked={discharge.discharge_blocked}"
        )
    except AutomationError as e:
        logger.error(f"Error al configurar descarga (6.3.2): {e}")
        notifier.send(success=False)
        return False

    # 7. Stats del día anterior + InfluxDB
    try:
        stats = get_yesterday_stats(cfg.inverter)
        write_daily_stats(cfg.influxdb, stats)
    except (LoggerReaderError, StorageError) as e:
        logger.warning(f"No se pudieron guardar stats diarias: {e}")

    try:
        write_cycle(
            cfg=cfg.influxdb,
            inp=inp,
            result=charge,
            state=state,
            solcast_error=(forecast_day1.p10 == 0.0 and forecast_day1.p50 == 0.0),
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
    import socket
    hostname = socket.gethostname()
    logger.info(f"solar-manager v{VERSION} arrancando en {hostname} (dry_run={cfg.system.dry_run})")

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

    from app.scheduler import start_scheduler
    start_scheduler(cfg)


if __name__ == "__main__":
    main()
