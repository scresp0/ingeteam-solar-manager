"""
storage.py — almacenamiento de datos en InfluxDB 2.x.

Guarda tres tipos de puntos por ciclo nocturno:

1. measurement: ciclo_carga
   — decisión tomada por el algoritmo + estado del inversor en ese momento

2. measurement: stats_diarias
   — acumulados del día anterior leídos del datalogger del inversor

3. measurement: solar_media_hora
   — producción solar y forecast en intervalos de 30 min
     forecast_p50/p10/p90_kwh: escritos en el ciclo nocturno para mañana
     real_kwh: escritos al día siguiente con los datos del datalogger
     Timestamps en "hora local española etiquetada como UTC" (igual que stats_diarias)

4. measurement: corriente_carga
   — un punto por cada cambio efectivo de la corriente máxima de carga (40087)
     que aplica el controlador de corriente. Timestamp al segundo del cambio
     ("hora local española etiquetada como UTC"). Tag mode=VALLE/SOLAR/IDLE.
     Para informes posteriores de a qué hora y por qué cambió la corriente.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from app.config import InfluxDBConfig
from app.decision import DecisionInput, ChargeDecision
from app.inverter import InverterState
from app.logger_reader import DailyStats

logger = logging.getLogger(__name__)


class StorageError(Exception):
    pass


def write_cycle(
    cfg: InfluxDBConfig,
    inp: DecisionInput,
    result: ChargeDecision,
    state: InverterState,
    solcast_error: bool = False,
    automation_ok: bool = True,
    timestamp: datetime | None = None,
) -> None:
    """
    Guarda el resultado del ciclo nocturno en InfluxDB.

    Args:
        cfg:           configuración de InfluxDB
        inp:           input del algoritmo de decisión
        result:        resultado del algoritmo
        state:         estado del inversor leído vía MODBUS
        solcast_error: True si Solcast falló y se usó fallback
        automation_ok: True si se escribió correctamente en el inversor
        timestamp:     timestamp del punto (por defecto: ahora)
    """
    if not cfg.enabled:
        return

    ts = timestamp or datetime.now(timezone.utc)

    point = {
        "measurement": "ciclo_carga",
        "time": ts.isoformat(),
        "fields": {
            # Estado del inversor
            "soc_actual_pct":       state.soc_pct,
            "soh_pct":              state.soh_pct,
            "battery_power_w":      float(state.battery_power_w),
            "battery_voltage_v":    state.battery_voltage_v,
            "battery_temp_c":       state.battery_temp_c,
            # Previsión Solcast
            "forecast_p10_kwh":     inp.forecast_day1.p10,
            "forecast_p50_kwh":     inp.forecast_day1.p50,
            "forecast_p90_kwh":     inp.forecast_day1.p90,
            "solar_effective_kwh":  result.solar_effective_kwh,
            # Cálculo
            "energy_stored_kwh":    result.energy_stored_kwh,
            "energy_at_dawn_kwh":   result.energy_at_dawn_kwh,
            "deficit_kwh":          result.deficit_kwh,
            # Decisión
            "charge_needed":        result.charge_needed,
            "target_soc_pct":       result.target_soc_pct,
            "target_kwh":           result.target_kwh,
            "valley_day_skip":      result.valley_day_skip,
            # Metadatos
            "solcast_error":        solcast_error,
            "automation_ok":        automation_ok,
            "dry_run":              result.dry_run,
        }
    }

    _write_point(cfg, point)
    logger.info(f"Ciclo guardado en InfluxDB (charge_needed={result.charge_needed}, soc={state.soc_pct}%)")


def write_charge_current(
    cfg: InfluxDBConfig,
    current_a: int,
    previous_a: int,
    mode: str,
    state: InverterState,
    now: datetime,
    calculated_a: int | None = None,
    dry_run: bool = False,
    verified: bool = True,
) -> None:
    """
    Registra un cambio de la corriente máxima de carga (holding 40087) en InfluxDB.

    Un punto por cada cambio efectivo aplicado por el controlador de corriente, con
    timestamp al segundo del momento del cambio. Pensado para informes posteriores
    (a qué hora y por qué cambió la corriente, con el SOC/temperatura del momento).

    Args:
        cfg:        configuración de InfluxDB
        current_a:  corriente aplicada tras el cambio (A) = "fijado" (acotado a config)
        previous_a: corriente que había antes del cambio (A)
        mode:       modo del controlador (la cadena de _compute_target_charge_current)
        state:      estado del inversor leído vía MODBUS en ese momento
        now:        hora local del cambio (aware); se etiqueta como UTC igual que el
                    resto de datos de visualización del proyecto
        calculated_a: corriente cruda calculada por el algoritmo ANTES de acotarla al
                    mínimo/máximo de config = "calculado". None (registros antiguos) o
                    igual a current_a cuando no hubo acotación.
        dry_run:    True si el cambio no se escribió realmente en el inversor
        verified:   True si la lectura read-back por MODBUS confirmó el valor aplicado
    """
    if not cfg.enabled:
        return

    # Hora local española etiquetada como UTC (misma convención que stats_diarias /
    # solar_media_hora) — conserva fecha/hora/minuto/segundo del cambio tal cual.
    ts = now.replace(tzinfo=timezone.utc)

    point = {
        "measurement": "corriente_carga",
        "time": ts.isoformat(),
        "tags": {
            # primer token del modo: VALLE / SOLAR / IDLE → agrupa en informes
            "mode": mode.split()[0] if mode else "?",
        },
        "fields": {
            "current_a":         float(current_a),
            "calculated_a":      float(current_a if calculated_a is None else calculated_a),
            "previous_a":        float(previous_a),
            "delta_a":           float(current_a - previous_a),
            "soc_pct":           state.soc_pct,
            "battery_temp_c":    state.battery_temp_c,
            "battery_voltage_v": state.battery_voltage_v,
            "detail":            mode,
            "verified":          verified,
            "dry_run":           dry_run,
        },
    }

    _write_point(cfg, point)
    logger.info(
        f"Cambio de corriente guardado en InfluxDB ({previous_a}A → {current_a}A, modo={mode})"
    )


def write_daily_stats(cfg: InfluxDBConfig, stats: DailyStats) -> None:
    """
    Guarda los acumulados diarios del inversor en InfluxDB.

    Args:
        cfg:   configuración de InfluxDB
        stats: acumulados calculados por logger_reader
    """
    if not cfg.enabled:
        return

    # Timestamp = medianoche del día de los datos (en UTC)
    ts = datetime.combine(stats.date, datetime.min.time()).replace(tzinfo=timezone.utc)

    point = {
        "measurement": "stats_diarias",
        "time": ts.isoformat(),
        "tags": {
            "device_id": stats.device_id,
        },
        "fields": {
            "solar_kwh":              stats.solar_kwh,
            "grid_consumed_kwh":      stats.grid_consumed_kwh,
            "grid_exported_kwh":      stats.grid_exported_kwh,
            "consumption_kwh":        stats.consumption_kwh,
            "night_consumption_kwh":  stats.night_consumption_kwh,
            "soc_start_pct":          stats.soc_start_pct,
            "soc_end_pct":            stats.soc_end_pct,
            "peak_soc_pct":           stats.peak_soc_pct,
            "battery_charged_kwh":    stats.battery_charged_kwh,
            "records":                float(stats.records),
        }
    }

    _write_point(cfg, point)
    logger.info(
        f"Stats {stats.date} guardadas en InfluxDB "
        f"(solar={stats.solar_kwh} kWh, red={stats.grid_consumed_kwh} kWh)"
    )


def get_production_window_end_hour(
    cfg: InfluxDBConfig,
    pct: int = 90,
    window_days: int = 30,
    min_days: int = 3,
) -> float | None:
    """
    Hora local (float, p.ej. 16.5) en la que la producción solar real media acumula
    `pct`% del total diario, a partir de `solar_media_hora` (campo real_kwh) de los
    últimos `window_days` días. Captura el perfil de la instalación (más producción
    por la mañana → T_fin más temprano).

    Devuelve None si InfluxDB está desactivado, hay menos de `min_days` días con
    datos, o la consulta falla. El factor de promediado se cancela en el ratio, así
    que basta sumar por franja horaria sobre todos los días.
    """
    if not cfg.enabled:
        return None

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{window_days}d)
  |> filter(fn: (r) => r._measurement == "solar_media_hora" and r._field == "real_kwh")
"""
    try:
        from collections import defaultdict
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)

        slot_sum: dict[int, float] = defaultdict(float)
        dates: set = set()
        for table in tables:
            for rec in table.records:
                t = rec.get_time()   # "hora local etiquetada UTC" → t.hour es hora local
                slot = t.hour * 2 + (1 if t.minute >= 30 else 0)
                slot_sum[slot] += rec.get_value() or 0.0
                dates.add(t.date())

        if len(dates) < min_days:
            logger.debug(
                f"Ventana productiva: solo {len(dates)} días (<{min_days}) — usando fallback"
            )
            return None

        total = sum(slot_sum.values())
        if total <= 0:
            return None

        threshold = pct / 100.0 * total
        cum = 0.0
        for slot in sorted(slot_sum):
            cum += slot_sum[slot]
            if cum >= threshold:
                end_hour = (slot * 30 + 30) / 60.0   # fin de la franja que cruza el umbral
                logger.debug(
                    f"Ventana productiva: T_fin={end_hour:.2f}h "
                    f"({pct}% del total, {len(dates)} días)"
                )
                return round(min(end_hour, 24.0), 2)
        return 24.0

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo calcular la ventana productiva en InfluxDB: {e}")
        return None


def get_avg_night_consumption(
    cfg: InfluxDBConfig,
    window_days: int = 30,
    min_days: int = 14,
) -> float | None:
    """
    Devuelve el consumo nocturno medio (kWh, 00:00–07:59) de los últimos
    window_days días almacenados en InfluxDB.

    Devuelve None si:
    - InfluxDB no está habilitado
    - Hay menos de min_days registros válidos en la ventana
    - La consulta falla (se loguea como warning)
    """
    if not cfg.enabled:
        return None

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{window_days}d)
  |> filter(fn: (r) => r._measurement == "stats_diarias" and r._field == "night_consumption_kwh")
  |> filter(fn: (r) => r._value > 0.5)
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            values = [rec.get_value() for table in tables for rec in table.records]

        if len(values) < min_days:
            logger.debug(
                f"Consumo nocturno dinámico: solo {len(values)} días válidos "
                f"(mínimo {min_days}) — usando fallback"
            )
            return None

        avg = sum(values) / len(values)
        logger.debug(f"Consumo nocturno dinámico: {len(values)} días, media={avg:.3f} kWh")
        return round(avg, 3)

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo consultar consumo nocturno dinámico en InfluxDB: {e}")
        return None


def get_avg_daily_consumption(
    cfg: InfluxDBConfig,
    window_days: int = 30,
    min_days: int = 14,
) -> float | None:
    """
    Devuelve el consumo diario medio (kWh, 00:00–24:00) de los últimos
    window_days días almacenados en InfluxDB.

    Calibra `installation.average_daily_consumption_kwh`, que hasta v1.74 era el
    único parámetro de `decide_charge` SIN ruta dinámica: se usaba el valor de
    config con cualquier cantidad de histórico. Como el consumo diurno se calcula
    restando (`daily − night`) y `night` sí era dinámico, el diurno absorbía el
    error de ambos: con 16.0 en config frente a 19.84 kWh medidos en verano, se
    subestimaba el déficit en ~3.8 kWh/día (17 puntos de SOC).

    ⚠️ Solo es fiable desde v1.73. Antes, `consumption_kwh` se calculaba con
    ∫`PacGrid` e inflaba el consumo un +39% (ver `logger_reader.house_power_w`);
    conectarlo entonces habría propagado ese error a la única decisión que estaba
    a salvo de él. La ventana deslizante hace además que el valor se adapte solo a
    la estación, que es la razón de ser del parámetro.

    Devuelve None si:
    - InfluxDB no está habilitado
    - Hay menos de min_days registros válidos en la ventana
    - La consulta falla (se loguea como warning)
    """
    if not cfg.enabled:
        return None

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{window_days}d)
  |> filter(fn: (r) => r._measurement == "stats_diarias" and r._field == "consumption_kwh")
  |> filter(fn: (r) => r._value > 0.5)
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            values = [rec.get_value() for table in tables for rec in table.records]

        if len(values) < min_days:
            logger.debug(
                f"Consumo diario dinámico: solo {len(values)} días válidos "
                f"(mínimo {min_days}) — usando fallback"
            )
            return None

        avg = sum(values) / len(values)
        logger.debug(f"Consumo diario dinámico: {len(values)} días, media={avg:.3f} kWh")
        return round(avg, 3)

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo consultar consumo diario dinámico en InfluxDB: {e}")
        return None


def get_avg_post_valley_consumption(
    cfg: InfluxDBConfig,
    window_days: int = 30,
    min_days: int = 14,
) -> float | None:
    """
    Devuelve el consumo POST-VALLE medio (kWh, 08:00–24:00) de los últimos
    window_days días almacenados en InfluxDB.

    Post-valle = consumption_kwh − night_consumption_kwh por día. NO es el
    consumo diurno solar (08:00–anochecer): es más ancho (incluye la tarde-noche
    tras el ocaso). El controlador de corriente lo prorratea a tasa plana sobre
    las 16 h del tramo para estimar el consumo que compite con el sol.

    Devuelve None si:
    - InfluxDB no está habilitado
    - Hay menos de min_days días con AMBOS campos en la ventana
    - La consulta falla (se loguea como warning)
    """
    if not cfg.enabled:
        return None

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{window_days}d)
  |> filter(fn: (r) => r._measurement == "stats_diarias" and
     (r._field == "consumption_kwh" or r._field == "night_consumption_kwh"))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            values = []
            for table in tables:
                for rec in table.records:
                    daily = rec.values.get("consumption_kwh")
                    night = rec.values.get("night_consumption_kwh")
                    if daily is None or night is None:
                        continue
                    post = daily - night
                    if post > 0.5:   # descarta días con datos erróneos
                        values.append(post)

        if len(values) < min_days:
            logger.debug(
                f"Consumo post-valle dinámico: solo {len(values)} días válidos "
                f"(mínimo {min_days}) — usando fallback"
            )
            return None

        avg = sum(values) / len(values)
        logger.debug(f"Consumo post-valle dinámico: {len(values)} días, media={avg:.3f} kWh")
        return round(avg, 3)

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo consultar consumo post-valle dinámico en InfluxDB: {e}")
        return None


def get_house_power_profile(
    cfg: InfluxDBConfig,
    window_days: int = 30,
    min_days_in_window: int = 14,
) -> dict[str, float] | None:
    """
    Perfil histórico de consumo de la vivienda por franja de 30 min: mediana de
    `house_kwh` (measurement `solar_media_hora`, campo disponible desde v1.73) en
    cada franja sobre los últimos `window_days` días. Devuelve
    `{"HH:MM": kWh_de_la_franja}`, con "HH:MM" la hora de INICIO de la franja
    (mismo convenio que `write_half_hour_stats`: timestamp = inicio, "hora local
    etiquetada como UTC" — se lee `.hour`/`.minute` del timestamp tal cual, sin
    conversión de zona horaria).

    Se usa como ancla de largo plazo en `_solar_surplus_window` (main.py) para las
    franjas alejadas del momento actual, donde la persistencia (mediana de la
    última hora) no tiene base real para extrapolar — el consumo sube por la
    tarde (aire acondicionado) aunque la mañana de hoy haya sido tranquila.

    Devuelve None si:
    - InfluxDB no está habilitado
    - La franja peor cubierta tiene menos de min_days_in_window registros
      (criterio conservador: basta con que UNA franja esté infra-muestreada para
      desconfiar del perfil completo — mismo espíritu que el resto de parámetros
      dinámicos)
    - La consulta falla (se loguea como warning)
    """
    if not cfg.enabled:
        return None

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{window_days}d)
  |> filter(fn: (r) => r._measurement == "solar_media_hora" and r._field == "house_kwh")
"""
    try:
        from influxdb_client import InfluxDBClient
        from statistics import median

        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            by_slot: dict[str, list[float]] = {}
            for table in tables:
                for rec in table.records:
                    t = rec.get_time()
                    by_slot.setdefault(f"{t.hour:02d}:{t.minute:02d}", []).append(rec.get_value())

        if not by_slot:
            return None

        min_samples = min(len(v) for v in by_slot.values())
        if min_samples < min_days_in_window:
            logger.debug(
                f"Perfil de consumo de casa: la franja peor cubierta tiene solo "
                f"{min_samples} días (mínimo {min_days_in_window}) — usando fallback"
            )
            return None

        profile = {slot: round(median(vals), 3) for slot, vals in by_slot.items()}
        logger.debug(
            f"Perfil de consumo de casa: {len(profile)} franjas, "
            f"{min_samples}-{max(len(v) for v in by_slot.values())} días/franja"
        )
        return profile

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo consultar el perfil de consumo de casa en InfluxDB: {e}")
        return None


def _forecast_real_pairs(cfg: InfluxDBConfig, fetch_days: int) -> list[tuple[float, float, float]]:
    """
    Cruza el forecast histórico (ciclo_carga) con la producción solar real
    (stats_diarias) usando el JOIN forecast_date = ciclo_UTC.date() + 1 día.

    Devuelve una lista de tuplas (p10, p50, solar_real) por cada día con ambos
    datos. Propaga ImportError/excepciones de InfluxDB para que el caller las
    maneje (devolviendo fallback).
    """
    from datetime import timedelta
    from influxdb_client import InfluxDBClient

    q_ciclo = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{fetch_days}d)
  |> filter(fn: (r) => r._measurement == "ciclo_carga" and
     (r._field == "forecast_p10_kwh" or r._field == "forecast_p50_kwh"))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
"""

    q_stats = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -{fetch_days}d)
  |> filter(fn: (r) => r._measurement == "stats_diarias" and r._field == "solar_kwh")
  |> filter(fn: (r) => r._value > 0.5)
"""

    with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
        api = client.query_api()
        ciclo_tables = api.query(q_ciclo, org=cfg.org)
        stats_tables = api.query(q_stats, org=cfg.org)

    # date -> solar_kwh (timestamp de stats_diarias = medianoche UTC del día)
    solar_by_date: dict = {}
    for table in stats_tables:
        for rec in table.records:
            solar_by_date[rec.get_time().date()] = rec.get_value()

    pairs: list[tuple[float, float, float]] = []
    for table in ciclo_tables:
        for rec in table.records:
            p10 = rec.values.get("forecast_p10_kwh")
            p50 = rec.values.get("forecast_p50_kwh")
            if p10 is None or p50 is None:
                continue
            # ciclo corre ~22-23h UTC → forecast es para el día siguiente UTC
            forecast_date = rec.get_time().date() + timedelta(days=1)
            solar_real = solar_by_date.get(forecast_date)
            if solar_real is None:
                continue
            pairs.append((p10, p50, solar_real))
    return pairs


def get_dynamic_risk_factor(
    cfg: InfluxDBConfig,
    window_days: int = 30,
    min_days: int = 14,
) -> float | None:
    """
    Calcula el risk_factor óptimo comparando predicciones históricas de Solcast
    con la producción solar real almacenada en InfluxDB.

    Fórmula por día: rf_óptimo = (solar_real - p50) / (p10 - p50), clamp [0,1]

    Devuelve None si:
    - InfluxDB no está habilitado
    - Hay menos de min_days pares válidos en la ventana
    - La consulta falla
    """
    if not cfg.enabled:
        return None

    try:
        pairs = _forecast_real_pairs(cfg, window_days + 2)  # +2: margen UTC+1/+2

        rf_values = []
        for p10, p50, solar_real in pairs:
            if p10 >= p50:
                continue
            rf = (solar_real - p50) / (p10 - p50)
            rf_values.append(max(0.0, min(1.0, rf)))

        if len(rf_values) < min_days:
            logger.debug(
                f"Risk factor dinámico: solo {len(rf_values)} días válidos "
                f"(mínimo {min_days}) — usando fallback"
            )
            return None

        avg_rf = sum(rf_values) / len(rf_values)
        logger.debug(f"Risk factor dinámico: {len(rf_values)} días, media={avg_rf:.3f}")
        return round(avg_rf, 3)

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo calcular risk_factor dinámico desde InfluxDB: {e}")
        return None


def get_dynamic_solar_bias(
    cfg: InfluxDBConfig,
    window_days: int = 30,
    min_days: int = 14,
) -> float | None:
    """
    Calcula el factor de calibración medio del forecast Solcast vs producción real.

    Fórmula por día: factor = solar_real / forecast_p50
    Devuelve la media de los últimos window_days, clamped a [0.5, 1.5] como defensa
    ante muestras pequeñas o anómalas.

    Devuelve None si:
    - InfluxDB no está habilitado
    - Hay menos de min_days pares válidos en la ventana
    - La consulta falla
    """
    if not cfg.enabled:
        return None

    try:
        pairs = _forecast_real_pairs(cfg, window_days + 2)  # +2: margen UTC+1/+2

        factors = []
        for _p10, p50, solar_real in pairs:
            if p50 <= 0.5:
                continue
            factors.append(solar_real / p50)

        if len(factors) < min_days:
            logger.debug(
                f"Factor de calibración solar dinámico: solo {len(factors)} días válidos "
                f"(mínimo {min_days}) — usando fallback"
            )
            return None

        avg = sum(factors) / len(factors)
        clamped = max(0.5, min(1.5, avg))
        if clamped != avg:
            logger.warning(
                f"Factor de calibración solar {avg:.3f} fuera del rango [0.5, 1.5] — "
                f"clamped a {clamped:.3f}"
            )
        logger.debug(
            f"Factor de calibración solar dinámico: {len(factors)} días, "
            f"media={avg:.3f}, aplicado={clamped:.3f}"
        )
        return round(clamped, 3)

    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo calcular factor de calibración solar dinámico: {e}")
        return None


def write_half_hour_stats(cfg: InfluxDBConfig, stats: DailyStats) -> None:
    """
    Guarda el perfil real de un día en resolución de 30 min (48 puntos).

    Campos en `solar_media_hora`:
      - real_kwh         : producción solar (existe desde v1.x)
      - house_kwh        : consumo de la vivienda        (v1.73)
      - grid_import_kwh  : energía tomada de red         (v1.73)
      - grid_export_kwh  : energía vertida a red         (v1.73)

    Van al MISMO measurement que la producción y el forecast, a propósito: comparten
    el timestamp de franja, así que el cruce más útil —el excedente disponible para la
    batería, `real_kwh − house_kwh`— sale de una sola consulta sin JOIN en Python. El
    precio es que el nombre `solar_media_hora` se queda corto; se asume.

    Timestamps en "hora local española etiquetada como UTC" (igual que stats_diarias).
    """
    if not cfg.enabled:
        return

    midnight_utc = datetime.combine(stats.date, datetime.min.time()).replace(tzinfo=timezone.utc)
    points = [
        {
            "measurement": "solar_media_hora",
            "time": (midnight_utc + timedelta(minutes=slot * 30)).isoformat(),
            "fields": {
                "real_kwh":        stats.half_hour_solar_kwh[slot],
                "house_kwh":       stats.half_hour_house_kwh[slot],
                "grid_import_kwh": stats.half_hour_grid_import_kwh[slot],
                "grid_export_kwh": stats.half_hour_grid_export_kwh[slot],
            },
        }
        for slot in range(len(stats.half_hour_solar_kwh))
    ]
    _write_points(cfg, points)
    logger.info(f"Perfil media hora {stats.date} guardado en InfluxDB ({len(points)} slots)")


def write_half_hour_forecast(
    cfg: InfluxDBConfig,
    intervals: list[dict],
    tz_name: str = "Europe/Madrid",
) -> None:
    """
    Guarda el forecast de Solcast para mañana en resolución de 30 min.

    Escribe hasta 48 puntos en solar_media_hora con campos forecast_p50/p10/p90_kwh.
    Convierte period_end UTC a hora local y etiqueta como UTC para alinear con real_kwh.
    """
    if not cfg.enabled or not intervals:
        return

    from zoneinfo import ZoneInfo
    _tz = ZoneInfo(tz_name)

    points = []
    for item in intervals:
        try:
            end_str = item["period_end"].rstrip("Z").split(".")[0] + "+00:00"
            end_utc = datetime.fromisoformat(end_str)
            end_local = end_utc.astimezone(_tz)
            start_local = end_local - timedelta(minutes=30)
            # "local labeled UTC" — mismo convenio que stats_diarias
            ts = datetime(
                start_local.year, start_local.month, start_local.day,
                start_local.hour, start_local.minute, 0,
                tzinfo=timezone.utc,
            )
            points.append({
                "measurement": "solar_media_hora",
                "time": ts.isoformat(),
                "fields": {
                    "forecast_p50_kwh": round(item.get("pv_estimate",   0.0) * 0.5, 4),
                    "forecast_p10_kwh": round(item.get("pv_estimate10", 0.0) * 0.5, 4),
                    "forecast_p90_kwh": round(item.get("pv_estimate90", 0.0) * 0.5, 4),
                },
            })
        except (KeyError, ValueError) as e:
            logger.warning(f"Intervalo Solcast con formato inesperado, ignorado: {e}")

    if not points:
        return

    _write_points(cfg, points)
    logger.info(f"Forecast media hora guardado en InfluxDB ({len(points)} slots)")


def _query_night_consumption(cfg: InfluxDBConfig, start_str: str, stop_str: str) -> float | None:
    """
    Media de night_consumption_kwh de stats_diarias en el rango [start, stop).

    Para vista de día devuelve el valor del único día; para semana/mes, la media.
    Devuelve None si no hay registros válidos (> 0) o la consulta falla.
    """
    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: {start_str}T00:00:00Z, stop: {stop_str}T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "stats_diarias" and r._field == "night_consumption_kwh")
  |> filter(fn: (r) => r._value > 0)
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            vals = [rec.get_value() for t in tables for rec in t.records if rec.get_value() is not None]
        return round(sum(vals) / len(vals), 3) if vals else None
    except Exception as e:
        logger.debug(f"No se pudo consultar night_consumption_kwh [{start_str}→{stop_str}]: {e}")
        return None


def get_solar_history_day(cfg: InfluxDBConfig, date_str: str) -> dict:
    """Historial de producción solar y forecast para un día (YYYY-MM-DD)."""
    if not cfg.enabled:
        return {}
    stop = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    result = _solar_history(cfg, date_str, stop)
    result["night_consumption_kwh"] = _query_night_consumption(cfg, date_str, stop)
    return result


def get_solar_history_range(cfg: InfluxDBConfig, start_str: str, end_str: str) -> dict:
    """Media horaria de producción solar y forecast en un rango de fechas (end exclusivo)."""
    if not cfg.enabled:
        return {}
    result = _solar_history(cfg, start_str, end_str)
    result["night_consumption_kwh"] = _query_night_consumption(cfg, start_str, end_str)
    return result


def get_last_real_solar_date(cfg: InfluxDBConfig, field: str = "real_kwh") -> date | None:
    """
    Día más reciente con dato en `solar_media_hora` para el campo indicado.

    Lo usa el backfill para saber desde qué día rellenar el hueco hasta ayer.
    Los timestamps son "hora local etiquetada como UTC", así que `.date()` da
    directamente el día local. Devuelve None si InfluxDB está deshabilitado, no
    hay datos en la ventana de búsqueda o la consulta falla.

    El parámetro `field` existe porque los campos se han ido añadiendo en momentos
    distintos: un día puede tener `real_kwh` desde hace meses y no tener `house_kwh`
    (añadido en v1.73). Preguntando solo por `real_kwh`, el backfill daría ese día
    por completo y no rellenaría nunca los campos nuevos — un hueco silencioso.
    """
    if not cfg.enabled:
        return None

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: -120d)
  |> filter(fn: (r) => r._measurement == "solar_media_hora" and r._field == "{field}")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 1)
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            for table in tables:
                for rec in table.records:
                    ts = rec.get_time()
                    if ts is not None:
                        return ts.date()
        return None
    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"No se pudo consultar el último día con {field} en solar_media_hora: {e}")
        return None


def get_charge_current_changes(cfg: InfluxDBConfig, day: date | None = None) -> list[dict]:
    """
    Devuelve los cambios de corriente registrados en `corriente_carga` para un día.

    `day`: fecha local (por defecto hoy). Los timestamps son "hora local etiquetada
    como UTC", así que el rango [día 00:00, día+1 00:00) en UTC corresponde al día
    local. Cada elemento incluye `hms` (HH:MM:SS local) listo para mostrar y todos
    los campos del punto. Orden ascendente por hora. Devuelve [] si InfluxDB está
    deshabilitado, no hay cambios o la consulta falla.
    """
    if not cfg.enabled:
        return []

    day = day or date.today()
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: {start}T00:00:00Z, stop: {end}T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "corriente_carga")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            recs = [rec.values for table in tables for rec in table.records]
    except ImportError:
        raise StorageError("influxdb-client no está instalado.")
    except Exception as e:
        logger.warning(f"Error consultando corriente_carga {start}: {e}")
        return []

    out = []
    for v in recs:
        ts = v.get("_time")
        if ts is None:
            continue
        out.append({
            "time":            ts.isoformat(),
            "hms":             ts.strftime("%H:%M:%S"),
            "mode":            v.get("mode"),
            "current_a":       v.get("current_a"),
            "calculated_a":    v.get("calculated_a"),
            "previous_a":      v.get("previous_a"),
            "delta_a":         v.get("delta_a"),
            "soc_pct":         v.get("soc_pct"),
            "battery_temp_c":  v.get("battery_temp_c"),
            "detail":          v.get("detail"),
            "verified":        v.get("verified"),
            "dry_run":         v.get("dry_run"),
        })
    # `mode` es un tag → el pivot de Flux devuelve una tabla por modo y su
    # sort(_time) ordena solo dentro de cada tabla; al aplanar en Python la lista
    # queda agrupada por modo, no por hora. Reordenar aquí por hora ascendente
    # (los timestamps son ISO UTC con el mismo offset → orden lexicográfico = cronológico).
    out.sort(key=lambda r: r["time"])
    return out


def _solar_history(cfg: InfluxDBConfig, start_str: str, end_str: str) -> dict:
    """Consulta solar_media_hora y devuelve medias horarias de real y forecast."""
    from collections import defaultdict

    query = f"""
from(bucket: "{cfg.bucket}")
  |> range(start: {start_str}T00:00:00Z, stop: {end_str}T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "solar_media_hora")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    try:
        from influxdb_client import InfluxDBClient
        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            tables = client.query_api().query(query, org=cfg.org)
            recs = [rec.values for table in tables for rec in table.records]
    except Exception as e:
        logger.warning(f"Error consultando solar_media_hora {start_str}→{end_str}: {e}")
        return {
            "hours": [], "p50_kw": [], "p10_kw": [], "p90_kw": [], "real_kw": [],
            "total_p50_kwh": 0.0, "total_real_kwh": 0.0,
            "has_real": False, "has_forecast": False, "days_with_data": 0,
        }

    # Acumular por (día, hora) — luego promediamos entre días
    by_day: dict = defaultdict(lambda: defaultdict(lambda: {
        "real": 0.0, "p50": 0.0, "p10": 0.0, "p90": 0.0,
        "has_real": False, "has_fc": False,
    }))

    for v in recs:
        ts = v.get("_time")
        if ts is None:
            continue
        h   = ts.hour
        day = ts.date().isoformat()
        s   = by_day[day][h]

        real = v.get("real_kwh")
        p50  = v.get("forecast_p50_kwh")
        p10  = v.get("forecast_p10_kwh")
        p90  = v.get("forecast_p90_kwh")

        if real is not None and real >= 0:
            s["real"] += real
            s["has_real"] = True
        if p50 is not None:
            s["p50"] += p50
            s["p10"] += (p10 or 0.0)
            s["p90"] += (p90 or 0.0)
            s["has_fc"] = True

    all_hours: set = set()
    for day_data in by_day.values():
        all_hours.update(day_data.keys())
    all_hours_sorted = sorted(all_hours)

    if not all_hours_sorted:
        return {
            "hours": [], "p50_kw": [], "p10_kw": [], "p90_kw": [], "real_kw": [],
            "total_p50_kwh": 0.0, "total_real_kwh": 0.0,
            "has_real": False, "has_forecast": False, "days_with_data": 0,
        }

    days_with_real = {d for d, dh in by_day.items() if any(s["has_real"] for s in dh.values())}
    days_with_fc   = {d for d, dh in by_day.items() if any(s["has_fc"]   for s in dh.values())}

    p50_kw, p10_kw, p90_kw, real_kw = [], [], [], []
    for h in all_hours_sorted:
        fc_slots = [by_day[d][h] for d in by_day if by_day[d][h]["has_fc"]]
        re_slots = [by_day[d][h] for d in by_day if by_day[d][h]["has_real"]]

        if fc_slots:
            n = len(fc_slots)
            p50_kw.append(round(sum(s["p50"] for s in fc_slots) / n, 4))
            p10_kw.append(round(sum(s["p10"] for s in fc_slots) / n, 4))
            p90_kw.append(round(sum(s["p90"] for s in fc_slots) / n, 4))
        else:
            p50_kw.append(None); p10_kw.append(None); p90_kw.append(None)

        real_kw.append(
            round(sum(s["real"] for s in re_slots) / len(re_slots), 4) if re_slots else None
        )

    total_p50  = round(sum(v for v in p50_kw  if v is not None), 3)
    total_real = round(sum(v for v in real_kw if v is not None), 3)

    return {
        "hours":          all_hours_sorted,
        "p50_kw":         p50_kw,
        "p10_kw":         p10_kw,
        "p90_kw":         p90_kw,
        "real_kw":        real_kw,
        "total_p50_kwh":  total_p50,
        "total_real_kwh": total_real,
        "has_real":       len(days_with_real) > 0,
        "has_forecast":   len(days_with_fc) > 0,
        "days_with_data": max(len(days_with_real), len(days_with_fc)),
    }


def _write_points(cfg: InfluxDBConfig, points: list[dict]) -> None:
    """Escribe una lista de puntos en InfluxDB en un único batch."""
    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
            write_api = client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=cfg.bucket, record=points)

    except ImportError:
        raise StorageError(
            "influxdb-client no está instalado. "
            "Añade 'influxdb-client' al requirements.txt"
        )
    except Exception as e:
        raise StorageError(f"Error escribiendo en InfluxDB: {e}") from e


def _write_point(cfg: InfluxDBConfig, point: dict) -> None:
    _write_points(cfg, [point])
