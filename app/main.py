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

    # 2. Estado actual del inversor
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
        logger.error(f"Error al leer el inversor: {e}")
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

    logger.info("=== Ciclo completado correctamente ===")
    notifier.send(success=True)
    return True


def main() -> None:
    """Punto de entrada — carga config y arranca el scheduler."""
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR de configuración: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(cfg)
    logger = logging.getLogger(__name__)
    logger.info(f"solar-manager arrancando (dry_run={cfg.system.dry_run})")

    # Importar aquí para que el logging ya esté configurado
    from app.scheduler import start_scheduler
    start_scheduler(cfg)


if __name__ == "__main__":
    main()
