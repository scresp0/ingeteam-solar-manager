"""
inverter.py — lectura del estado del inversor Ingeteam vía MODBUS TCP.

Usa el mapa de input registers oficial ABH2010IMB08_I (23/04/2025).
Puerto MODBUS TCP: 502 (función 0x04 - Read Input Registers)

Registros usados:
  30016 — Inverter Status     UINT16
  30018 — Battery Voltage     [V x10]   UINT16
  30020 — Battery Power       [W]       INT16  (+ descargando, - cargando) — convención Ingeteam
  30021 — Battery SOC         [%]       UINT16
  30022 — Battery SOH         [%]       UINT16
  30027 — Battery Status      UINT16
  30028 — Battery Temperature [ºC x10]  INT16
"""

import logging
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.config import InverterConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tablas de estados (de la documentación oficial)
# ---------------------------------------------------------------------------

INVERTER_STATUS = {
    0: "Stopped",
    1: "Starting",
    2: "Off-grid",
    3: "On-grid",
    4: "On-grid (Standby Battery)",
    5: "Waiting to connect to Grid",
    6: "Critical Loads Bypassed to Grid",
    7: "Emergency Charge from PV",
    8: "Emergency Charge from Grid",
    9: "Inverter Locked waiting for Reset",
    10: "Error Mode",
}

BATTERY_STATUS = {
    0: "Standby",
    1: "Discharging",
    2: "Constant Current Charging",
    3: "Constant Voltage Charging",
    4: "Floating",
    5: "Equalizing",
    6: "Error Communication with BMS",
    7: "Not Configured",
    8: "Capacity Calibration (Step 1)",
    9: "Capacity Calibration (Step 2)",
    10: "Standby Manual",
}


# ---------------------------------------------------------------------------
# Dataclass de resultado
# ---------------------------------------------------------------------------

@dataclass
class InverterState:
    """Estado actual del inversor y la batería."""
    soc_pct: float             # SOC batería [%]
    soh_pct: float             # SOH batería [%]
    battery_voltage_v: float   # Tensión batería [V]
    battery_power_w: int       # Potencia batería [W] (+ descargando, - cargando) — convención Ingeteam
    battery_temp_c: float      # Temperatura batería [ºC]
    inverter_status: str       # Descripción del estado del inversor
    battery_status: str        # Descripción del estado de la batería
    min_soc_pct: float = 0.0   # SOC mínimo configurado en el inversor (holding reg 40126)


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

class InverterError(Exception):
    """Error al leer datos del inversor."""


def read_inverter_state(cfg: InverterConfig) -> InverterState:
    """
    Lee el estado actual del inversor y las baterías vía MODBUS TCP.

    Args:
        cfg: configuración del inversor (web_url contiene la IP)

    Returns:
        InverterState con SOC, tensión, potencia y estados

    Raises:
        InverterError: si no se puede conectar o leer los registros
    """
    host = cfg.get_modbus_host()
    port = cfg.modbus_port
    slave = cfg.modbus_slave
    logger.debug(f"Conectando a inversor MODBUS TCP: {host}:{port} (slave={slave})")

    client = ModbusTcpClient(host=host, port=port, timeout=cfg.browser_timeout_seconds)

    try:
        if not client.connect():
            raise InverterError(f"No se pudo conectar al inversor en {host}:502")

        # El inversor usa direccionamiento base 0 (registro 30001 = address 0)
        # Leemos desde address=0 (30001) hasta cubrir todos los registros necesarios
        # El más lejano es 30028 (Battery Temp) → count=28
        result = client.read_input_registers(address=0, count=28, slave=slave)

        if result.isError():
            raise InverterError(f"Error MODBUS al leer registros: {result}")

        regs = result.registers
        # regs[15] = 30016 Inverter Status
        # regs[17] = 30018 Battery Voltage [V x10]
        # regs[19] = 30020 Battery Power [W] INT16
        # regs[20] = 30021 Battery SOC [%]
        # regs[21] = 30022 Battery SOH [%]
        # regs[26] = 30027 Battery Status
        # regs[27] = 30028 Battery Temperature [ºC x10] INT16

        inverter_status_code = regs[15]
        battery_voltage_raw  = regs[17]
        battery_power_raw    = regs[19]
        soc                  = regs[20]
        soh                  = regs[21]
        battery_status_code  = regs[26]
        battery_temp_raw     = regs[27]

        # INT16: si el valor supera 32767 es negativo en complemento a 2
        battery_power_w = battery_power_raw if battery_power_raw < 32768 else battery_power_raw - 65536
        battery_temp_c  = (battery_temp_raw if battery_temp_raw < 32768 else battery_temp_raw - 65536) / 10.0

        # Leer SOC mínimo del holding register 40126
        # Mismo patrón que input registers: address = número_registro - 40001 = 125
        min_soc = 0.0
        try:
            hr = client.read_holding_registers(address=125, count=1, slave=slave)
            if not hr.isError():
                min_soc = float(hr.registers[0])
                logger.debug(f"SOC mínimo leído del inversor: {min_soc}%")
            else:
                logger.warning(f"Error leyendo holding register 40126: {hr}")
        except Exception as e:
            logger.warning(f"No se pudo leer SOC mínimo del inversor: {e}")

        state = InverterState(
            soc_pct=float(soc),
            soh_pct=float(soh),
            battery_voltage_v=battery_voltage_raw / 10.0,
            battery_power_w=battery_power_w,
            battery_temp_c=battery_temp_c,
            inverter_status=INVERTER_STATUS.get(inverter_status_code, f"Unknown ({inverter_status_code})"),
            battery_status=BATTERY_STATUS.get(battery_status_code, f"Unknown ({battery_status_code})"),
            min_soc_pct=min_soc,
        )

        logger.info(
            f"Inversor: {state.inverter_status} | "
            f"Batería: {state.battery_status} | "
            f"SOC: {state.soc_pct}% (min={state.min_soc_pct}%) | SOH: {state.soh_pct}% | "
            f"Potencia: {state.battery_power_w}W | "
            f"Tensión: {state.battery_voltage_v}V | "
            f"Temp: {state.battery_temp_c}ºC"
        )
        # Nota convención Ingeteam: Battery Power positivo = descargando, negativo = cargando
        return state

    except ModbusException as e:
        raise InverterError(f"Error MODBUS: {e}") from e
    finally:
        client.close()

