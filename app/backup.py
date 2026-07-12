"""
backup.py — copia de seguridad periódica a un servidor externo por SCP.

Empaqueta en un único .tar.gz:
  - backup online de InfluxDB (`influx backup` del bucket configurado; no para el
    contenedor influxdb, habla con él por HTTP igual que el resto del código)
  - el directorio de logs
  - config.yaml

y lo sube por SCP (autenticación por clave SSH) a `backup.remote_dir` del host
remoto. Tras subir, rota los backups antiguos manteniendo `backup.retention`.

Reutilizado por el job APScheduler (scheduler._run_backup_job, diario) y por el
endpoint manual POST /api/backup/run.
"""

import logging
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
from datetime import datetime

from app.config import AppConfig

logger = logging.getLogger(__name__)

# Prefijo de los ficheros de backup en el remoto. La rotación borra por este
# patrón, así que debe ser estable y no colisionar con otros ficheros.
_ARCHIVE_PREFIX = "solar-backup"


class BackupError(Exception):
    """Fallo durante la creación o subida del backup."""


def _get_hostname() -> str:
    return os.environ.get("HOST_HOSTNAME") or socket.gethostname() or "host"


def _dump_influxdb(cfg: AppConfig, dest_dir: str) -> None:
    """Genera un backup online de InfluxDB en dest_dir (mismo método que /api/db/export)."""
    cmd = [
        "influx", "backup", dest_dir,
        "--host", cfg.influxdb.url,
        "--org", cfg.influxdb.org,
        "--bucket", cfg.influxdb.bucket,
    ]
    env = {**os.environ, "INFLUX_TOKEN": cfg.influxdb.token}
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise BackupError(proc.stderr.strip() or "influx backup falló")


def create_backup_archive(cfg: AppConfig) -> tuple[str, str, str]:
    """Crea el .tar.gz con DB + logs + config.

    Devuelve (tmp_dir, archive_path, filename). El llamante es responsable de
    borrar tmp_dir. Lanza BackupError si algo falla (tras limpiar tmp_dir).
    """
    tmp = tempfile.mkdtemp(prefix="solar-backup-")
    try:
        # 1) Backup de InfluxDB (si está habilitado)
        if cfg.influxdb.enabled:
            _dump_influxdb(cfg, os.path.join(tmp, "influxdb"))
        else:
            logger.warning("InfluxDB no habilitado — el backup no incluirá la BD")

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        fname = f"{_ARCHIVE_PREFIX}-{_get_hostname()}-{ts}.tar.gz"
        archive = os.path.join(tmp, fname)

        with tarfile.open(archive, "w:gz") as tar:
            db_dir = os.path.join(tmp, "influxdb")
            if os.path.isdir(db_dir):
                tar.add(db_dir, arcname="influxdb")
            # 2) Logs
            log_dir = os.path.dirname(cfg.system.log_file) or "."
            if os.path.isdir(log_dir):
                tar.add(log_dir, arcname="logs")
            else:
                logger.warning(f"Directorio de logs no encontrado: {log_dir}")
            # 3) config.yaml
            cfg_path = os.environ.get("CONFIG_PATH") or "/app/config.yaml"
            if not os.path.isfile(cfg_path):
                cfg_path = "config.yaml"
            if os.path.isfile(cfg_path):
                tar.add(cfg_path, arcname="config.yaml")
            else:
                logger.warning("config.yaml no encontrado — no se incluye en el backup")

        return tmp, archive, fname
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(e, BackupError):
            raise
        raise BackupError(f"Error creando el archivo de backup: {e}") from e


def _ssh_common_opts(cfg: AppConfig) -> list[str]:
    """Opciones -o comunes a scp y ssh (clave, timeout, verificación de host)."""
    opts = [
        "-o", "BatchMode=yes",                       # nunca pedir contraseña interactiva
        "-o", f"ConnectTimeout={min(cfg.backup.timeout_seconds, 120)}",
        "-i", cfg.backup.ssh_key_path,
    ]
    if cfg.backup.strict_host_key_checking:
        opts += [
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={cfg.backup.known_hosts_path}",
        ]
    else:
        opts += ["-o", "StrictHostKeyChecking=no"]
    return opts


def _upload(cfg: AppConfig, archive: str, fname: str) -> None:
    """Sube el archivo por SCP al remoto."""
    b = cfg.backup
    dest = f"{b.user}@{b.host}:{b.remote_dir.rstrip('/')}/{fname}"
    # scp usa -P (mayúscula) para el puerto.
    cmd = ["scp", "-P", str(b.port), *_ssh_common_opts(cfg), archive, dest]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=b.timeout_seconds,
    )
    if proc.returncode != 0:
        raise BackupError(f"scp falló: {proc.stderr.strip() or 'error desconocido'}")


def _rotate_remote(cfg: AppConfig) -> None:
    """Borra en el remoto los backups que excedan `retention` (más antiguos primero)."""
    b = cfg.backup
    remote_dir = b.remote_dir.rstrip("/")
    # ls -1t: más nuevos primero; tail -n +N+1: los que sobran; xargs -r rm.
    remote_cmd = (
        f"ls -1t '{remote_dir}'/{_ARCHIVE_PREFIX}-*.tar.gz 2>/dev/null "
        f"| tail -n +{b.retention + 1} | xargs -r rm -f"
    )
    # ssh usa -p (minúscula) para el puerto.
    cmd = ["ssh", "-p", str(b.port), *_ssh_common_opts(cfg),
           f"{b.user}@{b.host}", remote_cmd]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=b.timeout_seconds,
    )
    if proc.returncode != 0:
        # La rotación fallida no invalida un backup ya subido: se avisa y sigue.
        logger.warning(
            f"Rotación de backups remotos falló (backup subido igualmente): "
            f"{proc.stderr.strip() or 'error desconocido'}"
        )


def run_backup(cfg: AppConfig) -> str:
    """Crea el backup, lo sube por SCP y rota los antiguos. Devuelve el nombre del fichero.

    Lanza BackupError ante cualquier fallo de creación o subida.
    """
    if not cfg.backup.enabled:
        raise BackupError("backup externo no habilitado (backup.enabled=false)")

    logger.info("Iniciando copia de seguridad externa por SCP…")
    tmp, archive, fname = create_backup_archive(cfg)
    try:
        size_mb = os.path.getsize(archive) / (1024 * 1024)
        logger.info(f"Archivo de backup creado: {fname} ({size_mb:.1f} MB)")
        _upload(cfg, archive, fname)
        logger.info(
            f"Backup subido a {cfg.backup.user}@{cfg.backup.host}:"
            f"{cfg.backup.remote_dir}/{fname}"
        )
        _rotate_remote(cfg)
        logger.info(f"Backup externo completado (retención: {cfg.backup.retention})")
        return fname
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
