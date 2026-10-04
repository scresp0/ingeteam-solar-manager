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
  30034 — PV 1 Power          [W]       UINT16  (= `Pdc1` del datalogger)
  30037 — PV 2 Power          [W]       UINT16  (= `Pdc2`)
  30038 — Inverter AC Power   [W]       INT16   (= `Pac`)
  30072 — External Meter Power [W]      INT16   (= `PacMeter`: + importando, − exportando)

Los cuatro últimos no salen del PDF sino del mapa que publica el propio inversor
(`GET /inverter/map/1`, firmware ABH1007AE, 2026-10-04): su `loggermap` declara de qué
dirección MODBUS copia el datalogger cada campo, así que valor y signo son los mismos.
"""

import functools
import logging
import threading
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.config import InverterConfig
from app.logger_reader import house_power_w

logger = logging.getLogger(__name__)

# El inversor Ingeteam admite muy pocas conexiones MODBUS TCP simultáneas: si el
# read-back del controlador de corriente y el poll del dashboard leen a la vez,
# una conexión recibe el frame de la otra y se lee un snapshot caducado (SOC y
# corriente oscilando entre el valor correcto y el anterior). Este lock serializa
# todo el acceso MODBUS, igual que _WEB_LOCK hace con Playwright en automation.py.
_MODBUS_LOCK = threading.Lock()


def _serialize_modbus(fn):
    """Serializa el acceso MODBUS (una transacción TCP con el inversor a la vez)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _MODBUS_LOCK:
            return fn(*args, **kwargs)
    return wrapper

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
    charge_current_max_a: float = 0.0  # Corriente máxima de carga configurada (holding reg 40087) [A]
    pv_power_w: int = 0        # Producción solar instantánea, FV 1 + FV 2 [W]
    grid_power_w: int = 0      # Vatímetro externo [W] (+ importando de red, − exportando)
    house_power_w: int = 0     # Consumo de la vivienda [W] — ver `logger_reader.house_power_w`


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

class InverterError(Exception):
    """Error al leer datos del inversor."""


@_serialize_modbus
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
        # El más lejano es 30072 (vatímetro externo) → count=72
        result = client.read_input_registers(address=0, count=72, slave=slave)

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
        # regs[33] = 30034 PV 1 Power [W]
        # regs[36] = 30037 PV 2 Power [W]
        # regs[37] = 30038 Inverter AC Power [W] INT16
        # regs[71] = 30072 External Meter Power [W] INT16

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
        pac_w           = regs[37] if regs[37] < 32768 else regs[37] - 65536
        grid_power_w    = regs[71] if regs[71] < 32768 else regs[71] - 65536

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

        # Corriente máxima de carga configurada (holding 40087 → address 86) [A]
        # Solo lectura por MODBUS (la escritura es por web/Playwright).
        charge_current_max = 0.0
        try:
            cc = client.read_holding_registers(address=86, count=1, slave=slave)
            if not cc.isError():
                charge_current_max = float(cc.registers[0])
            else:
                logger.debug(f"Error leyendo holding register 40087: {cc}")
        except Exception as e:
            logger.debug(f"No se pudo leer corriente máxima de carga (40087): {e}")

        state = InverterState(
            soc_pct=float(soc),
            soh_pct=float(soh),
            battery_voltage_v=battery_voltage_raw / 10.0,
            battery_power_w=battery_power_w,
            battery_temp_c=battery_temp_c,
            inverter_status=INVERTER_STATUS.get(inverter_status_code, f"Unknown ({inverter_status_code})"),
            battery_status=BATTERY_STATUS.get(battery_status_code, f"Unknown ({battery_status_code})"),
            min_soc_pct=min_soc,
            charge_current_max_a=charge_current_max,
            pv_power_w=regs[33] + regs[36],
            grid_power_w=grid_power_w,
            house_power_w=round(house_power_w({"Pac": pac_w, "PacMeter": grid_power_w})),
        )

        logger.debug(
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

