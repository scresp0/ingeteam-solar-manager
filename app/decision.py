"""
decision.py — lógica de cálculo del nivel de carga de baterías.

Dado el estado actual de la batería y la previsión solar del día siguiente,
calcula el nivel de SOC (%) al que hay que cargar durante el horario valle.

Fórmula central:
    solar_efectiva  = p10 * risk_factor + p50 * (1 - risk_factor)
    energia_actual  = (soc_actual / 100) * capacidad_kwh
    deficit         = max(0, consumo_diario + margen_seguridad - solar_efectiva)
    a_cargar        = max(0, deficit - energia_actual)
    soc_objetivo    = clamp(
                        (energia_actual + a_cargar) / capacidad_kwh * 100,
                        min_soc_pct,
                        max_soc_pct
                      )
"""

from dataclasses import dataclass


@dataclass
class SolarForecast:
    """Previsión de producción solar para el día siguiente (kWh)."""
    p10: float   # pesimista  (10% de probabilidad de quedar por debajo)
    p50: float   # esperado   (mediana)
    p90: float   # optimista  (90% de probabilidad de quedar por debajo)


@dataclass
class DecisionInput:
    """Todos los datos necesarios para tomar la decisión de carga."""
    forecast: SolarForecast
    soc_actual_pct: float        # SOC actual de la batería (0-100)
    battery_capacity_kwh: float  # capacidad total del banco de baterías
    daily_consumption_kwh: float # consumo medio diario de la vivienda
    risk_factor: float           # 0.0 = confiar en p50, 1.0 = usar solo p10
    min_soc_pct: float           # límite inferior de SOC (protección batería)
    max_soc_pct: float           # límite superior de SOC (salud batería)
    safety_margin_kwh: float     # margen de seguridad adicional en kWh


@dataclass
class DecisionResult:
    """Resultado del cálculo, con desglose para logging y auditoría."""
    target_soc_pct: float        # SOC objetivo a programar en el inversor
    target_kwh: float            # equivalente en kWh
    to_charge_kwh: float         # kWh a cargar desde la red
    solar_effective_kwh: float   # producción solar que se usará en el cálculo
    energy_stored_kwh: float     # energía ya almacenada en batería ahora mismo
    deficit_kwh: float           # déficit esperado (consumo - solar - almacenado)
    clamped: bool                # True si el resultado fue limitado por min/max SOC
    dry_run: bool                # True si el script está en modo simulación


def calculate_charge_target(inp: DecisionInput, dry_run: bool = False) -> DecisionResult:
    """
    Calcula el nivel de SOC objetivo para el horario valle.

    Args:
        inp:     datos de entrada (previsión, SOC actual, parámetros instalación)
        dry_run: si True, el resultado es válido pero no se actuará sobre él

    Returns:
        DecisionResult con el SOC objetivo y desglose completo del cálculo
    """
    # 1. Producción solar efectiva interpolada según risk_factor
    solar_effective = (
        inp.forecast.p10 * inp.risk_factor
        + inp.forecast.p50 * (1.0 - inp.risk_factor)
    )

    # 2. Energía ya almacenada en la batería ahora mismo
    energy_stored = (inp.soc_actual_pct / 100.0) * inp.battery_capacity_kwh

    # 3. Déficit: lo que no cubrirán ni la solar ni la batería actual
    total_needed = inp.daily_consumption_kwh + inp.safety_margin_kwh
    deficit = max(0.0, total_needed - solar_effective - energy_stored)

    # 4. kWh a cargar desde la red en horario valle
    to_charge = max(0.0, deficit)

    # 5. SOC objetivo (energía actual + carga planificada) como porcentaje
    target_kwh_raw = energy_stored + to_charge
    target_soc_raw = (target_kwh_raw / inp.battery_capacity_kwh) * 100.0

    # 6. Clampear entre min y max configurados
    target_soc = max(inp.min_soc_pct, min(inp.max_soc_pct, target_soc_raw))
    clamped = target_soc != target_soc_raw
    target_kwh = (target_soc / 100.0) * inp.battery_capacity_kwh

    return DecisionResult(
        target_soc_pct=round(target_soc, 1),
        target_kwh=round(target_kwh, 2),
        to_charge_kwh=round(to_charge, 2),
        solar_effective_kwh=round(solar_effective, 2),
        energy_stored_kwh=round(energy_stored, 2),
        deficit_kwh=round(deficit, 2),
        clamped=clamped,
        dry_run=dry_run,
    )


def decision_summary(inp: DecisionInput, result: DecisionResult) -> str:
    """Devuelve un resumen legible del cálculo para el log."""
    lines = [
        "=== Cálculo de carga ===",
        f"  Producción solar p10/p50/p90 : {inp.forecast.p10}/{inp.forecast.p50}/{inp.forecast.p90} kWh",
        f"  Solar efectiva (risk={inp.risk_factor}) : {result.solar_effective_kwh} kWh",
        f"  Energía almacenada actual     : {result.energy_stored_kwh} kWh  (SOC {inp.soc_actual_pct}%)",
        f"  Consumo diario + margen       : {inp.daily_consumption_kwh} + {inp.safety_margin_kwh} kWh",
        f"  Déficit esperado              : {result.deficit_kwh} kWh",
        f"  A cargar en valle             : {result.to_charge_kwh} kWh",
        f"  SOC objetivo                  : {result.target_soc_pct}%  ({result.target_kwh} kWh)",
    ]
    if result.clamped:
        lines.append(f"  (limitado por min={inp.min_soc_pct}% / max={inp.max_soc_pct}%)")
    if result.dry_run:
        lines.append("  [DRY RUN — no se modificará el inversor]")
    return "\n".join(lines)
