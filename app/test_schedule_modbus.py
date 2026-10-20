"""
test_schedule_modbus.py — tests de `inverter.decode_schedule`: la traducción de los
holding registers empaquetados de 6.3.1/6.3.2 a `ScheduleState`.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_schedule_modbus
  o con toda la batería:  make test

Determinista: no abre ninguna conexión MODBUS, se le pasan los registros a mano.
El primer caso son los valores reales leídos del inversor el 2026-10-21 (firmware
ABH1007AG) con la descarga libre y la carga desactivada.

Existe desde v1.96, cuando el ciclo pasó a leer el estado ANTES por MODBUS en vez de
por web: un desplazamiento de bits mal hecho daría un estado falso pero plausible, y
el flujo de escritura condicionada lo tomaría por "ya correcto" sin reconfigurar.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.automation import ScheduleState
from app.inverter import decode_schedule

passed = failed = 0


def check(desc, cond, extra=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓  {desc}")
    else:
        failed += 1
        print(f"  ✗  {desc}" + (f"  → {extra!r}" if extra is not None else ""))


def hhmm(h, m):
    return h * 256 + m


# Valores reales del 2026-10-21 (00:30): carga DESACTIVADA, descarga LIBRE. 40101 = 513
# lleva bits ajenos encendidos (bit 0 tipo de red, bit 9 potencia contratada), y 6.3.2
# conserva las horas del bloqueo canónico de una prueba anterior con los modos a 0.
REAL = {a: 0 for a in range(58, 134)}
REAL.update({61: 1, 62: 1851, 63: 75, 64: 0, 65: 1, 100: 513,
             117: 1, 118: 1851, 119: 75, 120: 2048, 121: 5947, 133: 0})


def regs(charge1=0, charge2=0, disc1=0, disc2=0, soc=75,
         disc1_on=(8, 0), disc1_off=(23, 59), disc2_on=(0, 0), disc2_off=(0, 1),
         extra_100=0, extra_133=0):
    r = dict(REAL)
    r[100] = extra_100 | (charge1 << 5) | (disc1 << 10) | (disc2 << 12)
    r[133] = extra_133 | (charge2 << 1)
    r[119] = soc
    r[120], r[121] = hhmm(*disc1_on), hhmm(*disc1_off)
    r[64], r[65] = hhmm(*disc2_on), hhmm(*disc2_off)
    return r


def main():
    print("=== valores reales (ABH1007AG) ===")
    check("carga DESACTIVADA, descarga LIBRE reconocida",
          decode_schedule(REAL) == ScheduleState(False, 0, False, True), decode_schedule(REAL))

    print("=== 6.3.1 carga ===")
    s = decode_schedule(regs(charge1=2, charge2=3, soc=85))
    check("L-V + S-D → activa con su SOC", (s.charge_active, s.charge_soc_pct) == (True, 85), s)
    s = decode_schedule(regs(charge2=3, soc=60))
    check("solo Prog 2 activa → activa", (s.charge_active, s.charge_soc_pct) == (True, 60), s)
    s = decode_schedule(regs(soc=85))
    check("desactivada → SOC 0 aunque 40120 tenga valor", (s.charge_active, s.charge_soc_pct) == (False, 0), s)

    print("=== 6.3.2 descarga ===")
    s = decode_schedule(regs(disc1=2, disc2=3))
    check("bloqueo canónico → BLOQUEADA reconocida", (s.discharge_blocked, s.discharge_recognized) == (True, True), s)
    s = decode_schedule(regs(disc1=2, disc2=3, disc1_on=(0, 1), disc1_off=(7, 59), disc2_on=(0, 1), disc2_off=(23, 59)))
    check("horario invertido ≤v1.50 → no reconocida", (s.discharge_blocked, s.discharge_recognized) == (False, False), s)
    s = decode_schedule(regs(disc1=1, disc2=3))
    check("Prog 1 'Toda la semana' → no reconocida", (s.discharge_blocked, s.discharge_recognized) == (False, False), s)
    s = decode_schedule(regs(disc1=2))
    check("solo Prog 1 activa → no reconocida", (s.discharge_blocked, s.discharge_recognized) == (False, False), s)

    print("=== independencia de bits ===")
    # Todos los bits ajenos encendidos: 40101 bits 0-4, 7-9 y 14-15; 40134 bit 0 y 3-15.
    ajenos_100 = 0xFFFF & ~((0b11 << 5) | (0b11 << 10) | (0b11 << 12))
    ajenos_133 = 0xFFFF & ~(0b11 << 1)
    s = decode_schedule(regs(extra_100=ajenos_100, extra_133=ajenos_133))
    check("bits ajenos a 1 no activan nada", s == ScheduleState(False, 0, False, True), s)
    s = decode_schedule(regs(charge1=2, disc1=2, disc2=3, extra_100=ajenos_100, extra_133=ajenos_133))
    check("bits ajenos a 1 no alteran un estado activo",
          s == ScheduleState(True, 75, True, True), s)
    s = decode_schedule(regs(disc1=2, disc2=3))
    check("6.3.2 activa no se lee como carga activa", not s.charge_active, s)

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        return 1
    print(f"✓ Todos los tests de decode_schedule pasaron ({passed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
