"""
backup_watch.py — vigila el backup diario de InfluxDB a la NAS.

El backup no lo hace la app: corre en el servicio `backup` de docker-compose.yml
(`scripts/backup_influxdb.sh`), que deja su resultado en el directorio de logs que
ambos contenedores comparten (`./logs` → `/app/logs` aquí, `/logs` allí):

  backup_status.json  último intento: {"status": "ok"|"error", "at", "backup", "message"}
  backup_last_ok      hora (ISO 8601) del último backup correcto

`check_backup` (job del scheduler cada CHECK_INTERVAL_MIN) registra en el log de la
app cada resultado nuevo —INFO si fue bien, ERROR con el motivo si falló— y avisa
por email (mismo mecanismo y destinatario que el ciclo nocturno):

  - cuando el backup PASA a fallar. Los reintentos (cada 30 min) se registran en el
    log pero no repiten el email.
  - cuando el último backup correcto tiene más de MAX_AGE_H, o no hay ninguno. Cubre
    lo que el contenedor no puede contar: que no arranque porque la NAS no respondía
    al levantar Docker, que se quede colgado en un montaje NFS caído (el montaje es
    `hard`: espera sin dar error) o que esté parado. Se repite como mucho cada
    STALE_REALERT_H mientras dure.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOGS_DIR = Path("/app/logs")
STATUS_FILE = "backup_status.json"
LAST_OK_FILE = "backup_last_ok"

CHECK_INTERVAL_MIN = 5
MAX_AGE_H = 26          # backup diario a las 23:57 + margen
STALE_REALERT_H = 24

# Estado entre ejecuciones. El job corre en un único hilo (max_instances=1).
_last_seen_at: str | None = None        # `at` del último resultado ya registrado
_last_status: str | None = None         # "ok" | "error"
_last_stale_alert: datetime | None = None
_started = False                        # la 1ª comprobación resume el estado en el log


def _alert(cfg, message: str) -> None:
    """ERROR en el log + email, como `scheduler._notify_config_error`."""
    from app.notifier import CycleEmailNotifier
    notifier = CycleEmailNotifier(cfg.system.email)
    notifier.attach()
    logger.error(message)
    notifier.send(success=False)


def _read_status(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") not in ("ok", "error") or not data.get("at"):
            raise ValueError(f"contenido inesperado: {data!r}")
        return data
    except (OSError, ValueError) as e:   # JSONDecodeError es ValueError
        logger.warning(f"[BACKUP] No se pudo leer {path.name}: {e}")
        return None


def _read_last_ok(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as e:
        logger.warning(f"[BACKUP] No se pudo leer {path.name}: {e}")
        return None


def check_backup(cfg, logs_dir: Path = LOGS_DIR, now: datetime | None = None,
                 alert=_alert) -> None:
    """Registra el resultado del backup si es nuevo y avisa si falla o se ha parado.

    `logs_dir`, `now` y `alert` existen para los tests (`app/test_backup_watch.py`).
    """
    global _last_seen_at, _last_status, _last_stale_alert, _started
    now = now or datetime.now(timezone.utc)

    # 1. Resultado nuevo del contenedor → al log (y email si pasa a error)
    status = _read_status(logs_dir / STATUS_FILE)
    if status is not None and status["at"] != _last_seen_at:
        _last_seen_at = status["at"]
        backup = status.get("backup") or "?"
        if status["status"] == "ok":
            logger.info(f"[BACKUP] Backup a la NAS completado: {backup} ({status['at']})")
            if _last_status == "error":
                logger.info("[BACKUP] El backup vuelve a funcionar tras el error anterior")
        else:
            reason = (status.get("message") or "sin detalle").replace("\n", " | ")
            msg = (f"[BACKUP] El backup a la NAS ha fallado ({status['at']}): {reason}. "
                   f"Detalle: `make logs-backup`")
            if _last_status != "error":
                alert(cfg, msg + " — el contenedor reintenta cada 30 min")
            else:
                logger.error(msg)   # reintento que sigue fallando: log, sin otro email
        _last_status = status["status"]

    # 2. ¿Hay un backup correcto reciente?
    last_ok = _read_last_ok(logs_dir / LAST_OK_FILE)
    age_h = None if last_ok is None else (now - last_ok).total_seconds() / 3600
    if not _started:
        _started = True
        if age_h is not None and age_h <= MAX_AGE_H:
            logger.info(f"[BACKUP] Vigilando el backup a la NAS: último correcto hace "
                        f"{age_h:.0f} h ({last_ok:%Y-%m-%d %H:%M})")
    if age_h is None or age_h > MAX_AGE_H:
        if (_last_stale_alert is None
                or (now - _last_stale_alert).total_seconds() >= STALE_REALERT_H * 3600):
            _last_stale_alert = now
            when = ("no hay constancia de ningún backup correcto" if age_h is None
                    else f"el último backup correcto fue hace {age_h:.0f} h ({last_ok:%Y-%m-%d %H:%M})")
            alert(cfg,
                  f"[BACKUP] Backup a la NAS parado: {when} (máx {MAX_AGE_H} h). Comprueba "
                  f"que el contenedor solar-manager-backup está corriendo (`docker ps`; no "
                  f"arranca si la NAS no responde al levantar Docker) y `make logs-backup`.")
    elif _last_stale_alert is not None:
        _last_stale_alert = None
        logger.info(f"[BACKUP] Vuelve a haber un backup correcto reciente "
                    f"(hace {age_h:.0f} h)")
