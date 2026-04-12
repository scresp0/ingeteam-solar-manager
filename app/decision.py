"""
decision.py — lógica de decisión de carga y descarga de baterías.

Dos funciones independientes:

  decide_charge(inp) → ChargeDecision
    ¿Cargo batería desde la red esta noche y hasta qué SOC?
    Solo mira el día 1 (mañana). Si mañana es día valle, nunca carga.

  decide_discharge(inp) → DischargeDecision
    ¿Bloqueo la descarga de la batería mañana?
    Solo aplica si mañana es día valle. Mira 2 días para decidir
    si reservar la batería para el día laborable siguiente.

is_valley_day(d, tariff) → bool
    ¿Es el día d un día de tarifa valle todo el día?
    (fin de semana o festivo)
"""

from dataclasses import dataclass, field
from datetime import date, timedelta


# ---------------------------------------------------------------------------
# Estructuras de datos
# ---------------------------------------------------------------------------

@dataclass
class SolarForecast:
    """Previsión de producción solar para un día (kWh)."""
    p10: float
    p50: float
    p90: float


@dataclass
class DecisionInput:
    """Todos los datos necesarios para tomar las decisiones de carga/descarga."""
    # Previsión solar
    forecast_day1: SolarForecast       # mañana
    forecast_day2: SolarForecast       # pasado mañana

    # Estado del inversor
    soc_actual_pct: float
    battery_capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float

    # Parámetros de consumo
    daily_consumption_kwh: float
    night_consumption_kwh: float       # consumo en horario valle (00:00-08:00)
    safety_margin_kwh: float

    # Parámetros del algoritmo
    risk_factor: float

    # Tarifa — para is_valley_day
    weekend_days: list = field(default_factory=lambda: [5, 6])
    holidays: list = field(default_factory=list)

    # Compatibilidad con código anterior — forecast = día 1
    @property
    def forecast(self) -> SolarForecast:
        return self.forecast_day1


@dataclass
class ChargeDecision:
    """Resultado de decide_charge."""
    charge_needed: bool
    target_soc_pct: float
    target_kwh: float
    to_charge_kwh: float
    solar_effective_kwh: float
    energy_stored_kwh: float
    energy_at_dawn_kwh: float
    deficit_kwh: float
    clamped: bool
    valley_day_skip: bool = False      # True si no se carga por ser día valle
    dry_run: bool = False


@dataclass
class DischargeDecision:
    """Resultado de decide_discharge."""
    discharge_blocked: bool            # True = bloquear descarga durante el valle
    reason: str = ""                   # motivo para el log
    energy_end_day1_kwh: float = 0.0  # energía estimada al final del día 1
    deficit_day2_kwh: float = 0.0     # déficit estimado del día 2


# ---------------------------------------------------------------------------
# Función auxiliar
# ---------------------------------------------------------------------------

def is_valley_day(d: date, weekend_days: list, holidays: list) -> bool:
    """
    Devuelve True si el día d tiene tarifa valle todo el día.
    Eso ocurre cuando es fin de semana o festivo nacional.
    """
    if d.weekday() in weekend_days:
        return True
    return d.isoformat() in holidays


def _solar_effective(forecast: SolarForecast, risk_factor: float) -> float:
    """Calcula la solar efectiva con el mismo criterio en ambas funciones."""
    return forecast.p10 * risk_factor + forecast.p50 * (1.0 - risk_factor)


# ---------------------------------------------------------------------------
# decide_charge
# ---------------------------------------------------------------------------

def decide_charge(inp: DecisionInput, dry_run: bool = False) -> ChargeDecision:
    """
    Decide si cargar la batería desde la red esta noche y hasta qué SOC.

    Regla principal: solo carga si mañana (día 1) es día laborable.
    Si mañana es valle (fin de semana o festivo), no tiene sentido cargar
    con las pérdidas de ~10% — la decisión se tomará en su momento.
    """
    tomorrow = date.today() + timedelta(days=1)
    energy_stored = (inp.soc_actual_pct / 100.0) * inp.battery_capacity_kwh
    energy_min = (inp.min_soc_pct / 100.0) * inp.battery_capacity_kwh

    # Si mañana es día valle → nunca cargar
    if is_valley_day(tomorrow, inp.weekend_days, inp.holidays):
        return ChargeDecision(
            charge_needed=False,
            target_soc_pct=0.0,
            target_kwh=0.0,
            to_charge_kwh=0.0,
            solar_effective_kwh=round(_solar_effective(inp.forecast_day1, inp.risk_factor), 2),
            energy_stored_kwh=round(energy_stored, 2),
            energy_at_dawn_kwh=round(max(energy_min, energy_stored - inp.night_consumption_kwh), 2),
            deficit_kwh=0.0,
            clamped=False,
            valley_day_skip=True,
            dry_run=dry_run,
        )

    # Mañana es laborable → calcular si hay déficit
    solar_effective = _solar_effective(inp.forecast_day1, inp.risk_factor)
    energy_at_dawn = max(energy_min, energy_stored - inp.night_consumption_kwh)
    energy_usable = max(0.0, energy_at_dawn - energy_min)
    needed_for_day = max(0.0,
        inp.daily_consumption_kwh + inp.safety_margin_kwh - solar_effective
    )
    deficit = max(0.0, needed_for_day - energy_usable)

    if deficit == 0.0:
        return ChargeDecision(
            charge_needed=False,
            target_soc_pct=0.0,
            target_kwh=0.0,
            to_charge_kwh=0.0,
            solar_effective_kwh=round(solar_effective, 2),
            energy_stored_kwh=round(energy_stored, 2),
            energy_at_dawn_kwh=round(energy_at_dawn, 2),
            deficit_kwh=0.0,
            clamped=False,
            dry_run=dry_run,
        )

    # Hay déficit → calcular target_soc
    # Cuando se carga de red, la batería descansa en SOC objetivo (la red cubre el valle)
    target_kwh_raw = energy_min + needed_for_day
    target_soc_raw = (target_kwh_raw / inp.battery_capacity_kwh) * 100.0
    target_soc = max(inp.min_soc_pct, min(inp.max_soc_pct, target_soc_raw))
    clamped = abs(target_soc - target_soc_raw) > 0.01
    target_kwh = (target_soc / 100.0) * inp.battery_capacity_kwh

    return ChargeDecision(
        charge_needed=True,
        target_soc_pct=round(target_soc, 1),
        target_kwh=round(target_kwh, 2),
        to_charge_kwh=round(deficit, 2),
        solar_effective_kwh=round(solar_effective, 2),
        energy_stored_kwh=round(energy_stored, 2),
        energy_at_dawn_kwh=round(energy_at_dawn, 2),
        deficit_kwh=round(deficit, 2),
        clamped=clamped,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# decide_discharge
# ---------------------------------------------------------------------------

def decide_discharge(inp: DecisionInput) -> DischargeDecision:
    """
    Decide si bloquear la descarga de la batería mañana (día 1).

    Solo tiene sentido bloquear si mañana es día valle — si no,
    la descarga siempre es libre.

    Cuando mañana es valle, simula la descarga libre del día 1 y calcula
    si la energía restante cubre el día 2. Si no cubre, bloquea la descarga
    para reservar la batería para el día laborable siguiente.
    """
    tomorrow = date.today() + timedelta(days=1)

    # Si mañana no es valle, la descarga siempre es libre
    if not is_valley_day(tomorrow, inp.weekend_days, inp.holidays):
        return DischargeDecision(
            discharge_blocked=False,
            reason="mañana es día laborable — descarga libre",
        )

    energy_stored = (inp.soc_actual_pct / 100.0) * inp.battery_capacity_kwh
    energy_min = (inp.min_soc_pct / 100.0) * inp.battery_capacity_kwh

    # Simular descarga libre del día 1:
    # La batería se carga con la solar y se descarga con el consumo,
    # sin bajar del mínimo configurado
    solar_day1 = _solar_effective(inp.forecast_day1, inp.risk_factor)
    energy_end_day1 = max(
        energy_min,
        energy_stored + solar_day1 - inp.daily_consumption_kwh
    )

    # Calcular si la energía restante cubre el día 2
    solar_day2 = _solar_effective(inp.forecast_day2, inp.risk_factor)
    energy_usable_day2 = max(0.0, energy_end_day1 - energy_min)
    needed_day2 = max(0.0,
        inp.daily_consumption_kwh + inp.safety_margin_kwh - solar_day2
    )
    deficit_day2 = max(0.0, needed_day2 - energy_usable_day2)

    if deficit_day2 == 0.0:
        return DischargeDecision(
            discharge_blocked=False,
            reason="solar de 2 días suficiente — descarga libre",
            energy_end_day1_kwh=round(energy_end_day1, 2),
            deficit_day2_kwh=0.0,
        )

    return DischargeDecision(
        discharge_blocked=True,
        reason=f"déficit día 2 = {round(deficit_day2, 2)} kWh — reservar batería",
        energy_end_day1_kwh=round(energy_end_day1, 2),
        deficit_day2_kwh=round(deficit_day2, 2),
    )


# ---------------------------------------------------------------------------
# Resumen para el log
# ---------------------------------------------------------------------------

def charge_summary(inp: DecisionInput, result: ChargeDecision) -> str:
    """Resumen legible de decide_charge para el log."""
    dias = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
    tomorrow = date.today() + timedelta(days=1)
    dia_es = dias[tomorrow.weekday()]

    lines = [
        "=== Decisión de carga ===",
        f"  Día 1 (mañana {dia_es} {tomorrow.strftime('%d/%m')})",
        f"  Producción solar p10/p50/p90   : {inp.forecast_day1.p10}/{inp.forecast_day1.p50}/{inp.forecast_day1.p90} kWh",
        f"  Solar efectiva (risk={inp.risk_factor}) : {result.solar_effective_kwh} kWh",
        f"  Energía actual en batería      : {result.energy_stored_kwh} kWh  (SOC {inp.soc_actual_pct}%)",
        f"  Mínimo SOC configurado         : {inp.min_soc_pct}% ({round((inp.min_soc_pct/100)*inp.battery_capacity_kwh,2)} kWh)",
        f"  Consumo nocturno valle         : {inp.night_consumption_kwh} kWh",
        f"  Energía estimada al amanecer   : {result.energy_at_dawn_kwh} kWh",
        f"  Consumo diario + margen        : {inp.daily_consumption_kwh} + {inp.safety_margin_kwh} kWh",
        f"  Déficit esperado               : {result.deficit_kwh} kWh",
    ]
    if result.valley_day_skip:
        lines.append(f"  → NO cargar (mañana {dia_es} {tomorrow.strftime('%d/%m')} es valle todo el día)")
    elif result.charge_needed:
        lines.append(f"  → CARGAR de red: SOC objetivo = {result.target_soc_pct}% ({result.target_kwh} kWh)")
        if result.clamped:
            lines.append(f"    (limitado por min={inp.min_soc_pct}% / max={inp.max_soc_pct}%)")
    else:
        lines.append("  → NO cargar de red (batería suficiente)")
    if result.dry_run:
        lines.append("  [DRY RUN — no se modificará el inversor]")
    return "\n".join(lines)


def discharge_summary(inp: DecisionInput, result: DischargeDecision) -> str:
    """Resumen legible de decide_discharge para el log."""
    dias = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
    tomorrow = date.today() + timedelta(days=1)
    day2 = tomorrow + timedelta(days=1)
    dia1_es = dias[tomorrow.weekday()]
    dia2_es = dias[day2.weekday()]

    lines = [
        "=== Decisión de descarga ===",
        f"  Día 1: {dia1_es} {tomorrow.strftime('%d/%m')} | Día 2: {dia2_es} {day2.strftime('%d/%m')}",
        f"  Solar efectiva día 2           : {round(_solar_effective(inp.forecast_day2, inp.risk_factor), 2)} kWh",
        f"  Energía estimada fin día 1     : {result.energy_end_day1_kwh} kWh",
        f"  Déficit día 2                  : {result.deficit_day2_kwh} kWh",
    ]
    if result.discharge_blocked:
        lines.append("  → BLOQUEAR descarga mañana (6.3.2: 00:01–07:59)")
    else:
        lines.append("  → Descarga libre mañana")
    lines.append(f"  Motivo: {result.reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compatibilidad con código anterior
# ---------------------------------------------------------------------------

def calculate_charge_target(inp: DecisionInput, dry_run: bool = False) -> ChargeDecision:
    """Alias de decide_charge para compatibilidad."""
    return decide_charge(inp, dry_run)


def decision_summary(inp: DecisionInput, result: ChargeDecision) -> str:
    """Alias de charge_summary para compatibilidad."""
    return charge_summary(inp, result)
