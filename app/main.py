"""
main.py — punto de entrada del contenedor.

Flujo completo (run):
  1. Cargar configuración
  2. Obtener previsión solar de Solcast (día 1 y día 2)
  3. Leer SOC actual del inversor vía MODBUS
  4. decide_charge  → ¿cargar batería esta noche?
  5. decide_discharge → ¿bloquear descarga mañana?
  6. Configurar inversor (6.3.1 carga + 6.3.2 descarga)
  7. Leer stats del día anterior y guardar en InfluxDB
  8. Enviar email de notificación

Re-evaluación nocturna (run_recheck), ejecutada a tariff.schedule_recheck_at:
  Repite pasos 2-6 con el SOC actualizado tras el consumo entre schedule_at y
  esa hora. Si la decisión cambia respecto al estado actual del inversor,
  reescribe y notifica por email; si no, sale sin tocar nada ni escribir
  ciclo_carga (rompería el JOIN con stats_diarias).
"""

import logging
import sys
from pathlib import Path

from app.version import VERSION
from app.config import load_config, AppConfig
from app.solcast import get_two_day_forecast, get_day1_intervals, get_today_intervals, SolcastError
from app.inverter import read_inverter_state, InverterError, InverterState
from app.decision import (
    DecisionInput, decide_charge, decide_discharge,
    charge_summary, discharge_summary,
    charge_oneliner, discharge_oneliner,
    SolarForecast, ChargeDecision, DischargeDecision,
)
from app.automation import (
    set_charge_schedule, set_discharge_schedule, AutomationError,
    read_inverter_schedule, ScheduleState, set_charge_current, _load_schedule_state,
    read_firmware_version, configure_active_profile,
)
from app.notifier import CycleEmailNotifier
from app.logger_reader import get_yesterday_stats, get_daily_stats, LoggerReaderError
from app.storage import (
    write_cycle, write_daily_stats, write_half_hour_solar, write_half_hour_forecast,
    write_charge_current,
    get_avg_night_consumption, get_avg_post_valley_consumption,
    get_dynamic_risk_factor, get_dynamic_solar_bias,
    get_last_real_solar_date, get_production_window_end_hour, StorageError,
)

# Máximo de días que el backfill rellena de una vez: cubre la vista "mes" del
# gráfico y evita martillear el datalogger en un arranque con InfluxDB casi vacío.
MAX_BACKFILL_DAYS = 35


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


def _collect_decision_inputs(
    cfg: AppConfig, logger: logging.Logger,
) -> tuple[DecisionInput, InverterState | None, list[dict]]:
    """
    Recoge previsión Solcast, lee MODBUS y parámetros dinámicos.
    Devuelve el DecisionInput listo para decide_charge/decide_discharge,
    junto con el estado del inversor (o None si falló) y los intervalos
    del día 1 de Solcast (para write_half_hour_forecast).
    """
    # 1. Previsión solar
    intervals_day1: list[dict] = []
    if cfg.system.dry_run:
        forecast_day1 = SolarForecast(p10=10.0, p50=20.0, p90=30.0)
        forecast_day2 = SolarForecast(p10=8.0, p50=18.0, p90=28.0)
        logger.info("DRY RUN: previsión ficticia día1=10/20/30 kWh, día2=8/18/28 kWh")
    else:
        try:
            logger.info("Obteniendo previsión solar de Solcast...")
            cutoff = cfg.tariff.night_cutoff_hour
            forecast_day1, forecast_day2 = get_two_day_forecast(cfg.solcast, cfg.system.timezone, cutoff)
            intervals_day1 = get_day1_intervals(cfg.solcast, cfg.system.timezone, cutoff)
        except SolcastError as e:
            logger.error(f"Error al obtener previsión solar: {e}")
            logger.warning("Usando previsión conservadora de 0 kWh como fallback")
            forecast_day1 = SolarForecast(p10=0.0, p50=0.0, p90=0.0)
            forecast_day2 = SolarForecast(p10=0.0, p50=0.0, p90=0.0)

    # 2. Estado actual del inversor vía MODBUS
    state: InverterState | None = None
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

    # 3. Consumo nocturno dinámico
    night_consumption_kwh = cfg.charging.night_consumption_kwh
    avg_night = get_avg_night_consumption(
        cfg.influxdb,
        window_days=cfg.charging.night_consumption_window_days,
        min_days=cfg.charging.night_consumption_min_days_in_window,
    )
    if avg_night is not None:
        logger.info(
            f"Consumo nocturno dinámico: {avg_night} kWh "
            f"(media {cfg.charging.night_consumption_window_days}d · "
            f"fallback config: {cfg.charging.night_consumption_kwh} kWh)"
        )
        night_consumption_kwh = avg_night
    else:
        logger.info(
            f"Consumo nocturno: {night_consumption_kwh} kWh "
            f"(config — menos de {cfg.charging.night_consumption_min_days_in_window} días en InfluxDB)"
        )

    # 4. Risk factor dinámico
    risk_factor = cfg.charging.risk_factor
    dynamic_rf = get_dynamic_risk_factor(
        cfg.influxdb,
        window_days=cfg.charging.risk_factor_window_days,
        min_days=cfg.charging.risk_factor_min_days_in_window,
    )
    if dynamic_rf is not None:
        logger.info(
            f"Risk factor dinámico: {dynamic_rf} "
            f"(media {cfg.charging.risk_factor_window_days}d · "
            f"fallback config: {cfg.charging.risk_factor})"
        )
        risk_factor = dynamic_rf
    else:
        logger.info(
            f"Risk factor: {risk_factor} "
            f"(config — menos de {cfg.charging.risk_factor_min_days_in_window} días en InfluxDB)"
        )

    # 4b. Factor de calibración del forecast Solcast
    solar_bias = cfg.charging.solar_bias_factor
    dynamic_bias = get_dynamic_solar_bias(
        cfg.influxdb,
        window_days=cfg.charging.solar_bias_window_days,
        min_days=cfg.charging.solar_bias_min_days_in_window,
    )
    if dynamic_bias is not None:
        logger.info(
            f"Factor de calibración solar dinámico: {dynamic_bias} "
            f"(media {cfg.charging.solar_bias_window_days}d · "
            f"fallback config: {cfg.charging.solar_bias_factor})"
        )
        solar_bias = dynamic_bias
    else:
        logger.info(
            f"Factor de calibración solar: {solar_bias} "
            f"(config — menos de {cfg.charging.solar_bias_min_days_in_window} días en InfluxDB)"
        )

    # 5. Construir input compartido por ambas decisiones
    inp = DecisionInput(
        forecast_day1=forecast_day1,
        forecast_day2=forecast_day2,
        soc_actual_pct=soc_actual,
        battery_capacity_kwh=cfg.installation.battery_capacity_kwh,
        daily_consumption_kwh=cfg.installation.average_daily_consumption_kwh,
        night_consumption_kwh=night_consumption_kwh,
        risk_factor=risk_factor,
        solar_bias_factor=solar_bias,
        min_soc_pct=min_soc,
        max_soc_pct=cfg.charging.max_soc_pct,
        safety_margin_kwh=cfg.charging.safety_margin_kwh,
        weekend_days=cfg.tariff.weekend_days,
        holidays=cfg.tariff.holidays,
    )

    return inp, state, intervals_day1


def _read_schedule_before(cfg: AppConfig, logger: logging.Logger) -> ScheduleState | None:
    """Lee config actual del inversor (6.3.1/6.3.2) y loguea estado ANTES."""
    schedule_before: ScheduleState | None = None
    if not cfg.system.dry_run:
        try:
            schedule_before = read_inverter_schedule(cfg.inverter)
        except Exception as e:
            logger.warning(f"Error al leer programación del inversor: {e}")

    if schedule_before is not None:
        logger.info(
            f"[ANTES] Carga (6.3.1): {schedule_before.charge_str()} | "
            f"Descarga (6.3.2): {schedule_before.discharge_str()}"
        )
    else:
        logger.warning(
            "[ANTES] Configuración del inversor no disponible — "
            "se aplicará igualmente"
        )
    return schedule_before


def _needs_update(
    schedule_before: ScheduleState | None,
    charge: ChargeDecision, discharge: DischargeDecision,
) -> tuple[bool, bool, int]:
    """Determina si hay que reconfigurar carga/descarga.
    Devuelve (charge_needs_update, discharge_needs_update, charge_soc_target)."""
    charge_soc_target = int(round(charge.target_soc_pct)) if charge.charge_needed else 0
    charge_needs_update = (
        schedule_before is None
        or schedule_before.charge_active != charge.charge_needed
        or (charge.charge_needed and schedule_before.charge_soc_pct != charge_soc_target)
    )
    discharge_needs_update = (
        schedule_before is None
        or not schedule_before.discharge_recognized   # config 6.3.2 no canónica → reescribir
        or schedule_before.discharge_blocked != discharge.discharge_blocked
    )
    return charge_needs_update, discharge_needs_update, charge_soc_target


def _apply_inverter_decisions(
    cfg: AppConfig, logger: logging.Logger,
    charge: ChargeDecision, discharge: DischargeDecision,
    charge_needs_update: bool, discharge_needs_update: bool,
    charge_soc_target: int,
    *, force_all: bool = False,
) -> bool:
    """Aplica carga (6.3.1) y descarga (6.3.2) al inversor. En dry_run o si
    force_all=True escribe siempre; si no, solo cuando *_needs_update.
    Loguea estado DESPUÉS. Devuelve False si alguna escritura falla."""
    write_charge = force_all or cfg.system.dry_run or charge_needs_update
    write_discharge = force_all or cfg.system.dry_run or discharge_needs_update

    if write_charge:
        try:
            logger.info("Programando carga en el inversor (6.3.1)...")
            set_charge_schedule(
                cfg=cfg.inverter,
                charge_needed=charge.charge_needed,
                target_soc_pct=charge.target_soc_pct,
                dry_run=cfg.system.dry_run,
            )
        except AutomationError as e:
            logger.error(f"Error al configurar carga (6.3.1): {e}")
            return False
    else:
        logger.info("6.3.1 sin cambios — configuración ya correcta en el inversor")

    if write_discharge:
        try:
            logger.info("Programando descarga en el inversor (6.3.2)...")
            set_discharge_schedule(
                cfg=cfg.inverter,
                discharge_blocked=discharge.discharge_blocked,
                dry_run=cfg.system.dry_run,
            )
        except AutomationError as e:
            logger.error(f"Error al configurar descarga (6.3.2): {e}")
            return False
    else:
        logger.info("6.3.2 sin cambios — configuración ya correcta en el inversor")

    after_charge_str = (
        f"ACTIVA (SOC {charge_soc_target}%)" if charge.charge_needed else "DESACTIVADA"
    )
    after_discharge_str = "BLOQUEADA" if discharge.discharge_blocked else "LIBRE"
    dry_prefix = "[DRY RUN] " if cfg.system.dry_run else ""
    logger.info(
        f"[DESPUÉS] {dry_prefix}Carga (6.3.1): {after_charge_str} | "
        f"Descarga (6.3.2): {after_discharge_str}"
    )
    return True


def run(cfg: AppConfig) -> bool:
    """
    Ciclo completo nocturno (schedule_at, p.ej. 23:55).
    Recoge inputs, decide, configura inversor, persiste stats y ciclo en
    InfluxDB y envía email siempre.

    Returns:
        True si todo fue bien, False si hubo algún error no fatal.
    """
    logger = logging.getLogger(__name__)

    notifier = CycleEmailNotifier(cfg.system.email)
    notifier.attach()

    logger.info("=== Iniciando ciclo de gestión de carga ===")
    if cfg.system.dry_run:
        logger.info("Modo DRY RUN activo — no se modificará el inversor")

    inp, state, intervals_day1 = _collect_decision_inputs(cfg, logger)

    cutoff = cfg.tariff.night_cutoff_hour
    charge    = decide_charge(inp, dry_run=cfg.system.dry_run, night_cutoff_hour=cutoff)
    discharge = decide_discharge(inp, night_cutoff_hour=cutoff)

    logger.info(charge_oneliner(inp, charge, night_cutoff_hour=cutoff))
    logger.debug("\n" + charge_summary(inp, charge, night_cutoff_hour=cutoff))
    logger.info(discharge_oneliner(inp, discharge, night_cutoff_hour=cutoff))
    logger.debug("\n" + discharge_summary(inp, discharge, night_cutoff_hour=cutoff))

    schedule_before = _read_schedule_before(cfg, logger)
    charge_upd, discharge_upd, charge_soc_target = _needs_update(schedule_before, charge, discharge)

    if not _apply_inverter_decisions(
        cfg, logger, charge, discharge, charge_upd, discharge_upd, charge_soc_target,
    ):
        notifier.send(success=False)
        return False

    # Stats del día anterior + InfluxDB
    try:
        stats = get_yesterday_stats(cfg.inverter)
        write_daily_stats(cfg.influxdb, stats)
        write_half_hour_solar(cfg.influxdb, stats)
    except (LoggerReaderError, StorageError) as e:
        logger.warning(f"No se pudieron guardar stats diarias: {e}")

    try:
        write_cycle(
            cfg=cfg.influxdb,
            inp=inp,
            result=charge,
            state=state,
            solcast_error=(inp.forecast_day1.p10 == 0.0 and inp.forecast_day1.p50 == 0.0),
            automation_ok=True,
        )
        write_half_hour_forecast(cfg.influxdb, intervals_day1, cfg.system.timezone)
    except StorageError as e:
        logger.warning(f"No se pudo guardar ciclo en InfluxDB: {e}")

    logger.info("=== Ciclo completado correctamente ===")
    notifier.send(success=True)
    return True


def run_recheck(cfg: AppConfig) -> bool:
    """
    Re-evaluación de la decisión a media madrugada (schedule_recheck_at, p.ej. 03:00).

    Recoge inputs frescos (SOC actualizado tras el consumo entre schedule_at y
    esta hora), recalcula decide_charge/decide_discharge y, si la decisión
    difiere del estado actual del inversor, la aplica y notifica por email.
    Si no difiere, sale silenciosamente.

    NO escribe ciclo_carga ni stats en InfluxDB: el ciclo de las 23:55 ya lo hizo
    y un segundo ciclo_carga del mismo día tendría timestamp en madrugada UTC,
    cuyo JOIN con stats_diarias (forecast_date = UTC.date()+1) apuntaría al día
    equivocado.

    Returns:
        True si todo fue bien (con o sin cambios aplicados), False si hubo error.
    """
    logger = logging.getLogger(__name__)

    notifier = CycleEmailNotifier(cfg.system.email)
    notifier.attach()

    logger.info("=== Re-evaluación nocturna de la decisión de carga ===")
    if cfg.system.dry_run:
        logger.info("Modo DRY RUN activo — no se modificará el inversor")

    inp, _state, _intervals = _collect_decision_inputs(cfg, logger)

    cutoff = cfg.tariff.night_cutoff_hour
    charge    = decide_charge(inp, dry_run=cfg.system.dry_run, night_cutoff_hour=cutoff)
    discharge = decide_discharge(inp, night_cutoff_hour=cutoff)

    logger.info(charge_oneliner(inp, charge, night_cutoff_hour=cutoff))
    logger.debug("\n" + charge_summary(inp, charge, night_cutoff_hour=cutoff))
    logger.info(discharge_oneliner(inp, discharge, night_cutoff_hour=cutoff))
    logger.debug("\n" + discharge_summary(inp, discharge, night_cutoff_hour=cutoff))

    schedule_before = _read_schedule_before(cfg, logger)
    charge_upd, discharge_upd, charge_soc_target = _needs_update(schedule_before, charge, discharge)

    # Sin cambios y no dry_run → salir sin tocar el inversor ni notificar
    if not (charge_upd or discharge_upd or cfg.system.dry_run):
        logger.info("Decisión sin cambios respecto al estado actual del inversor — no se reconfigura ni se notifica")
        notifier.discard()
        return True

    if not _apply_inverter_decisions(
        cfg, logger, charge, discharge, charge_upd, discharge_upd, charge_soc_target,
    ):
        notifier.send(success=False)
        return False

    logger.info("=== Re-evaluación completada — decisión actualizada ===")
    notifier.send(success=True)
    return True


def backfill_solar_history(cfg: AppConfig) -> None:
    """
    Rellena en InfluxDB los días de producción real que falten entre el último
    almacenado y ayer (incluido).

    El ciclo nocturno solo escribe "ayer" relativo a su hora de ejecución (23:45),
    de modo que el día anterior al actual queda sin datos reales hasta esa noche.
    Este backfill (al arrancar y en el job diario de las 00:30) cierra ese hueco
    leyendo el datalogger para cada día pendiente y escribiendo stats_diarias +
    solar_media_hora. No toca el día en curso (datos incompletos) ni ciclo_carga.
    """
    from datetime import date, timedelta

    if not cfg.influxdb.enabled:
        return

    logger = logging.getLogger(__name__)
    yesterday = date.today() - timedelta(days=1)
    floor_day = yesterday - timedelta(days=MAX_BACKFILL_DAYS - 1)

    last_stored = get_last_real_solar_date(cfg.influxdb)
    start_day = (last_stored + timedelta(days=1)) if last_stored else floor_day
    if start_day < floor_day:   # hueco enorme → no retroceder más allá del límite
        start_day = floor_day

    if start_day > yesterday:
        logger.debug(f"Backfill solar: sin huecos (último real = {last_stored})")
        return

    missing = [start_day + timedelta(days=i) for i in range((yesterday - start_day).days + 1)]
    logger.info(f"Backfill solar: rellenando {len(missing)} día(s) [{missing[0]} → {missing[-1]}]")

    filled = 0
    for day in missing:
        try:
            stats = get_daily_stats(cfg.inverter, day)
            write_daily_stats(cfg.influxdb, stats)
            write_half_hour_solar(cfg.influxdb, stats)
            filled += 1
        except (LoggerReaderError, StorageError) as e:
            logger.warning(f"Backfill solar: no se pudo rellenar {day}: {e}")
        except Exception as e:
            logger.warning(f"Backfill solar: error inesperado en {day}: {e}")

    logger.info(f"Backfill solar: {filled}/{len(missing)} día(s) rellenados")


# ---------------------------------------------------------------------------
# Controlador de corriente máxima de carga (job diurno+nocturno periódico)
# ---------------------------------------------------------------------------

# Ventana del valle nocturno (carga de red), coincide con la programación 6.3.1.
_VALLE_START_HOUR = 0.0
_VALLE_END_HOUR   = 8.0


def _productive_window_end(cfg: AppConfig) -> float:
    """Fallback de la hora local de fin de producción solar (cuando no hay forecast).

    Del perfil real histórico (solar_media_hora): la hora en que la producción media
    acumula `productive_window_pct`% del total diario. Fallback al valor de config.
    """
    try:
        h = get_production_window_end_hour(
            cfg.influxdb,
            pct=cfg.charge_current.productive_window_pct,
            window_days=cfg.charging.solar_bias_window_days,
        )
    except StorageError:
        h = None
    return h if h is not None else cfg.charge_current.productive_window_end_hour


def _remaining_solar_forecast(cfg: AppConfig, now) -> tuple[float | None, float]:
    """Excedente solar NETO que queda HOY para la batería (producción calibrada
    menos el consumo de la casa) y la hora local de fin de producción.

    Devuelve (kWh_restantes_neto, hora_fin):
    - forecast OK con producción pendiente → (excedente_neto, hora_última_franja)
    - forecast OK sin producción pendiente → (0.0, 0.0)  → fuerza IDLE
    - forecast no disponible               → (None, fallback_histórico)  → SOLAR usará máx

    Producción por franja (igual que decide_charge): (p10·rf + p50·(1−rf))·bias.
    El rf inclina hacia p10 (pesimista) = margen de seguridad ante menos producción.
    A cada franja se le resta el consumo post-valle prorrateado (tasa plana sobre
    16 h, suelo en 0): así el resultado es el excedente real que entra en batería,
    no la producción bruta. El fin de producción se sigue calculando con la
    producción bruta (una franja produce aunque la casa se la coma toda).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    logger = logging.getLogger(__name__)

    try:
        intervals = get_today_intervals(cfg.solcast, cfg.system.timezone)
    except SolcastError as e:
        logger.warning(f"Controlador corriente: forecast de hoy no disponible — {e}")
        return None, _productive_window_end(cfg)

    try:
        rf = get_dynamic_risk_factor(
            cfg.influxdb, cfg.charging.risk_factor_window_days,
            cfg.charging.risk_factor_min_days_in_window)
    except StorageError:
        rf = None
    rf = cfg.charging.risk_factor if rf is None else rf
    try:
        bias = get_dynamic_solar_bias(
            cfg.influxdb, cfg.charging.solar_bias_window_days,
            cfg.charging.solar_bias_min_days_in_window)
    except StorageError:
        bias = None
    bias = cfg.charging.solar_bias_factor if bias is None else bias

    # Consumo post-valle (08:00–24:00) que la casa restará al sol. Dinámico de
    # InfluxDB con la misma ventana/mínimo que el nocturno (mismo origen
    # stats_diarias → misma disponibilidad de datos); fallback a config
    # (diario − nocturno). Se prorratea a tasa plana sobre las 16 h del tramo y
    # se resta por franja, así solo descuenta el consumo de las horas solares.
    try:
        post_valley = get_avg_post_valley_consumption(
            cfg.influxdb, cfg.charging.night_consumption_window_days,
            cfg.charging.night_consumption_min_days_in_window)
    except StorageError:
        post_valley = None
    if post_valley is None:
        post_valley = max(0.0, cfg.installation.average_daily_consumption_kwh
                          - cfg.charging.night_consumption_kwh)
    house_per_interval = post_valley / 16.0 * 0.5   # kWh de casa en una franja de 30 min

    tz = ZoneInfo(cfg.system.timezone)
    gross = 0.0
    remaining = 0.0
    end_hour = None
    for it in intervals:
        try:
            end_str = it["period_end"].rstrip("Z").split(".")[0] + "+00:00"
            end_local = datetime.fromisoformat(end_str).astimezone(tz)
        except (KeyError, ValueError):
            continue
        if end_local <= now:
            continue
        eff = (it.get("pv_estimate10", 0.0) * rf
               + it.get("pv_estimate", 0.0) * (1.0 - rf)) * bias * 0.5   # kWh de la franja
        if eff > 0.02:
            gross += eff
            remaining += max(0.0, eff - house_per_interval)   # excedente neto a batería
            end_hour = end_local.hour + end_local.minute / 60.0

    if end_hour is None:
        return 0.0, 0.0   # forecast OK pero ya no queda producción → IDLE
    logger.debug(
        f"Excedente solar: bruto {gross:.2f} kWh − consumo casa "
        f"{gross - remaining:.2f} kWh → neto {remaining:.2f} kWh "
        f"(post-valle {post_valley:.2f} kWh/16h)"
    )
    return round(remaining, 2), end_hour


def _compute_target_charge_current(
    cfg: AppConfig, state: InverterState, hour: float,
    schedule_state, current: int,
    remaining_solar_kwh: float | None, solar_end_hour: float,
) -> tuple[int, int, str]:
    """Devuelve (amperios_objetivo, amperios_calculados, modo).

    `amperios_calculados` es la corriente cruda que sale de la fórmula solar/valle
    ANTES de acotarla al `[floor_a, max_a]` de la configuración; `amperios_objetivo`
    es esa misma corriente ya acotada (la que se fija en el inversor). Cuando el
    cálculo cae por debajo del mínimo configurado, `objetivo` = mínimo pero
    `calculado` refleja el valor real que pedía la fórmula. En los modos que no
    calculan corriente (idle / batería llena / máx por solar insuficiente) ambos
    coinciden con el valor devuelto.

    - VALLE (00:00–08:00 con carga de red): corriente mínima para llegar al target
      en lo que queda de valle (sin temperatura — de noche no hay calor).
    - SOLAR (08:00–fin de producción): si la producción solar restante calibrada cubre
      la energía pendiente → corriente mínima para llenar con esa solar; si no (solar
      insuficiente o sin forecast) → máx (66) para llenar seguro. Con
      temp_gate_enabled=True además exige temp > hot_threshold (si la batería está fría
      va a máx); con temp_gate_enabled=False la temperatura no influye.
    - Sin carga (idle / valle sin carga / batería llena) → deja la corriente como está.
    """
    cc = cfg.charge_current
    soc = state.soc_pct
    v = state.battery_voltage_v if state.battery_voltage_v and state.battery_voltage_v > 30 else 50.0
    cap = cfg.installation.battery_capacity_kwh
    max_soc = cfg.charging.max_soc_pct

    def amps_for(energy_kwh: float, hours: float, floor_a: int | None = None) -> tuple[int, int]:
        """(acotado a [floor, max], crudo sin acotar) — el crudo es "calculado"."""
        hours = max(0.25, hours)
        i = (energy_kwh * 1000.0) / (v * hours) * cc.margin
        raw = int(round(i))
        return (max(floor_a if floor_a is not None else cc.floor_a,
                    min(cc.max_a, raw)),
                raw)

    # Fin de la ventana de carga en curso (valle nocturno o ventana solar), o None
    # si ahora mismo no estamos cargando. Sirve para el override de BALANCE.
    charging_valle = (_VALLE_START_HOUR <= hour < _VALLE_END_HOUR
                      and schedule_state is not None and schedule_state.charge_needed)
    charging_solar = _VALLE_END_HOUR <= hour < solar_end_hour
    window_end = (_VALLE_END_HOUR if charging_valle
                  else solar_end_hour if charging_solar else None)

    # BALANCE: cerca del tope, carga lo más suave posible para balancear las celdas
    # del stack, siempre que dé tiempo a llegar a max_soc antes de fin de ventana.
    # Ignora la puerta de temperatura y baja del floor normal (balance_floor_a).
    if (cc.battery_balance and window_end is not None
            and cc.balance_soc_pct <= soc < max_soc):
        energy = (max_soc - soc) / 100.0 * cap
        # 2ª etapa: aún más suave en el último tramo (balance_soc_pct_2, default 99%)
        if soc >= cc.balance_soc_pct_2:
            floor, label = max(1, cc.balance_floor_a // 2), "BALANCE (fino)"
        else:
            floor, label = cc.balance_floor_a, "BALANCE"
        target, raw = amps_for(energy, window_end - hour, floor)
        return target, raw, label

    # VALLE: carga de red 00:00–08:00
    if _VALLE_START_HOUR <= hour < _VALLE_END_HOUR:
        if schedule_state is not None and schedule_state.charge_needed:
            target_soc = schedule_state.target_soc_pct or max_soc
            energy = max(0.0, (target_soc - soc) / 100.0 * cap)
            if energy <= 0:
                return current, current, "VALLE (objetivo alcanzado — sin cambios)"
            target, raw = amps_for(energy, _VALLE_END_HOUR - hour)
            return target, raw, "VALLE"
        return current, current, "IDLE (valle sin carga — sin cambios)"

    # SOLAR: ventana diurna productiva 08:00 – fin de producción del forecast
    if _VALLE_END_HOUR <= hour < solar_end_hour:
        energy = (max_soc - soc) / 100.0 * cap
        if energy <= 0:
            return current, current, "SOLAR (batería llena — sin cambios)"
        if cc.temp_gate_enabled and state.battery_temp_c <= cc.hot_threshold_c:
            return cc.max_a, cc.max_a, f"SOLAR (temp {state.battery_temp_c}≤{cc.hot_threshold_c}ºC → máx)"
        if remaining_solar_kwh is None:
            return cc.max_a, cc.max_a, "SOLAR (forecast no disponible → máx)"
        if remaining_solar_kwh < energy:
            return cc.max_a, cc.max_a, (f"SOLAR (solar restante {remaining_solar_kwh:.1f} < "
                                        f"{energy:.1f} kWh → máx)")
        why = "calor + solar suficiente" if cc.temp_gate_enabled else "solar suficiente"
        target, raw = amps_for(energy, solar_end_hour - hour)
        return target, raw, f"SOLAR ({why} → mín)"

    return current, current, "IDLE (sin carga — sin cambios)"


def run_charge_current_controller(cfg: AppConfig, simulate: bool = False) -> None:
    """Ajusta la corriente máxima de carga (holding 40087) al mínimo necesario.

    Lee SOC/temp/V/tope por MODBUS, calcula el objetivo (valle/solar/idle) y, solo
    si difiere del tope actual, lo escribe por Playwright (1.2) y verifica el
    resultado releyendo 40087 por MODBUS.

    Si `simulate=True`: solo lee y calcula, registra a INFO el tope actual, el
    objetivo y si cambiaría, y NO toca el inversor (ni Playwright ni verificación).
    Útil para el botón "Simular control" de la web.
    """
    logger = logging.getLogger(__name__)
    if not cfg.charge_current.enabled and not simulate:
        return

    try:
        state = read_inverter_state(cfg.inverter)
    except InverterError as e:
        logger.warning(f"Controlador corriente de carga: no se pudo leer MODBUS — {e}")
        return

    # 40087 debe leer 1..66; un 0 significa que no se pudo leer → no escribir a ciegas
    if state.charge_current_max_a < 1:
        logger.warning(
            "Controlador corriente de carga: tope actual (40087) ilegible — se omite este tick"
        )
        return

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(cfg.system.timezone))
    hour = now.hour + now.minute / 60.0
    current = int(round(state.charge_current_max_a))

    remaining_solar, solar_end = _remaining_solar_forecast(cfg, now)
    schedule_state = _load_schedule_state()
    target, calculated, mode = _compute_target_charge_current(
        cfg, state, hour, schedule_state, current, remaining_solar, solar_end)

    solar_txt = "?" if remaining_solar is None else f"{remaining_solar:.1f}"
    calc_txt = "" if calculated == target else f" (calculado {calculated}A)"
    msg = (
        f"[CORRIENTE] modo={mode} · SOC {state.soc_pct}% · temp {state.battery_temp_c}ºC · "
        f"V {state.battery_voltage_v} · solar_rest {solar_txt}kWh · fin_solar {solar_end:.1f}h · "
        f"actual {current}A · objetivo {target}A{calc_txt}"
    )

    # Simulación: solo informa, no toca el inversor.
    if simulate:
        veredicto = f"cambiaría {current}A → {target}A" if current != target else "sin cambios"
        logger.info(msg + f" → SIMULACIÓN: {veredicto} (no se escribe nada)")
        return

    # Sin cambios → DEBUG (cada 15 min sería ruido en INFO); solo INFO al reescribir.
    if current == target:
        logger.debug(msg + " — sin cambios")
        return

    logger.info(msg)

    try:
        set_charge_current(cfg.inverter, target, dry_run=cfg.system.dry_run)
    except AutomationError as e:
        logger.error(f"No se pudo escribir la corriente de carga: {e}")
        return

    if cfg.system.dry_run:
        _record_charge_current_change(cfg, target, current, calculated, mode, state, now,
                                      dry_run=True, verified=False)
        return

    # Verificación read-back por MODBUS (más fiable que releer la web)
    verified = False
    try:
        verify = read_inverter_state(cfg.inverter)
        if int(round(verify.charge_current_max_a)) == target:
            logger.info(f"[CORRIENTE] verificada: {target}A aplicada")
            verified = True
        else:
            logger.error(
                f"[CORRIENTE] verificación FALLIDA: objetivo {target}A, "
                f"inversor quedó {verify.charge_current_max_a}A"
            )
    except InverterError as e:
        logger.warning(f"No se pudo verificar la corriente de carga por MODBUS: {e}")

    _record_charge_current_change(cfg, target, current, calculated, mode, state, now,
                                  dry_run=False, verified=verified)


def _record_charge_current_change(cfg, target, previous, calculated, mode, state, now,
                                  dry_run, verified):
    """Persiste el cambio de corriente en InfluxDB; un fallo de BD no rompe el ciclo."""
    try:
        write_charge_current(cfg.influxdb, target, previous, mode, state, now,
                             calculated_a=calculated, dry_run=dry_run, verified=verified)
    except StorageError as e:
        logging.getLogger(__name__).warning(
            f"No se pudo guardar el cambio de corriente en InfluxDB: {e}")


def _configure_firmware_profile(cfg: AppConfig) -> None:
    """Lee el firmware del inversor y fija el perfil de etiquetas activo.
    Pensado para ejecutarse en un hilo daemon al arrancar. Nunca lanza: si la
    lectura falla, configure_active_profile(None) deja el perfil por defecto."""
    logger = logging.getLogger(__name__)
    try:
        firmware = read_firmware_version(cfg.inverter)
    except Exception as e:
        logger.warning(f"No se pudo leer el firmware al arrancar: {e}")
        firmware = None
    configure_active_profile(firmware)


def main() -> None:
    """Punto de entrada — carga config, arranca el scheduler y la interfaz web."""
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR de configuración: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(cfg)
    logger = logging.getLogger(__name__)
    import os, socket
    hostname = os.environ.get("HOST_HOSTNAME") or socket.gethostname()
    logger.info(f"solar-manager v{VERSION} arrancando en {hostname} (dry_run={cfg.system.dry_run})")

    # Avisar de claves de config con nombre obsoleto (siguen funcionando vía alias)
    from app.config import find_deprecated_config_keys
    for dep in find_deprecated_config_keys():
        logger.warning(
            f"config.yaml usa la clave obsoleta '{dep['section']}.{dep['legacy_key']}' "
            f"(={dep['value']}); renómbrala a '{dep['canonical_key']}'. Se sigue "
            f"aceptando vía alias, pero conviene migrarla."
        )

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

    # Leer el firmware del inversor y fijar el perfil de etiquetas de 6.3.1/6.3.2
    # acorde (ver FIRMWARE_PROFILES en automation.py). En segundo plano para no
    # bloquear el arranque; completa en pocos segundos, mucho antes del ciclo de
    # las 23:55. Si falla la lectura, queda el perfil por defecto (el más reciente).
    import threading as _threading
    _threading.Thread(
        target=_configure_firmware_profile, args=(cfg,), daemon=True,
        name="firmware-profile-startup",
    ).start()

    # Rellenar huecos de producción histórica al arrancar (en segundo plano para
    # no bloquear el scheduler). El job diario de las 00:30 mantiene "ayer" al día.
    _threading.Thread(
        target=backfill_solar_history, args=(cfg,), daemon=True, name="backfill-startup",
    ).start()

    from app.scheduler import start_scheduler
    start_scheduler(cfg)


if __name__ == "__main__":
    main()
