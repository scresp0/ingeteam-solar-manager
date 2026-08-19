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

Re-evaluación (run_recheck), ejecutada a cada hora de tariff.schedule_recheck_at:
  Repite pasos 2-6 con el SOC del momento, que ya refleja el consumo y la
  producción solar reales. Si la decisión cambia respecto al estado del inversor,
  reescribe y notifica por email; si no, sale sin tocar nada ni escribir
  ciclo_carga (rompería el JOIN con stats_diarias).
"""

import logging
import sys
from dataclasses import dataclass
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
from app.logger_reader import (
    get_yesterday_stats, get_daily_stats, get_recent_house_power, LoggerReaderError,
)
from app.storage import (
    write_cycle, write_daily_stats, write_half_hour_stats, write_half_hour_forecast,
    write_charge_current,
    get_avg_night_consumption, get_avg_post_valley_consumption, get_avg_daily_consumption,
    get_dynamic_risk_factor, get_dynamic_solar_bias, get_house_power_profile,
    get_last_real_solar_date, get_production_window_end_hour, StorageError,
)

# Máximo de días que el backfill rellena de una vez. El datalogger del inversor es
# una ventana RODANTE de ~59 días (verificado por búsqueda binaria el 2026-08-17):
# lo que no se copie a InfluxDB antes de que salga de esa ventana se pierde para
# siempre. 59 aprovecha el histórico entero; el coste medido es ~17 s y 54 MB por
# LAN para los 59 días, así que no hay razón para quedarse corto.
MAX_BACKFILL_DAYS = 59


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

    # 3b. Consumo diario dinámico. Es el fallback `average_daily_consumption_kwh`
    # el que se calibra aquí; `decide_charge` deriva el consumo diurno restando
    # (`daily − night`), así que dejar este fijo mientras el nocturno se movía
    # solo hacía que el diurno absorbiera el error de ambos.
    daily_consumption_kwh = cfg.installation.average_daily_consumption_kwh
    avg_daily = get_avg_daily_consumption(
        cfg.influxdb,
        window_days=cfg.charging.daily_consumption_window_days,
        min_days=cfg.charging.daily_consumption_min_days_in_window,
    )
    if avg_daily is not None:
        logger.info(
            f"Consumo diario dinámico: {avg_daily} kWh "
            f"(media {cfg.charging.daily_consumption_window_days}d · "
            f"fallback config: {cfg.installation.average_daily_consumption_kwh} kWh)"
        )
        daily_consumption_kwh = avg_daily
    else:
        logger.info(
            f"Consumo diario: {daily_consumption_kwh} kWh "
            f"(config — menos de {cfg.charging.daily_consumption_min_days_in_window} días en InfluxDB)"
        )

    # Coherencia: el nocturno es un subconjunto del diario. Si la media dinámica
    # del nocturno supera al diario (ventanas distintas, o un diario que cayó al
    # fallback de config), `daytime = daily − night` se iría a 0 y el déficit
    # colapsaría en silencio. Preferimos avisar y recortar.
    if night_consumption_kwh > daily_consumption_kwh:
        logger.warning(
            f"Consumo nocturno ({night_consumption_kwh} kWh) supera al diario "
            f"({daily_consumption_kwh} kWh) — se recorta al diario. Revisa "
            f"average_daily_consumption_kwh o las ventanas de calibración."
        )
        night_consumption_kwh = daily_consumption_kwh

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
        daily_consumption_kwh=daily_consumption_kwh,
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
        write_half_hour_stats(cfg.influxdb, stats)
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
    Re-evaluación de la decisión (cada tariff.schedule_recheck_at, p.ej. 19:00 y 03:00).

    Recoge inputs frescos (SOC actual, que ya incorpora el consumo y la producción
    solar reales del día), recalcula decide_charge/decide_discharge y, si la decisión
    difiere del estado actual del inversor, la aplica y notifica por email.
    Si no difiere, sale silenciosamente.

    Ojo con las horas de tarde: el algoritmo parte del SOC actual y solo descuenta
    el consumo nocturno (00:00–07:59), así que a las 19:00 el consumo de la tarde
    aún no se resta → la decisión es optimista respecto a la de las 23:55, que es
    la canónica. Ver la nota de reference_date en CLAUDE.md.

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

    # Un día está completo solo si tiene TODOS los campos de media hora. Los campos
    # se han ido añadiendo en momentos distintos (house/grid en v1.73), así que hay
    # que arrancar desde el más atrasado: mirando solo `real_kwh` los días antiguos
    # se darían por hechos y los campos nuevos no se rellenarían nunca.
    last_by_field = [
        get_last_real_solar_date(cfg.influxdb, field=f)
        for f in ("real_kwh", "house_kwh", "grid_import_kwh", "grid_export_kwh")
    ]
    last_stored = None if any(d is None for d in last_by_field) else min(last_by_field)
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
            write_half_hour_stats(cfg.influxdb, stats)
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

# Plazo en el que el consumo de casa por franja pasa de estimarse por persistencia
# (mediana de la última hora — capta si hoy hace más/menos calor de lo normal) a
# estimarse por el perfil histórico de esa franja (sabe que la tarde consume más
# que la mañana aunque la mañana de hoy haya sido tranquila). Ver
# `_house_consumption_for_slot`.
_HOUSE_PROFILE_BLEND_HOURS = 2.0


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


@dataclass
class SolarWindow:
    """Perfil de excedente solar que queda HOY, franja de 30 min a franja de 30 min.

    `surplus[i]` = kWh que la batería PODRÍA absorber en esa franja si la corriente
    no la limitase (producción calibrada − consumo de la vivienda, suelo en 0).
    Es la entrada de `_min_current_for_surplus`.
    """
    surplus: list[float]     # kWh de excedente por franja futura de 30 min
    end_hour: float          # hora local de fin de producción
    gross_kwh: float         # producción calibrada restante, antes de restar casa
    house_kw: float          # potencia de casa asumida por franja
    house_source: str        # "medido" (datalogger de hoy) | "media" (fallback)

    @property
    def surplus_kwh(self) -> float:
        return round(sum(self.surplus), 2)


def _house_power_estimate(cfg: AppConfig) -> tuple[float, str]:
    """Potencia (kW) que se asume que la vivienda consumirá en las horas de sol restantes.

    Predictor de PERSISTENCIA: lo que la casa ha consumido en la última hora es la
    mejor estimación de lo que consumirá en la próxima. Para el aire acondicionado
    —que es lo que domina la varianza en verano— la inercia es de horas, así que
    esto bate a cualquier media histórica de 30 días, que no sabe si hoy hace 40º o
    está nublado.

    Fallback si el datalogger no responde: consumo post-valle medio prorrateado a
    tasa plana sobre las 16 h del tramo (el comportamiento hasta v1.71).

    ⚠️ El fallback SOBREESTIMA el consumo diurno por dos motivos acumulados: reparte
    sobre las horas solares un total que incluye la tarde-noche sin sol, y se calcula
    sobre `stats_diarias.consumption_kwh`, que integra `PacGrid` (salida AC del
    inversor, exportación incluida) en lugar del consumo real de la vivienda — medido
    +39% sobre 14 días (2026-08-17). Sobreestimar el consumo infravalora el excedente
    y sube la corriente, que es el lado seguro para llenar la batería, pero cuesta
    calor. Por eso el camino medido es el principal y este solo el paracaídas.
    """
    logger = logging.getLogger(__name__)

    house_w = get_recent_house_power(
        cfg.inverter,
        cfg.charge_current.house_power_window_min,
        cfg.charge_current.house_power_cache_min,
    )
    if house_w is not None:
        return house_w / 1000.0, "medido"

    try:
        post_valley = get_avg_post_valley_consumption(
            cfg.influxdb, cfg.charging.night_consumption_window_days,
            cfg.charging.night_consumption_min_days_in_window)
    except StorageError:
        post_valley = None
    if post_valley is None:
        post_valley = max(0.0, cfg.installation.average_daily_consumption_kwh
                          - cfg.charging.night_consumption_kwh)
    logger.debug(f"Consumo de casa: sin lectura del datalogger → media {post_valley:.2f} kWh/16h")
    return post_valley / 16.0, "media"


def _house_consumption_for_slot(
    slot_start, now, persistence_kwh: float, profile: dict[str, float] | None,
) -> float:
    """kWh que se asume consumirá la vivienda en una franja de 30 min, mezclando
    la persistencia (mediana de la última hora) con el perfil histórico de esa
    franja horaria.

    Peso de la persistencia = 1 en la franja actual, decae linealmente a 0 en
    `_HOUSE_PROFILE_BLEND_HOURS`. Cerca de ahora manda la persistencia (capta si
    hoy hace más o menos calor de lo normal); más allá manda el histórico, que
    sabe que la tarde consume más que la mañana aunque la mañana de hoy haya sido
    tranquila — es lo que la persistencia sola no puede anticipar (gotcha real
    del 2026-08-18: consumo de casa 0,5 kW por la mañana → controlador a 33A todo
    el día → aire acondicionado sube el consumo de tarde a 1,1-2,3 kW → batería se
    queda al 89%). Sin perfil (pocos días en InfluxDB), pura persistencia.
    """
    if not profile:
        return persistence_kwh
    profile_kwh = profile.get(f"{slot_start.hour:02d}:{slot_start.minute:02d}")
    if profile_kwh is None:
        return persistence_kwh
    hours_ahead = max(0.0, (slot_start - now).total_seconds() / 3600.0)
    w = max(0.0, 1.0 - hours_ahead / _HOUSE_PROFILE_BLEND_HOURS)
    return w * persistence_kwh + (1 - w) * profile_kwh


def _solar_surplus_window(cfg: AppConfig, now) -> SolarWindow | None:
    """Construye el perfil de excedente solar restante de HOY por franjas de 30 min.

    Devuelve:
    - forecast OK con producción pendiente → SolarWindow
    - forecast OK sin producción pendiente → SolarWindow con end_hour = 0.0 → fuerza IDLE
    - forecast no disponible               → None (el llamante cae a la fórmula plana)

    **Producción por franja: p50 · bias, sin ponderar con p10.** El `risk_factor`
    inclina hacia p10 (pesimista) porque `decide_charge` toma UNA decisión a las 23:55
    que no puede revisar hasta las 08:00. Este controlador se recalcula cada
    `interval_min`: si se queda corto, el siguiente tick lo ve y sube la corriente. En
    un lazo cerrado el pesimismo no compra seguridad, solo cuesta calor — y apilado
    sobre la resta del consumo fue lo que disparó el acantilado a 66 A de v1.68. El
    único margen deliberado vive en `charge_current.margin`.

    **Consumo de casa por franja: persistencia cerca de ahora, perfil histórico
    lejos** (`_house_consumption_for_slot`). Restar la misma mediana de la última
    hora en las 26 franjas del día (hasta v1.7x) asumía que el consumo de la
    mañana representa el de toda la jornada — falso en verano, cuando el AC dispara
    el consumo de tarde muy por encima del de la mañana.

    El fin de producción se calcula con la producción BRUTA: una franja sigue
    produciendo aunque la casa se coma todo lo que da.

    **Pre-check barato antes de bias/consumo (v1.79):** `intervals` es lo primero
    que se pide, y ya trae `pv_estimate` crudo de Solcast. Si NINGUNA franja futura
    de hoy tiene `pv_estimate > 0` (exacto 0.0 en Solcast de noche — no hay
    ambigüedad de `bias` que valga: bias solo escala un valor que ya es cero), el
    resultado va a ser una ventana vacía de todos modos, así que se corta ahí sin
    pagar `get_dynamic_solar_bias` (InfluxDB), `_house_power_estimate` (datalogger,
    el caro) ni `get_house_power_profile` (InfluxDB). Cubre la tarde-noche tras el
    fin de producción; el valle (00:00–08:00) se corta un nivel más arriba, en
    `run_charge_current_controller`, porque ahí SÍ hay producción prevista más
    tarde ese mismo día — este pre-check no serviría para distinguirlo.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    logger = logging.getLogger(__name__)

    try:
        intervals = get_today_intervals(cfg.solcast, cfg.system.timezone)
    except SolcastError as e:
        logger.warning(f"Controlador corriente: forecast de hoy no disponible — {e}")
        return None

    tz = ZoneInfo(cfg.system.timezone)
    parsed: list[tuple] = []
    for it in intervals:
        try:
            end_str = it["period_end"].rstrip("Z").split(".")[0] + "+00:00"
            end_local = datetime.fromisoformat(end_str).astimezone(tz)
        except (KeyError, ValueError):
            continue
        if end_local <= now:
            continue
        parsed.append((end_local, it.get("pv_estimate", 0.0)))

    if not any(pv > 0 for _, pv in parsed):
        return SolarWindow([], 0.0, 0.0, 0.0, "sin sol restante")

    try:
        bias = get_dynamic_solar_bias(
            cfg.influxdb, cfg.charging.solar_bias_window_days,
            cfg.charging.solar_bias_min_days_in_window)
    except StorageError:
        bias = None
    bias = cfg.charging.solar_bias_factor if bias is None else bias

    house_kw, house_source = _house_power_estimate(cfg)
    house_per_interval = house_kw * 0.5   # kWh que la casa consume en una franja de 30 min

    try:
        house_profile = get_house_power_profile(
            cfg.influxdb, cfg.charge_current.house_profile_window_days,
            cfg.charge_current.house_profile_min_days_in_window)
    except StorageError:
        house_profile = None

    surplus: list[float] = []
    gross = 0.0
    end_hour = None
    for end_local, pv_estimate in parsed:
        eff = pv_estimate * bias * 0.5   # kWh de la franja (p50 calibrado)
        if eff > 0.02:
            gross += eff
            slot_start = end_local - timedelta(minutes=30)
            house_kwh_slot = _house_consumption_for_slot(
                slot_start, now, house_per_interval, house_profile)
            surplus.append(max(0.0, eff - house_kwh_slot))
            end_hour = end_local.hour + end_local.minute / 60.0

    if end_hour is None:
        return SolarWindow([], 0.0, 0.0, house_kw, house_source)   # ya no queda sol → IDLE

    win = SolarWindow(surplus, end_hour, round(gross, 2), house_kw, house_source)
    logger.debug(
        f"Excedente solar: bruto {win.gross_kwh:.2f} kWh − casa "
        f"{house_kw:.2f} kW ({house_source}, perfil histórico "
        f"{'disponible' if house_profile else 'no disponible'}) → neto "
        f"{win.surplus_kwh:.2f} kWh en {len(surplus)} franjas hasta las {end_hour:.1f}h"
    )
    return win


def _min_current_for_surplus(
    surplus: list[float], energy_kwh: float, v: float, cc,
) -> tuple[int, bool]:
    """Corriente mínima (A) que mete `energy_kwh` en la batería dado el perfil de
    excedente solar por franja. Devuelve (amperios_sin_acotar, objetivo_alcanzable).

    La batería carga en cada franja a `min(I·V·0.5h, excedente_de_la_franja)`: la
    corriente es un TECHO, no un caudal garantizado. Repartir la energía linealmente
    sobre las horas restantes —lo que hacía `amps_for`— ignora que la producción es
    una campana: a las 9:00 y a las 18:00 no hay `I·V` disponibles aunque el total
    del día sobre, así que el plan se quedaba corto y el lazo lo corregía tarde,
    subiendo a 66 A al final de la tarde.

    `captured(I)` es monótona creciente en I, así que el primer I que alcanza el
    objetivo es el mínimo. 66×48 iteraciones cada `interval_min`: irrelevante.

    Si ni `max_a` llega, se devuelve el pico de excedente previsto: por encima de él
    no se captura ni un vatio más, así que es el techo útil. Devolver eso en lugar de
    `max_a` es lo que evita el acantilado a 66 A por unas décimas de kWh. Ojo: ese
    pico puede quedar por ENCIMA de `max_a` (excedente concentrado al mediodía), y
    entonces el límite real es la corriente, no el sol — el llamante distingue los
    dos casos al etiquetar comparando el valor crudo con `max_a`.

    En el régimen sin limitación solar el resultado coincide con la fórmula plana
    `E·1000/(V·H)·margin` salvo por el redondeo (aquí hacia arriba, por ser "el
    mínimo que cumple"), así que el cambio no altera los casos que ya funcionaban.
    """
    target = energy_kwh * cc.margin

    def captured(amps: int) -> float:
        cap_slot = amps * v * 0.5 / 1000.0      # kWh que caben en una franja a `amps`
        return sum(min(cap_slot, s) for s in surplus)

    for amps in range(1, cc.max_a + 1):
        if captured(amps) >= target:
            return amps, True

    peak_kwh = max(surplus, default=0.0)
    return max(1, int(round(peak_kwh / 0.5 * 1000.0 / v))), False


def _compute_target_charge_current(
    cfg: AppConfig, state: InverterState, hour: float,
    schedule_state, current: int, solar_end_hour: float,
    window: SolarWindow | None = None,
) -> tuple[int, int, str]:
    """Devuelve (amperios_objetivo, amperios_calculados, modo).

    `amperios_calculados` es la corriente cruda que sale de la fórmula solar/valle
    ANTES de acotarla al `[floor_a, max_a]` de la configuración; `amperios_objetivo`
    es esa misma corriente ya acotada (la que se fija en el inversor). Cuando el
    cálculo cae por debajo del mínimo configurado, `objetivo` = mínimo pero
    `calculado` refleja el valor real que pedía la fórmula. En los modos que no
    calculan corriente (idle / batería llena / máx por temperatura) ambos
    coinciden con el valor devuelto.

    - VALLE (00:00–08:00 con carga de red): corriente mínima para llegar al target
      en lo que queda de valle (sin temperatura — de noche no hay calor). La red
      entrega potencia constante, así que aquí el reparto lineal SÍ es correcto.
    - SOLAR (08:00–fin de producción): con `window`, corriente mínima que llena la
      batería simulando el excedente franja a franja (`_min_current_for_surplus`);
      sin `window` (forecast caído), reparto lineal sobre las horas restantes.
      Acotada a [floor_a, max_a]. Con temp_gate_enabled=True, si la batería está fría
      (temp ≤ hot_threshold) va a máx (capta picos intermitentes que el forecast p50
      no ve); con False la temperatura no influye.
    - Sin carga (idle / valle sin carga / batería llena) → deja la corriente como está.

    BALANCE conserva el reparto lineal a propósito: es el último 2 % de la batería,
    con su propio suelo, y el lazo de 15 min lo reajusta.
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

    # SOLAR: ventana diurna productiva 08:00 – fin de producción del forecast.
    if _VALLE_END_HOUR <= hour < solar_end_hour:
        energy = (max_soc - soc) / 100.0 * cap
        if energy <= 0:
            return current, current, "SOLAR (batería llena — sin cambios)"
        if cc.temp_gate_enabled and state.battery_temp_c <= cc.hot_threshold_c:
            return cc.max_a, cc.max_a, f"SOLAR (temp {state.battery_temp_c}≤{cc.hot_threshold_c}ºC → máx)"
        if window is not None and window.surplus:
            raw, reached = _min_current_for_surplus(window.surplus, energy, v, cc)
            target = max(cc.floor_a, min(cc.max_a, raw))
            if reached:
                label = "SOLAR (rampa al excedente previsto)"
            elif raw >= cc.max_a:
                label = "SOLAR (máx — el excedente supera el tope de corriente)"
            else:
                label = "SOLAR (limitada por sol — más A no captan más)"
            return target, raw, label
        target, raw = amps_for(energy, solar_end_hour - hour)
        return target, raw, "SOLAR (rampa a fin de producción — sin forecast)"

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

    if hour < _VALLE_END_HOUR:
        # En valle la decisión (VALLE/BALANCE-en-valle) nunca consulta el excedente
        # solar — usa el reloj fijo 00:00–08:00 y el objetivo ya decidido a las
        # 23:55 (`schedule_state`). Construir la ventana aquí sería tirar siempre
        # el resultado: verificado ejecutando `_compute_target_charge_current` con
        # windows contradictorios y comprobando que el resultado no cambia (ver
        # CLAUDE.md). No se llama ni siquiera al fallback barato
        # `_productive_window_end` (también hace una query a InfluxDB) — es tan
        # inútil en valle como la ventana completa.
        window, solar_end = None, cfg.charge_current.productive_window_end_hour
    else:
        window = _solar_surplus_window(cfg, now)
        solar_end = window.end_hour if window is not None else _productive_window_end(cfg)
    schedule_state = _load_schedule_state()
    target, calculated, mode = _compute_target_charge_current(
        cfg, state, hour, schedule_state, current, solar_end, window)

    if window is None:
        solar_txt, house_txt = "?", "?"
    else:
        solar_txt = f"{window.surplus_kwh:.1f}"
        house_txt = f"{window.house_kw:.2f}kW/{window.house_source}"
    calc_txt = "" if calculated == target else f" (calculado {calculated}A)"
    msg = (
        f"[CORRIENTE] modo={mode} · SOC {state.soc_pct}% · temp {state.battery_temp_c}ºC · "
        f"V {state.battery_voltage_v} · excedente {solar_txt}kWh · casa {house_txt} · "
        f"fin_solar {solar_end:.1f}h · actual {current}A · objetivo {target}A{calc_txt}"
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
