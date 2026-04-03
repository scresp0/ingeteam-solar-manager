"""
Test del ciclo completo — ejecutar con:
  docker compose run --rm solar-manager python -m app.test_main

Ejecuta el ciclo completo una vez en dry_run y sale.
Para ejecutar con escritura real: añadir --write
"""
import logging
import sys
from app.config import load_config
from app.main import run, setup_logging

def main():
    cfg = load_config("config.yaml")

    if "--write" not in sys.argv:
        cfg.system.dry_run = True

    setup_logging(cfg)

    print(f"\nTest del ciclo completo")
    print(f"  Dry run : {cfg.system.dry_run}")
    print()

    success = run(cfg)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
