"""
simulate_charge_current.py — ejecuta el controlador de corriente de carga en modo
SIMULACIÓN: lee el inversor (MODBUS), calcula el objetivo y reporta a cuánto está,
a cuánto lo fijaría y si cambiaría — SIN tocar el inversor.

Pensado para el botón "Simular control de corriente" de la web (se lanza como
subprocess y su salida se transmite por SSE), pero también vale por CLI:

  docker exec -i solar-manager python -m app.simulate_charge_current
"""
import logging
import sys

from app.config import load_config
from app.main import run_charge_current_controller


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )
    try:
        cfg = load_config()
    except Exception as e:
        print(f"ERROR cargando configuración: {e}")
        sys.exit(1)

    # Sin envolver en "=== … ===": el visor de logs colapsa esas líneas y
    # ocultaría la línea [CORRIENTE], que es justo lo que se quiere ver.
    print("Simulación del controlador de corriente de carga (no escribe nada)…")
    run_charge_current_controller(cfg, simulate=True)
    print("Fin de la simulación.")


if __name__ == "__main__":
    main()
