#!/usr/bin/env python3
"""
recompute_daily_stats.py — fuerza la reescritura de stats_diarias + solar_media_hora
para todos los días disponibles en el datalogger, sin importar si ya existen en
InfluxDB.

A diferencia de `main.backfill_solar_history` (que solo rellena huecos, nunca
reescribe un día ya presente), este script recorre TODOS los días de la ventana
rodante del datalogger (~59 días, `MAX_BACKFILL_DAYS`) y sobrescribe cada uno.
Pensado para cuando se corrige la fórmula de cálculo de un campo ya almacenado
(p. ej. `house_power_w`, v1.80) y hace falta recalcular el histórico, no solo
rellenar lo que falta. Reescribir un día ya presente es idempotente (mismo
measurement + mismo timestamp → InfluxDB sobrescribe).

Uso (dentro del contenedor, necesita las dependencias de la app):
    docker exec -i solar-manager python -m scripts.recompute_daily_stats
    docker exec -i solar-manager python -m scripts.recompute_daily_stats --days 14
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, "/app")

from app.config import load_config
from app.logger_reader import LoggerReaderError, get_daily_stats
from app.storage import StorageError, write_daily_stats, write_half_hour_stats

MAX_BACKFILL_DAYS = 59

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("recompute_daily_stats")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=MAX_BACKFILL_DAYS,
                         help=f"días hacia atrás desde ayer (máx {MAX_BACKFILL_DAYS}, límite del datalogger)")
    parser.add_argument("--config", default=None, help="ruta a config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not cfg.influxdb.enabled:
        logger.error("InfluxDB deshabilitado en config — nada que recalcular")
        sys.exit(1)

    yesterday = date.today() - timedelta(days=1)
    n_days = min(args.days, MAX_BACKFILL_DAYS)
    days = [yesterday - timedelta(days=i) for i in range(n_days)][::-1]

    ok = failed = 0
    for day in days:
        try:
            stats = get_daily_stats(cfg.inverter, day)
            write_daily_stats(cfg.influxdb, stats)
            write_half_hour_stats(cfg.influxdb, stats)
            ok += 1
            logger.info(f"{day}: recalculado (consumo={stats.consumption_kwh:.2f} kWh, "
                        f"nocturno={stats.night_consumption_kwh:.2f} kWh)")
        except LoggerReaderError as e:
            failed += 1
            logger.warning(f"{day}: fuera de la ventana del datalogger o sin datos — {e}")
        except StorageError as e:
            failed += 1
            logger.error(f"{day}: error escribiendo en InfluxDB — {e}")

    logger.info(f"Terminado: {ok} día(s) recalculados, {failed} no disponibles/fallidos")


if __name__ == "__main__":
    main()
