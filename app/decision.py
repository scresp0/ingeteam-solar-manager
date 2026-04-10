"""
decision.py — lógica de cálculo del nivel de carga de baterías.

Algoritmo:
    solar_efectiva    = p10 * risk_factor + p50 * (1 - risk_factor)
    energia_actual    = (soc_actual / 100) * capacidad_kwh
    energia_amanecer  = energia_actual - consumo_nocturno_kwh  (lo que queda al acabar el valle)
    necesario_dia     = consumo_diario + margen_seguridad - solar_efectiva
    deficit           = necesario_dia - energia_amanecer

    Si deficit <= 0:
        → charge_needed = False  (desactivar carga de red)
    Si deficit > 0:
        → charge_needed = True
        → soc_objetivo = clamp(
              (energia_amanecer + deficit) / capacidad * 100,
              min_soc_pct, max_soc_pct
          )
"""

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class SolarForecast:
    """Previsión de producción solar para el día siguiente (kWh)."""
    p10: float
    p50: float
    p90: float


@dataclass
class DecisionInput:
    """Todos los datos necesarios para tomar la decisión de carga."""
    forecast: SolarForecast
    soc_actual_pct: float
    battery_capacity_kwh: float
    daily_consumption_kwh: float
    night_consumption_kwh: float   # consumo en horario valle (00:00-08:00)
    risk_factor: float
    min_soc_pct: float
    max_soc_pct: float
    safety_margin_kwh: float
    weekend_days: list = None      # días de fin de semana (0=lunes, 5=sábado, 6=domingo)
    holidays: list = None          # festivos como strings "YYYY-MM-DD"


@dataclass
class DecisionResult:
    """Resultado del cálculo, con desglose para logging y auditoría."""
    charge_needed: bool            # False = desactivar, True = cargar hasta target_soc_pct
    target_soc_pct: float          # SOC objetivo (solo relevante si charge_needed=True)
    target_kwh: float
    to_charge_kwh: float           # kWh a cargar desde la red
    solar_effective_kwh: float
    energy_stored_kwh: float       # energía actual en batería
    energy_at_dawn_kwh: float      # energía estimada al acabar el valle (08:00)
    deficit_kwh: float
    clamped: bool
    dry_run: bool
    weekend_skip: bool = False     # True si se omite la carga por ser fin de semana


def _is_tomorrow_weekend_or_holiday(inp: DecisionInput) -> bool:
    """Devuelve True si mañana es fin de semana o festivo."""
    tomorrow = date.today() + timedelta(days=1)
    weekend_days = inp.weekend_days or [5, 6]
    if tomorrow.weekday() in weekend_days:
        return True
    holidays = inp.holidays or []
    if tomorrow.isoformat() in holidays:
        return True
    return False


def calculate_charge_target(inp: DecisionInput, dry_run: bool = False) -> DecisionResult:
    """
    Calcula si hay que cargar de red y a qué nivel.

    Returns:
        DecisionResult con charge_needed y target_soc_pct
    """
    # 0. Si mañana es fin de semana o festivo, no cargar nunca de red
    #    (tarifa valle todo el día → ineficiente cargar con ~10% pérdidas)
    if _is_tomorrow_weekend_or_holiday(inp):
        return DecisionResult(
            charge_needed=False,
            target_soc_pct=0.0,
            target_kwh=0.0,
            to_charge_kwh=0.0,
            solar_effective_kwh=0.0,
            energy_stored_kwh=round((inp.soc_actual_pct / 100.0) * inp.battery_capacity_kwh, 2),
            energy_at_dawn_kwh=0.0,
            deficit_kwh=0.0,
            clamped=False,
            dry_run=dry_run,
            weekend_skip=True,
        )

    # 1. Producción solar efectiva interpolada según risk_factor
    solar_effective = (
        inp.forecast.p10 * inp.risk_factor
        + inp.forecast.p50 * (1.0 - inp.risk_factor)
    )

    # 2. Energía actual en batería
    energy_stored = (inp.soc_actual_pct / 100.0) * inp.battery_capacity_kwh

    # 3. Energía mínima que el inversor mantiene siempre (min_soc configurado)
    energy_min = (inp.min_soc_pct / 100.0) * inp.battery_capacity_kwh

    # 4. Energía estimada al amanecer sin cargar de red:
    #    la batería se descarga durante el valle — no puede bajar del mínimo
    energy_at_dawn_no_charge = max(energy_min, energy_stored - inp.night_consumption_kwh)

    # 5. Energía utilizable al amanecer sin cargar (por encima del mínimo)
    energy_usable_no_charge = max(0.0, energy_at_dawn_no_charge - energy_min)

    # 6. Energía necesaria para cubrir el día (consumo - solar)
    needed_for_day = max(0.0,
        inp.daily_consumption_kwh + inp.safety_margin_kwh - solar_effective
    )

    # 7. Déficit sin cargar de red
    deficit = max(0.0, needed_for_day - energy_usable_no_charge)

    # Nota: cuando charge_needed=True, el consumo nocturno lo cubre la red
    # (el inversor mantiene la batería en SOC Grid durante el valle).
    # Por tanto energy_at_dawn = SOC objetivo, y el déficit se calcula
    # directamente como needed_for_day sin restar consumo nocturno.

    # 6. Decidir si hay que cargar
    charge_needed = deficit > 0.0

    if not charge_needed:
        return DecisionResult(
            charge_needed=False,
            target_soc_pct=0.0,
            target_kwh=0.0,
            to_charge_kwh=0.0,
            solar_effective_kwh=round(solar_effective, 2),
            energy_stored_kwh=round(energy_stored, 2),
            energy_at_dawn_kwh=round(energy_at_dawn_no_charge, 2),
            deficit_kwh=0.0,
            clamped=False,
            dry_run=dry_run,
            weekend_skip=False,
        )

    # 8. SOC objetivo cuando charge_needed=True:
    #    consumo nocturno cubierto por la red → batería llega al amanecer en SOC Grid
    #    solo necesitamos energy_min + needed_for_day
    target_kwh_raw = energy_min + needed_for_day
    target_soc_raw = (target_kwh_raw / inp.battery_capacity_kwh) * 100.0
    target_soc = max(inp.min_soc_pct, min(inp.max_soc_pct, target_soc_raw))
    clamped = target_soc != target_soc_raw
    target_kwh = (target_soc / 100.0) * inp.battery_capacity_kwh

    return DecisionResult(
        charge_needed=True,
        target_soc_pct=round(target_soc, 1),
        target_kwh=round(target_kwh, 2),
        to_charge_kwh=round(deficit, 2),
        solar_effective_kwh=round(solar_effective, 2),
        energy_stored_kwh=round(energy_stored, 2),
        energy_at_dawn_kwh=round(energy_at_dawn_no_charge, 2),
        deficit_kwh=round(deficit, 2),
        clamped=clamped,
        dry_run=dry_run,
        weekend_skip=False,
    )


def decision_summary(inp: DecisionInput, result: DecisionResult) -> str:
    """Devuelve un resumen legible del cálculo para el log."""
    lines = [
        "=== Cálculo de carga ===",
        f"  Producción solar p10/p50/p90   : {inp.forecast.p10}/{inp.forecast.p50}/{inp.forecast.p90} kWh",
        f"  Solar efectiva (risk={inp.risk_factor}) : {result.solar_effective_kwh} kWh",
        f"  Energía actual en batería      : {result.energy_stored_kwh} kWh  (SOC {inp.soc_actual_pct}%)",
        f"  Mínimo SOC configurado         : {inp.min_soc_pct}% ({round((inp.min_soc_pct/100)*inp.battery_capacity_kwh,2)} kWh)",
        f"  Consumo nocturno valle         : {inp.night_consumption_kwh} kWh",
        f"  Energía estimada al amanecer   : {result.energy_at_dawn_kwh} kWh  (sin cargar de red)",
        f"  Consumo diario + margen        : {inp.daily_consumption_kwh} + {inp.safety_margin_kwh} kWh",
        f"  Solar efectiva                 : {result.solar_effective_kwh} kWh",
        f"  Déficit esperado               : {result.deficit_kwh} kWh",
    ]
    if result.charge_needed:
        lines.append(f"  → CARGAR de red: SOC objetivo = {result.target_soc_pct}% ({result.target_kwh} kWh)")
        if result.clamped:
            lines.append(f"    (limitado por min={inp.min_soc_pct}% / max={inp.max_soc_pct}%)")
    elif result.weekend_skip:
        tomorrow = date.today() + timedelta(days=1)
        dias = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
        dia_es = dias[tomorrow.weekday()]
        lines.append(f"  → NO cargar de red (mañana {dia_es} {tomorrow.strftime('%d/%m')} es fin de semana/festivo — tarifa valle todo el día)")
    else:
        lines.append(f"  → NO cargar de red (batería suficiente para el día)")
    if result.dry_run:
        lines.append("  [DRY RUN — no se modificará el inversor]")
    return "\n".join(lines)
