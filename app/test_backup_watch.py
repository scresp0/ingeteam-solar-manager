"""
test_backup_watch.py — pruebas deterministas de la vigilancia del backup a la NAS.

`check_backup` lee los dos ficheros que deja el contenedor de backup en ./logs y
decide qué registrar y cuándo avisar por email. Lo delicado es el estado entre
ejecuciones (cada 5 min): un único email por incidencia, no uno por reintento.

Ejecutar:
  PYTHONPATH=. python app/test_backup_watch.py
  o en el contenedor:  docker exec -i solar-manager python -m app.test_backup_watch
"""
import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app.backup_watch as bw

passed = failed = 0


def check(desc, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓  {desc}")
    else:
        failed += 1
        print(f"  ✗  {desc}  {detail}")


class _Captura(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


T0 = datetime(2026, 11, 5, 23, 58, tzinfo=timezone(timedelta(hours=1)))


def main():
    cap = _Captura()
    logging.getLogger("app.backup_watch").addHandler(cap)
    logging.getLogger("app.backup_watch").setLevel(logging.INFO)
    logging.getLogger("app.backup_watch").propagate = False

    alerts = []

    with tempfile.TemporaryDirectory() as d:
        logs = Path(d)

        def status(st, at, msg=""):
            (logs / bw.STATUS_FILE).write_text(json.dumps(
                {"status": st, "at": at.isoformat(), "backup": at.strftime("%Y%m%d_%H%M%S"),
                 "message": msg}))
            if st == "ok":
                (logs / bw.LAST_OK_FILE).write_text(at.isoformat() + "\n")

        def tick(now):
            cap.records.clear()
            n = len(alerts)
            bw.check_backup(None, logs_dir=logs, now=now, alert=lambda c, m: alerts.append(m))
            return alerts[n:], list(cap.records)

        def reset():
            bw._last_seen_at = bw._last_status = bw._last_stale_alert = None
            bw._started = False
            alerts.clear()
            for f in logs.iterdir():
                f.unlink()

        print("=== Sin ficheros (el contenedor nunca ha dejado resultado) ===")
        reset()
        a, _ = tick(T0)
        check("avisa de que no hay ningún backup", len(a) == 1 and "ningún backup" in a[0], a)
        a, _ = tick(T0 + timedelta(minutes=5))
        check("no repite el aviso a los 5 min", a == [], a)
        a, _ = tick(T0 + timedelta(hours=24))
        check("lo repite a las 24 h si sigue igual", len(a) == 1, a)

        print("=== Arranque con un backup reciente y sin resultado nuevo ===")
        reset()
        (logs / bw.LAST_OK_FILE).write_text(T0.isoformat())
        a, l0 = tick(T0 + timedelta(hours=2))
        check("la 1ª comprobación resume el estado a INFO, sin email",
              a == [] and any(l == "INFO" and "hace 2 h" in m for l, m in l0), (a, l0))
        a, l0 = tick(T0 + timedelta(hours=2, minutes=5))
        check("las siguientes no repiten el resumen", l0 == [], l0)

        print("=== Backup correcto ===")
        reset()
        status("ok", T0)
        a, logs1 = tick(T0 + timedelta(minutes=1))
        check("sin email", a == [], a)
        check("registra el backup completado a INFO",
              any(l == "INFO" and "completado" in m for l, m in logs1), logs1)
        a, logs2 = tick(T0 + timedelta(minutes=6))
        check("no vuelve a registrar el mismo resultado", logs2 == [], logs2)

        print("=== Fallo y reintentos ===")
        status("error", T0 + timedelta(days=1), 'mkdir: Read-only file system\nERROR: "x"')
        a, _ = tick(T0 + timedelta(days=1, minutes=1))
        check("el primer fallo avisa por email", len(a) == 1 and "ha fallado" in a[0], a)
        check("el email lleva el motivo en una línea",
              "Read-only file system | ERROR" in a[0], a)
        status("error", T0 + timedelta(days=1, minutes=30), "otra vez")
        a, logs3 = tick(T0 + timedelta(days=1, minutes=31))
        check("el reintento fallido NO repite el email", a == [], a)
        check("pero sí queda en el log como ERROR",
              any(l == "ERROR" and "otra vez" in m for l, m in logs3), logs3)

        print("=== Recuperación ===")
        status("ok", T0 + timedelta(days=1, hours=1))
        a, logs4 = tick(T0 + timedelta(days=1, hours=1, minutes=1))
        check("sin email al recuperarse", a == [], a)
        check("registra que vuelve a funcionar",
              any("vuelve a funcionar" in m for _, m in logs4), logs4)

        print("=== Backup parado (el contenedor no deja nada nuevo) ===")
        reset()
        status("ok", T0)
        tick(T0 + timedelta(minutes=1))
        a, _ = tick(T0 + timedelta(hours=25))
        check("a las 25 h todavía no avisa", a == [], a)
        a, _ = tick(T0 + timedelta(hours=27))
        check("a las 27 h avisa de backup parado",
              len(a) == 1 and "parado" in a[0] and "hace 27 h" in a[0], a)
        a, _ = tick(T0 + timedelta(hours=28))
        check("no repite el aviso a la hora siguiente", a == [], a)
        status("ok", T0 + timedelta(hours=29))
        a, logs5 = tick(T0 + timedelta(hours=29, minutes=1))
        check("al volver el backup, registra la vuelta y no avisa",
              a == [] and any("reciente" in m for _, m in logs5), (a, logs5))
        a, _ = tick(T0 + timedelta(hours=29 + 27))
        check("un nuevo parón vuelve a avisar enseguida (no espera 24 h)", len(a) == 1, a)

        print("=== Al arrancar la app con un fallo pendiente ===")
        reset()
        status("ok", T0)
        status("error", T0 + timedelta(hours=1), "NAS caída")
        a, _ = tick(T0 + timedelta(hours=1, minutes=1))
        check("avisa del fallo aunque ocurriera antes de arrancar", len(a) == 1, a)

        print("=== Fichero corrupto ===")
        reset()
        status("ok", T0)
        (logs / bw.STATUS_FILE).write_text("{no es json")
        a, logs6 = tick(T0 + timedelta(minutes=1))
        check("no revienta: WARNING y sigue", any(l == "WARNING" for l, _ in logs6), logs6)
        check("y sin email (el último correcto es reciente)", a == [], a)

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        return 1
    print(f"✓ Todos los tests de backup_watch pasaron ({passed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
