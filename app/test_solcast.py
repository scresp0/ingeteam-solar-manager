"""
Test de solcast.py — ejecutar con credenciales reales:
  docker compose run --rm solar-manager python -m app.test_solcast

Requiere config.yaml con solcast.api_key y solcast.resource_id reales.
"""
import logging
import sys
from app.config import load_config
from app.solcast import get_tomorrow_forecast, SolcastError

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)

def main():
    cfg = load_config("config.yaml")

    print(f"\nLlamando a Solcast...")
    print(f"  resource_id : {cfg.solcast.resource_id}")
    print(f"  timezone    : {cfg.system.timezone}")

    try:
        forecast = get_tomorrow_forecast(cfg.solcast, cfg.system.timezone)
        print(f"\nPrevisión para mañana:")
        print(f"  p10 (pesimista) : {forecast.p10} kWh")
        print(f"  p50 (esperado)  : {forecast.p50} kWh")
        print(f"  p90 (optimista) : {forecast.p90} kWh")
        print("\nOK")
    except SolcastError as e:
        print(f"\nError de Solcast: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
