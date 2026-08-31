"""
run_cycle.py — ejecuta el ciclo nocturno completo una vez y sale.

NO es un test: es el lanzador del ciclo manual. `POST /api/cycle` (botón
"Ejecutar ahora" del dashboard) arranca este módulo como subproceso y transmite
su salida por SSE, así que forma parte del camino de producción.

  docker compose run --rm solar-manager python -m app.run_cycle

Por defecto fuerza dry_run. Con --write ejecuta el ciclo REAL: escribe en el
inversor y persiste ciclo_carga en InfluxDB.
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
