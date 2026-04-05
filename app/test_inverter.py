"""
Test de inverter.py — ejecutar con acceso a la red local del inversor:
  docker compose run --rm solar-manager python -m app.test_inverter

Requiere config.yaml con inverter.web_url apuntando al inversor real.
"""
import logging
import sys
from app.config import load_config
from app.inverter import read_inverter_state, InverterError

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)

def main():
    cfg = load_config("config.yaml")
    print(f"\nConectando al inversor: {cfg.inverter.web_url}")

    try:
        state = read_inverter_state(cfg.inverter)
        print(f"\nEstado del inversor:")
        print(f"  Inversor        : {state.inverter_status}")
        print(f"  Batería         : {state.battery_status}")
        print(f"  SOC             : {state.soc_pct} %")
        print(f"  SOH             : {state.soh_pct} %")
        print(f"  Potencia        : {state.battery_power_w} W  ({'descargando' if state.battery_power_w > 0 else 'cargando' if state.battery_power_w < 0 else 'en reposo'})")
        print(f"  Tensión         : {state.battery_voltage_v} V")
        print(f"  Temperatura     : {state.battery_temp_c} ºC")
        print(f"  SOC mínimo inv. : {state.min_soc_pct} %")
        print("\nOK")
    except InverterError as e:
        print(f"\nError al leer el inversor: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
