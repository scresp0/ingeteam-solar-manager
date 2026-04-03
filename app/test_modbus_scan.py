"""
Escaneo de registros MODBUS — para diagnosticar el offset correcto.
  docker compose run --rm solar-manager python -m app.test_modbus_scan
"""
import sys
import logging
from pymodbus.client import ModbusTcpClient

logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

HOST = "10.172.24.42"
PORT = 502
SLAVE = 1

client = ModbusTcpClient(host=HOST, port=PORT, timeout=10)
if not client.connect():
    print(f"No se pudo conectar a {HOST}:{PORT}")
    sys.exit(1)

print(f"Conectado a {HOST}:{PORT}\n")

# Probar distintos rangos de direcciones
tests = [
    ("address=0, count=30",   0,   30),
    ("address=1, count=30",   1,   30),
    ("address=15, count=13",  15,  13),
    ("address=16, count=13",  16,  13),
    ("address=30000, count=5",30000, 5),
    ("address=30001, count=5",30001, 5),
]

for label, addr, count in tests:
    result = client.read_input_registers(address=addr, count=count, slave=SLAVE)
    if result.isError():
        print(f"  [{label}] ERROR: {result}")
    else:
        print(f"  [{label}] OK → {result.registers}")

client.close()
