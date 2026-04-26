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

is_valley_day(d, weekend_days, holidays) → bool
    ¿Es el día d un día de tarifa valle todo el día?
    (fin de semana o festivo)

reference_date(night_cutoff_hour) → date
    Fecha de referencia nocturna. Si se ejecuta antes de night_cutoff_hour
    actúa como si fuera la noche del día anterior.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional


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
    forecast_day1: SolarForecast
    forecast_day2: SolarForecast

    soc_actual_pct: float
    battery_capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float

    daily_consumption_kwh: float
    night_consumption_kwh: float
    safety_margin_kwh: float

    risk_factor: float

    weekend_days: list = field(default_factory=lambda: [5, 6])
    holidays: list = field(default_factory=list)

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
    valley_day_skip: bool = False
    dry_run: bool = False


@dataclass
class DischargeDecision:
    """Resultado de decide_discharge."""
    discharge_blocked: bool
    reason: str = ""
    energy_end_day1_kwh: float = 0.0
    deficit_day2_kwh: float = 0.0


# ---------------------------------------------------------------------------
# Funciones auxiliares
# ---------------------------------------------------------------------------

def is_valley_day(d: date, weekend_days: list, holidays: list) -> bool:
    """Devuelve True si el día d tiene tarifa valle todo el día."""
    if d.weekday() in weekend_days:
        return True
    return d.isoformat() in holidays


def _solar_effective(forecast: SolarForecast, risk_factor: float) -> float:
    return forecast.p10 * risk_factor + forecast.p50 * (1.0 - risk_factor)


def reference_date(night_cutoff_hour: int = 8) -> date:
    """
    Fecha de referencia para el ciclo nocturno.

    Si la hora actual es anterior a night_cutoff_hour, el programa se está
    ejecutando en la madrugada del día siguiente al que corresponde la decisión,
    por lo que se retrocede un día.

    Ejemplos (night_cutoff_hour=8):
      23:55 del jueves  → referencia = jueves  → día1 = viernes  ✓
      00:30 del viernes → referencia = jueves  → día1 = viernes  ✓
      10:00 del viernes → referencia = viernes → día1 = sábado   ✓
    """
    now = datetime.now()
    if now.hour < night_cutoff_hour:
        return now.date() - timedelta(days=1)
    return now.date()


# ---------------------------------------------------------------------------
# decide_charge
# ---------------------------------------------------------------------------

def decide_charge(
    inp: DecisionInput,
    dry_run: bool = False,
    night_cutoff_hour: int = 8,
    _ref_date: Optional[date] = None,
) -> ChargeDecision:
    """
    Decide si cargar la batería desde la red esta noche y hasta qué SOC.

    Args:
        inp:               datos de entrada del algoritmo.
        dry_run:           si True, no modifica el inversor.
        night_cutoff_hour: hora límite de madrugada (leída de config.yaml).
        _ref_date:         fecha de referencia; si se pasa, omite reference_date()
                           (uso exclusivo en tests para evitar dependencia de datetime).
    """
    ref      = _ref_date if _ref_date is not None else reference_date(night_cutoff_hour)
    tomorrow = ref + timedelta(days=1)

    energy_stored = (inp.soc_actual_pct / 100.0) * inp.battery_capacity_kwh
    energy_min    = (inp.min_soc_pct    / 100.0) * inp.battery_capacity_kwh

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

    solar_effective = _solar_effective(inp.forecast_day1, inp.risk_factor)
    energy_at_dawn  = max(energy_min, energy_stored - inp.night_consumption_kwh)
    energy_usable   = max(0.0, energy_at_dawn - energy_min)
    needed_for_day  = max(0.0, inp.daily_consumption_kwh + inp.safety_margin_kwh - solar_effective)
    deficit         = max(0.0, needed_for_day - energy_usable)

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

    target_kwh_raw = energy_min + needed_for_day
    target_soc_raw = (target_kwh_raw / inp.battery_capacity_kwh) * 100.0
    target_soc     = max(inp.min_soc_pct, min(inp.max_soc_pct, target_soc_raw))
    clamped        = abs(target_soc - target_soc_raw) > 0.01
    target_kwh     = (target_soc / 100.0) * inp.battery_capacity_kwh

    return ChargeDecision(
        charge_needed=True,
        target_soc_pct=round(target_soc, 1),
        target_kwh=round(target_kwh, 2),
        to_charge_kwh=round(max(0.0, target_kwh - energy_stored), 2),
        solar_effective_kwh=round(solar_effective, 2),
        energy_stored_kwh=round(energy_stored, 2),
        energy_at_dawn_kwh=round(target_kwh, 2),
        deficit_kwh=round(deficit, 2),
        clamped=clamped,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# decide_discharge
# ---------------------------------------------------------------------------

def decide_discharge(
    inp: DecisionInput,
    night_cutoff_hour: int = 8,
    _ref_date: Optional[date] = None,
) -> DischargeDecision:
    """
    Decide si bloquear la descarga de la batería mañana (día 1).

    Args:
        inp:               datos de entrada del algoritmo.
        night_cutoff_hour: hora límite de madrugada (leída de config.yaml).
        _ref_date:         fecha de referencia; si se pasa, omite reference_date()
                           (uso exclusivo en tests).
    """
    ref      = _ref_date if _ref_date is not None else reference_date(night_cutoff_hour)
    tomorrow = ref + timedelta(days=1)

    if not is_valley_day(tomorrow, inp.weekend_days, inp.holidays):
        return DischargeDecision(
            discharge_blocked=False,
            reason="mañana es día laborable — descarga libre",
        )

    energy_stored   = (inp.soc_actual_pct / 100.0) * inp.battery_capacity_kwh
    energy_min      = (inp.min_soc_pct    / 100.0) * inp.battery_capacity_kwh

    solar_day1  = _solar_effective(inp.forecast_day1, inp.risk_factor)
    solar_day2  = _solar_effective(inp.forecast_day2, inp.risk_factor)
    needed_day2 = max(0.0, inp.daily_consumption_kwh + inp.safety_margin_kwh - solar_day2)

    # Criterio de decisión: escenario sin bloqueo (conservador)
    energy_end_no_block    = max(energy_min, energy_stored + solar_day1 - inp.daily_consumption_kwh)
    energy_usable_no_block = max(0.0, energy_end_no_block - energy_min)
    deficit_no_block       = max(0.0, needed_day2 - energy_usable_no_block)

    if deficit_no_block == 0.0:
        return DischargeDecision(
            discharge_blocked=False,
            reason="solar de 2 días suficiente — descarga libre",
            energy_end_day1_kwh=round(energy_end_no_block, 2),
            deficit_day2_kwh=0.0,
        )

    # Con bloqueo el consumo nocturno (00:01–07:59) pasa a la red → batería retiene night_consumption_kwh más
    energy_end_day1    = max(energy_min, energy_stored + solar_day1 - (inp.daily_consumption_kwh - inp.night_consumption_kwh))
    energy_usable_day2 = max(0.0, energy_end_day1 - energy_min)
    deficit_day2       = max(0.0, needed_day2 - energy_usable_day2)

    return DischargeDecision(
        discharge_blocked=True,
        reason=f"déficit día 2 = {round(deficit_no_block, 2)} kWh sin bloqueo — reservar batería",
        energy_end_day1_kwh=round(energy_end_day1, 2),
        deficit_day2_kwh=round(deficit_day2, 2),
    )


# ---------------------------------------------------------------------------
# Resumen para el log
# ---------------------------------------------------------------------------

def charge_summary(
    inp: DecisionInput,
    result: ChargeDecision,
    night_cutoff_hour: int = 8,
    _ref_date: Optional[date] = None,
) -> str:
    """Resumen legible de decide_charge para el log."""
    dias     = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
    ref      = _ref_date if _ref_date is not None else reference_date(night_cutoff_hour)
    tomorrow = ref + timedelta(days=1)
    dia_es   = dias[tomorrow.weekday()]

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
        lines.append(f"    Carga desde red              : {result.to_charge_kwh} kWh")
        if result.clamped:
            lines.append(f"    (limitado por min={inp.min_soc_pct}% / max={inp.max_soc_pct}%)")
    else:
        lines.append("  → NO cargar de red (batería suficiente)")
    if result.dry_run:
        lines.append("  [DRY RUN — no se modificará el inversor]")
    return "\n".join(lines)


def discharge_summary(
    inp: DecisionInput,
    result: DischargeDecision,
    night_cutoff_hour: int = 8,
    _ref_date: Optional[date] = None,
) -> str:
    """Resumen legible de decide_discharge para el log."""
    dias     = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
    ref      = _ref_date if _ref_date is not None else reference_date(night_cutoff_hour)
    tomorrow = ref + timedelta(days=1)
    day2     = tomorrow + timedelta(days=1)
    dia1_es  = dias[tomorrow.weekday()]
    dia2_es  = dias[day2.weekday()]

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
# One-liners para el log de producción (INFO level)
# ---------------------------------------------------------------------------

def charge_oneliner(
    inp: DecisionInput,
    result: ChargeDecision,
    night_cutoff_hour: int = 8,
    _ref_date: Optional[date] = None,
) -> str:
    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    ref      = _ref_date if _ref_date is not None else reference_date(night_cutoff_hour)
    tomorrow = ref + timedelta(days=1)
    dia_es   = dias[tomorrow.weekday()]

    if result.valley_day_skip:
        return (
            f"[CARGA] NO · valle {dia_es} {tomorrow.strftime('%d/%m')} · "
            f"SOC {inp.soc_actual_pct}% · "
            f"solar p10/p50 {inp.forecast_day1.p10}/{inp.forecast_day1.p50} kWh"
        )
    if not result.charge_needed:
        return (
            f"[CARGA] NO · solar suficiente · "
            f"SOC {inp.soc_actual_pct}% → alba {result.energy_at_dawn_kwh} kWh · "
            f"solar efectiva {result.solar_effective_kwh} kWh · déficit 0 kWh"
        )
    return (
        f"[CARGA] SÍ → SOC {result.target_soc_pct}% · "
        f"+{result.to_charge_kwh} kWh desde red · "
        f"SOC {inp.soc_actual_pct}% → alba {result.energy_at_dawn_kwh} kWh · "
        f"solar efectiva {result.solar_effective_kwh} kWh · déficit {result.deficit_kwh} kWh"
    )


def discharge_oneliner(
    inp: DecisionInput,
    result: DischargeDecision,
    night_cutoff_hour: int = 8,
    _ref_date: Optional[date] = None,
) -> str:
    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    ref      = _ref_date if _ref_date is not None else reference_date(night_cutoff_hour)
    day2     = ref + timedelta(days=2)

    if not result.discharge_blocked:
        if "laborable" in result.reason:
            return "[DESCARGA] LIBRE · mañana es laborable"
        solar_day2 = round(_solar_effective(inp.forecast_day2, inp.risk_factor), 2)
        return (
            f"[DESCARGA] LIBRE · "
            f"solar {dias[day2.weekday()]} {solar_day2} kWh · "
            f"déficit 0 kWh · energía fin día1 {result.energy_end_day1_kwh} kWh"
        )
    return (
        f"[DESCARGA] BLOQUEADA · "
        f"déficit día2 {result.deficit_day2_kwh} kWh · "
        f"energía fin día1 {result.energy_end_day1_kwh} kWh"
    )


# ---------------------------------------------------------------------------
# Compatibilidad con código anterior
# ---------------------------------------------------------------------------

def calculate_charge_target(inp: DecisionInput, dry_run: bool = False) -> ChargeDecision:
    return decide_charge(inp, dry_run)


def decision_summary(inp: DecisionInput, result: ChargeDecision) -> str:
    return charge_summary(inp, result)