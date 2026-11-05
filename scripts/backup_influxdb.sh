#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# solar-manager - Backup diario de InfluxDB con rotación GFS
# Backup binario (influx backup) + export line protocol (gzip)
# Volcado a NAS Synology vía NFS
#
# El host solo orquesta con `docker`; nada toca la NAS desde el sistema operativo:
#   1-2. backup y export se generan DENTRO del contenedor de InfluxDB (docker exec)
#        y se extraen a un staging local (docker cp);
#   4.   la copia a la NAS y la rotación corren en un contenedor EFÍMERO con la NAS
#        montada como volumen NFS de Docker.
# Así no hay mount/umount ni sudo en el host, y funciona igual en Linux (systemd)
# y en macOS (launchd, bash 3.2 de serie). En macOS además evita dos problemas de
# montar desde el sistema: la protección TCC «Volúmenes de red» (un bash lanzado
# por launchd no puede escribir en un volumen de red: `Operation not permitted`) y
# los ficheros AppleDouble `._*` que macOS crea junto a todo lo que escribe en NFS.
# ============================================================

# launchd arranca con un PATH mínimo sin /usr/local/bin (docker de Docker
# Desktop) ni Homebrew. Inocuo en Linux.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"

# --- Configuración ---
# El repo vive en rutas distintas en cada máquina: se deduce de la del script.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
CONTAINER="solar-manager-influxdb"
INFLUX_ORG="solar"
INFLUX_BUCKET="solar-manager"
INFLUX_BUCKET_ID="fa2d32fca4e9f966"
ENGINE_PATH_CONTAINER="/var/lib/influxdb2/engine"

STAGING_DIR="${PROJECT_DIR}/backups/tmp"

NFS_SERVER="numancia.metafrase.net"   # Synology (172.24.2.20)
NFS_EXPORT="/volume4/APP-BACKUPS"
NFS_OPTS="nfsvers=4"                  # el export solo admite la IP de este host
NAS_SUBDIR="solar-manager-backup"     # carpeta dentro del export

# El share APP-BACKUPS tiene WriteOnce (Synology): cada fichero queda inmutable
# desde 2 h después de su última escritura y durante 30 días → no se puede borrar
# hasta ~30 d 2 h después de crearlo. Si la rotación intenta borrar una copia aún
# bloqueada, `rm` falla y el script acaba en ERROR cada noche. Regla al cambiar el
# bloqueo en el DSM: RETENTION_DAILY ≥ días_bloqueo + 1 y 7·RETENTION_WEEKLY >
# días_bloqueo (mensual y anual nunca chocan).
RETENTION_DAILY=31    # ≥ 30 + 1
RETENTION_WEEKLY=5    # 5·7 = 35 > 30
RETENTION_MONTHLY=11
RETENTION_YEARLY=1

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

LOG_PREFIX="[solar-manager-backup]"

log() {
    echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - $*"
}

err() {
    echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - ERROR: $*" >&2
}

# --- Estado para cleanup ---
CONTAINER_TMP_BIN=""
CONTAINER_TMP_LP=""

cleanup() {
    local exit_code=$?

    if [[ -n "${CONTAINER_TMP_BIN}" ]]; then
        docker exec "${CONTAINER}" rm -rf "${CONTAINER_TMP_BIN}" 2>/dev/null || true
    fi
    if [[ -n "${CONTAINER_TMP_LP}" ]]; then
        docker exec "${CONTAINER}" rm -f "${CONTAINER_TMP_LP}" 2>/dev/null || true
    fi

    rm -rf "${STAGING_DIR:?}"/* 2>/dev/null || true

    if [[ ${exit_code} -ne 0 ]]; then
        err "El script terminó con errores (exit code ${exit_code})"
    else
        log "Backup completado correctamente"
    fi

    exit "${exit_code}"
}
trap cleanup EXIT

# --- Leer token desde .env (sin exponerlo en logs) ---
if [[ ! -f "${ENV_FILE}" ]]; then
    err "No se encuentra el archivo .env en ${ENV_FILE}"
    exit 1
fi

INFLUXDB_TOKEN="$(grep -E '^INFLUXDB_TOKEN=' "${ENV_FILE}" | head -n1 | cut -d'=' -f2-)"
if [[ -z "${INFLUXDB_TOKEN}" ]]; then
    err "No se pudo leer INFLUXDB_TOKEN desde ${ENV_FILE}"
    exit 1
fi

# --- Preparar staging local ---
mkdir -p "${STAGING_DIR}"

# --- 1. Backup binario ---
CONTAINER_TMP_BIN="/tmp/influx-backup-${TIMESTAMP}"
log "Generando backup binario en el contenedor: ${CONTAINER_TMP_BIN}"
docker exec "${CONTAINER}" influx backup \
    --org "${INFLUX_ORG}" \
    --bucket "${INFLUX_BUCKET}" \
    --token "${INFLUXDB_TOKEN}" \
    "${CONTAINER_TMP_BIN}"

log "Copiando backup binario a staging local"
docker cp "${CONTAINER}:${CONTAINER_TMP_BIN}" "${STAGING_DIR}/binary"

# --- 2. Export line protocol (comprimido con gzip) ---
CONTAINER_TMP_LP="/tmp/influx-export-${TIMESTAMP}.lp"
log "Generando export line protocol en el contenedor: ${CONTAINER_TMP_LP}"
docker exec "${CONTAINER}" influxd inspect export-lp \
    --bucket-id "${INFLUX_BUCKET_ID}" \
    --engine-path "${ENGINE_PATH_CONTAINER}" \
    --output-path "${CONTAINER_TMP_LP}" \
    --compress

log "Copiando export line protocol a staging local"
# El contenido ya está en formato gzip aunque el nombre no lleve .gz; lo renombramos al copiar
docker cp "${CONTAINER}:${CONTAINER_TMP_LP}" "${STAGING_DIR}/export.lp.gz"

# --- 3. Determinar qué categorías de retención aplican hoy ---
APPLY_DAILY=1
APPLY_WEEKLY=0
APPLY_MONTHLY=0
APPLY_YEARLY=0

DOW="$(date +%u)"  # 1=lunes ... 7=domingo
if [[ "${DOW}" -eq 7 ]]; then
    APPLY_WEEKLY=1
fi

if date -d tomorrow +%d >/dev/null 2>&1; then
    TOMORROW_DAY="$(date -d tomorrow +%d)"     # GNU date
else
    TOMORROW_DAY="$(date -v+1d +%d)"           # BSD date (macOS)
fi
if [[ "${TOMORROW_DAY}" == "01" ]]; then
    APPLY_MONTHLY=1
    CURRENT_MONTH="$(date +%m)"
    if [[ "${CURRENT_MONTH}" == "12" ]]; then
        APPLY_YEARLY=1
    fi
fi

log "Categorías aplicables hoy -> diario:${APPLY_DAILY} semanal:${APPLY_WEEKLY} mensual:${APPLY_MONTHLY} anual:${APPLY_YEARLY}"

# --- 4. Copia a la NAS y rotación, en un contenedor efímero ---
# Misma imagen que el contenedor de InfluxDB: ya está descargada (no hay pull) y es
# Debian con bash y coreutils de GNU. El volumen NFS es anónimo: `--rm` lo borra al
# terminar, y el montaje vive y muere con el contenedor.
HELPER_IMAGE="$(docker inspect -f '{{.Config.Image}}' "${CONTAINER}")"
log "Copiando a ${NFS_SERVER}:${NFS_EXPORT}/${NAS_SUBDIR} desde un contenedor efímero (${HELPER_IMAGE})"

# `-e VAR` sin valor toma la variable del entorno del cliente docker: hay que exportarlas.
export LOG_PREFIX TIMESTAMP APPLY_DAILY APPLY_WEEKLY APPLY_MONTHLY APPLY_YEARLY \
       RETENTION_DAILY RETENTION_WEEKLY RETENTION_MONTHLY RETENTION_YEARLY
docker run --rm -i \
    --entrypoint bash \
    -e TZ="Europe/Madrid" \
    -e LOG_PREFIX -e TIMESTAMP \
    -e APPLY_DAILY -e APPLY_WEEKLY -e APPLY_MONTHLY -e APPLY_YEARLY \
    -e RETENTION_DAILY -e RETENTION_WEEKLY -e RETENTION_MONTHLY -e RETENTION_YEARLY \
    -e BASE="/nas/${NAS_SUBDIR}" \
    -v "${STAGING_DIR}:/staging:ro" \
    --mount "type=volume,dst=/nas,volume-driver=local,volume-opt=type=nfs,\"volume-opt=o=addr=${NFS_SERVER},${NFS_OPTS}\",volume-opt=device=:${NFS_EXPORT}" \
    "${HELPER_IMAGE}" -s <<'IN_CONTAINER'
set -euo pipefail

log() { echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - $*"; }

mkdir -p "${BASE}"/{daily,weekly,monthly,yearly}

copy_and_rotate() {
    local category="$1"
    local retention="$2"
    local dest_dir="${BASE}/${category}/${TIMESTAMP}"

    log "Copiando backup a categoría '${category}' (${dest_dir#/nas/})"
    mkdir -p "${dest_dir}"
    cp -r /staging/binary "${dest_dir}/"
    cp /staging/export.lp.gz "${dest_dir}/"

    local count
    count="$(find "${BASE}/${category}" -mindepth 1 -maxdepth 1 -type d | wc -l)"
    if [[ "${count}" -gt "${retention}" ]]; then
        local to_delete=$((count - retention))
        log "Rotando '${category}': ${count} backups presentes, reteniendo ${retention}, eliminando ${to_delete}"
        # Los directorios se llaman YYYYMMDD_HHMMSS: orden alfabético = cronológico.
        find "${BASE}/${category}" -mindepth 1 -maxdepth 1 -type d \
            | sort \
            | head -n "${to_delete}" \
            | while read -r old_dir; do
                log "Eliminando backup antiguo: ${old_dir#/nas/}"
                rm -rf "${old_dir}"
            done
    fi
}

if [[ "${APPLY_DAILY}" -eq 1 ]];   then copy_and_rotate daily   "${RETENTION_DAILY}";   fi
if [[ "${APPLY_WEEKLY}" -eq 1 ]];  then copy_and_rotate weekly  "${RETENTION_WEEKLY}";  fi
if [[ "${APPLY_MONTHLY}" -eq 1 ]]; then copy_and_rotate monthly "${RETENTION_MONTHLY}"; fi
if [[ "${APPLY_YEARLY}" -eq 1 ]];  then copy_and_rotate yearly  "${RETENTION_YEARLY}";  fi
IN_CONTAINER

log "Todas las categorías aplicables procesadas correctamente"
