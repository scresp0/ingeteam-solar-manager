## 10. Backup de InfluxDB

Sistema de backup diario automatizado de la base de datos InfluxDB (bucket `solar-manager`), con doble formato y rotación GFS (Grandfather-Father-Son), volcado a NAS Synology.

> **Nota — coexiste con `app/backup.py` (v1.70), pero ese mecanismo está INACTIVO:** la aplicación ya incluye un sistema de backup vía SCP (`influx backup` + logs + `config.yaml` → `.tar.gz` → servidor remoto por SSH), configurable en la sección `backup` de `config.yaml`. Verificado en agosto 2026 (`grep -n "backup" config.yaml` → salida vacía): esa sección **nunca se ha configurado** en producción, por lo que `app/backup.py` está implementado en el código pero no se ejecuta. El mecanismo descrito en esta sección 10 (script externo + systemd timer + NFS) es el que realmente corre en producción. No asumir que el backup SCP está activo sin comprobar `backup.enabled` en `config.yaml` primero.

### 10.1 Componentes

```
solar-manager/
└── scripts/
    ├── backup_influxdb.sh              # Script principal de backup
    └── systemd/
        ├── solar-manager-backup.service
        └── solar-manager-backup.timer
```

### 10.2 Qué hace el script

1. Lee `INFLUXDB_TOKEN` desde `.env` (nunca se expone en logs).
2. Genera **backup binario** con `docker exec ... influx backup` (formato nativo, restauración rápida vía `influx restore`).
3. Genera **export en line protocol comprimido** con `docker exec ... influxd inspect export-lp --bucket-id <id> --engine-path /var/lib/influxdb2/engine --compress` (texto legible/auditable; el contenido queda en gzip aunque el flag `--compress` no añade `.gz` al nombre — se renombra manualmente a `export.lp.gz`).
4. Ambos ficheros se generan primero en `/tmp` **dentro del contenedor** y se extraen con `docker cp` a un staging local (`backups/tmp/`), sin tocar el bind mount de datos en vivo (`/home/scresp0/solar-manager/influxdb/data`).
5. Monta temporalmente por **NFS** el volumen `Intercambio` de la NAS Synology (`172.24.0.6:/volume1/Intercambio` → `/mnt/nas-intercambio`), copia los backups a la categoría de rotación correspondiente, y **desmonta siempre** al terminar (`trap cleanup EXIT`), tanto en éxito como en fallo.
6. Sin *fallbacks* silenciosos: `set -euo pipefail`; cualquier fallo interrumpe el script y queda registrado como `ERROR` en el journal.

### 10.3 Esquema de rotación (GFS)

| Categoría | Disparador | Backups conservados |
|---|---|---|
| `daily` | Cada ejecución | 7 |
| `weekly` | Domingo | 4 |
| `monthly` | Último día del mes | 11 |
| `yearly` | Último día de diciembre | 1 (solo año en curso) |

Destino en NAS: `/mnt/nas-intercambio/solar-manager-backup/{daily,weekly,monthly,yearly}/<timestamp>/{binary, export.lp.gz}`

### 10.4 Programación

- **systemd timer**, no cron — elegido por `Persistent=true`: si el servidor está apagado a las 23:57, el backup se ejecuta en cuanto arranca de nuevo (evita huecos silenciosos por reinicios/cortes).
- Horario: `23:57` diario.
- Logs vía `journalctl -u solar-manager-backup.service`.
- Habilitado con: `sudo systemctl enable --now solar-manager-backup.timer`

### 10.5 Gotchas técnicos — Backup

- **`influxd inspect export-lp` es del daemon, no del CLI cliente**: no aparece en `influx --help` (que solo lista subcomandos de `influx`); está en el binario `influxd`, disponible dentro del mismo contenedor `influxdb:2.7`.
- **Requiere `--bucket-id`, no `--bucket` (nombre)**: a diferencia de `influx backup`, `export-lp` exige el ID del bucket. Bucket `solar-manager` → ID `fa2d32fca4e9f966` (org `solar`).
- **`--compress` no renombra el fichero**: el contenido queda en gzip binario (magic bytes `1f 8b`) pero conserva la extensión indicada en `--output-path`; hay que añadir `.gz` manualmente si se quiere reflejar el formato real.
- **`INFLUXDB_TOKEN` no está disponible como variable de entorno dentro del contenedor** (solo existen las `DOCKER_INFLUXDB_INIT_*` de la inicialización, y `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` queda vacío tras el primer arranque). El token real vive únicamente en `.env` del host y se pasa explícitamente con `--token` en cada `docker exec`.
- **`influxd.bolt` y `influxd.sqlite`** (metadatos: usuarios, orgs, tokens, dashboards) están fuera del alcance de este backup — solo cubre el **bucket de datos** `solar-manager`. Si se necesita reconstruir la instancia completa de InfluxDB desde cero, haría falta respaldar también esos ficheros o rehacer el `setup` inicial.
- **Autenticación SSH/sudo para NFS efímero**: el montaje/desmontaje requiere `sudo`, y como el timer corre sin terminal interactiva, hizo falta una regla `NOPASSWD` **restringida** (solo `mount`/`umount` de esa ruta exacta, no `NOPASSWD: ALL`) en `/etc/sudoers.d/solar-manager-backup`. Esta regla es configuración local del servidor, **no versionada en git**.
- **Export NFS con squash restrictivo por defecto**: el primer intento de montaje dio `Permiso denegado` porque la carpeta compartida `Intercambio` en el Synology tenía el mapeo NFS (squash) configurado de forma que el UID del cliente no tenía acceso real. Se resolvió ajustando los permisos NFS de la carpeta compartida en el DSM (Panel de control → Carpeta compartida → Permisos NFS).

### 10.6 Verificación / mantenimiento

- Prueba manual: `sudo systemctl start solar-manager-backup.service` + `journalctl -u solar-manager-backup.service -n 100 --no-pager`
- Comprobar próxima ejecución programada: `systemctl list-timers solar-manager-backup.timer`
- La lógica de rotación (borrado de backups antiguos por encima del límite) no se ha visto todavía "en producción" con más de N backups acumulados en ninguna categoría — pendiente de confirmar comportamiento real con el paso de las semanas/meses.
- Mejora pendiente (no implementada): notificación por email (reutilizando `notifier.py`/SMTP ya existente) si el timer falla una noche — actualmente el único rastro de un fallo es el journal de systemd.
