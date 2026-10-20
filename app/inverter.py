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
  30072 — External Meter Power [W]      INT16   (= `PacMeter`: + importando, − exportando)
  30079 — Total Loads Power   [W]       UINT16  (= `Pload` del datalogger SDO: consumo de la vivienda)

Los cuatro últimos no salen del PDF sino del mapa que publica el propio inversor
(`GET /inverter/map/1`, firmware ABH1007AE, 2026-10-04): su `loggermap` declara de qué
dirección MODBUS copia el datalogger cada campo, así que valor y signo son los mismos.

Holding registers (función 0x03):
  40087 — Corriente Máxima de Carga [A]
  40126 — SOC mínimo                [%]
  40132 — Capacidad Nominal         [kWh x10]  (sección web 1.2; mapa: `of: float(1)`)
  40059–40134 — Programación horaria 6.3.1 / 6.3.2, empaquetada por bits — ver
                `read_schedule_state`. Solo LECTURA: escribirla por MODBUS no funciona,
                la escritura sigue siendo por web.
"""

import functools
import logging
import threading
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.automation import (
    ScheduleState, SCHEDULE_DISABLED, SCHEDULE_WEEKDAY, SCHEDULE_WEEKEND,
    DISC_WEEKDAY_ON_H, DISC_WEEKDAY_ON_M, DISC_WEEKDAY_OFF_H, DISC_WEEKDAY_OFF_M,
    DISC_WEEKEND_ON_H, DISC_WEEKEND_ON_M, DISC_WEEKEND_OFF_H, DISC_WEEKEND_OFF_M,
)
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
    battery_capacity_kwh: float = 0.0  # Capacidad nominal configurada (holding reg 40132) [kWh]; 0 = no leída
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
        # El más lejano es 30079 (cargas totales) → count=79
        result = client.read_input_registers(address=0, count=79, slave=slave)

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
        # regs[71] = 30072 External Meter Power [W] INT16
        # regs[78] = 30079 Total Loads Power [W]

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

        # Capacidad nominal de la batería (holding 40132 → address 131) [kWh x10]
        # Es el valor que se configura en la web (1.2), no una medida del BMS.
        battery_capacity = 0.0
        try:
            cap = client.read_holding_registers(address=131, count=1, slave=slave)
            if not cap.isError():
                battery_capacity = cap.registers[0] / 10.0
            else:
                logger.warning(f"Error leyendo holding register 40132: {cap}")
        except Exception as e:
            logger.warning(f"No se pudo leer capacidad nominal de la batería (40132): {e}")

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
            battery_capacity_kwh=battery_capacity,
            pv_power_w=regs[33] + regs[36],
            grid_power_w=grid_power_w,
            house_power_w=round(house_power_w({"Pload": regs[78]})),
        )

        logger.debug(
            f"Inversor: {state.inverter_status} | "
            f"Batería: {state.battery_status} | "
            f"SOC: {state.soc_pct}% (min={state.min_soc_pct}%) | SOH: {state.soh_pct}% | "
            f"Capacidad: {state.battery_capacity_kwh} kWh | "
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


# ---------------------------------------------------------------------------
# Programación horaria 6.3.1 / 6.3.2 (solo lectura)
# ---------------------------------------------------------------------------
# No viene en el PDF de registros: sale de `GET /inverter/map/1` (campos `start`/`len`
# de cada entrada `holding`), verificado el 2026-10-21 con firmware ABH1007AG. Los
# modos van empaquetados por bits en registros que comparten con campos de otras
# secciones (40101 lleva también el tipo de red, la potencia contratada y 6.3.3), así
# que hay que enmascarar. Las horas van como hora·256 + minuto.
#   ⚠️ 40125 también se llama «SOC Grid 1…», pero es el de 6.2 UPS: el de 6.3.1 es 40120.
# Direcciones base-0 (40101 → 100).
_SCHED_FIRST, _SCHED_COUNT = 58, 76          # bloque 40059–40134, una sola lectura
_CHARGE_PROG1_MODE = (100, 5, 2)             # (dirección, bit inicial, nº de bits)
_CHARGE_PROG2_MODE = (133, 1, 2)
_CHARGE_SOC_1      = 119                     # 40120 SOC Grid 1 de 6.3.1
_DISC_PROG1_MODE   = (100, 10, 2)
_DISC_PROG2_MODE   = (100, 12, 2)
_DISC_PROG1_ON, _DISC_PROG1_OFF = 120, 121   # 40121 / 40122
_DISC_PROG2_ON, _DISC_PROG2_OFF = 64, 65     # 40065 / 40066


def _bits(regs: dict[int, int], field: tuple[int, int, int]) -> str:
    addr, start, length = field
    return str((regs[addr] >> start) & ((1 << length) - 1))


def _hhmm(value: int) -> tuple[int, int]:
    return value >> 8, value & 0xFF


def decode_schedule(regs: dict[int, int]) -> ScheduleState:
    """Traduce los holding registers de 6.3.1/6.3.2 al mismo `ScheduleState` que la web.

    Mismas reglas que `automation._read_charge_state` / `_read_discharge_state`:
    carga activa si cualquiera de las dos programaciones no está Desactivada; descarga
    LIBRE si ambas lo están, BLOQUEADA solo con la config canónica, y cualquier otra
    cosa → `discharge_recognized=False` para forzar la reescritura.
    """
    charge_active = (_bits(regs, _CHARGE_PROG1_MODE) != SCHEDULE_DISABLED
                     or _bits(regs, _CHARGE_PROG2_MODE) != SCHEDULE_DISABLED)
    charge_soc = regs[_CHARGE_SOC_1] if charge_active else 0

    disc1, disc2 = _bits(regs, _DISC_PROG1_MODE), _bits(regs, _DISC_PROG2_MODE)
    if disc1 == SCHEDULE_DISABLED and disc2 == SCHEDULE_DISABLED:
        blocked, recognized = False, True
    elif (disc1 == SCHEDULE_WEEKDAY and disc2 == SCHEDULE_WEEKEND
          and _hhmm(regs[_DISC_PROG1_ON]) + _hhmm(regs[_DISC_PROG1_OFF])
              == (DISC_WEEKDAY_ON_H, DISC_WEEKDAY_ON_M, DISC_WEEKDAY_OFF_H, DISC_WEEKDAY_OFF_M)
          and _hhmm(regs[_DISC_PROG2_ON]) + _hhmm(regs[_DISC_PROG2_OFF])
              == (DISC_WEEKEND_ON_H, DISC_WEEKEND_ON_M, DISC_WEEKEND_OFF_H, DISC_WEEKEND_OFF_M)):
        blocked, recognized = True, True
    else:
        logger.warning(
            "6.3.2 tiene una programación de descarga activa pero NO canónica "
            f"(Prog1={disc1} {_hhmm(regs[_DISC_PROG1_ON])}–{_hhmm(regs[_DISC_PROG1_OFF])}, "
            f"Prog2={disc2} {_hhmm(regs[_DISC_PROG2_ON])}–{_hhmm(regs[_DISC_PROG2_OFF])}) "
            "— se reescribirá"
        )
        blocked, recognized = False, False

    return ScheduleState(
        charge_active=charge_active,
        charge_soc_pct=charge_soc,
        discharge_blocked=blocked,
        discharge_recognized=recognized,
    )


@_serialize_modbus
def read_schedule_state(cfg: InverterConfig) -> ScheduleState | None:
    """Lee 6.3.1 y 6.3.2 por MODBUS. None si falla (el caller fuerza la reescritura).

    Sustituye a `automation.read_inverter_schedule` (Playwright, ~25 s y una sesión
    web) para leer el estado ANTES de escribir; esa sigue existiendo para
    `diag_automation`, que compara ambas lecturas tras un cambio de firmware.
    """
    client = ModbusTcpClient(host=cfg.get_modbus_host(), port=cfg.modbus_port,
                             timeout=cfg.browser_timeout_seconds)
    try:
        if not client.connect():
            raise InverterError(f"No se pudo conectar al inversor en {cfg.get_modbus_host()}:502")
        result = client.read_holding_registers(address=_SCHED_FIRST, count=_SCHED_COUNT,
                                               slave=cfg.modbus_slave)
        if result.isError():
            raise InverterError(f"Error MODBUS al leer la programación: {result}")
        regs = dict(enumerate(result.registers, start=_SCHED_FIRST))
        state = decode_schedule(regs)
        logger.info(
            f"Programación leída del inversor (MODBUS): "
            f"6.3.1 Carga={state.charge_str()} | 6.3.2 Descarga={state.discharge_str()}"
        )
        return state
    except Exception as e:
        logger.warning(f"No se pudo leer la programación del inversor por MODBUS: {e}")
        return None
    finally:
        client.close()
