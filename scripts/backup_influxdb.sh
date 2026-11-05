#!/usr/bin/env bash
# ============================================================
# solar-manager - Backup diario de InfluxDB a la NAS, con rotación GFS
# Backup binario (influx backup) + export line protocol (gzip)
#
# Corre DENTRO del servicio `backup` de docker-compose.yml (imagen influxdb:2.7,
# que trae `influx` e `influxd`). El host no interviene: ni programador del
# sistema (launchd/systemd), ni montajes, ni sudo.
#
#   bash /backup.sh schedule  → proceso principal del contenedor: cada minuto
#                               comprueba si toca backup (ver `last_slot_epoch`)
#   bash /backup.sh run       → un backup ahora mismo (`make backup`)
#   bash /backup.sh check     → healthcheck: falla si el último diario es viejo
#
# Montajes (docker-compose.yml):
#   /var/lib/influxdb2  ← ./influxdb/data en SOLO LECTURA (para export-lp)
#   /nas                ← volumen NFS `nas-backups` (la NAS)
#   /logs               ← ./logs, compartido con la app: aquí se deja el resultado
#                         de cada backup para que la app lo vigile (app/backup_watch.py)
# ============================================================
set -euo pipefail

INFLUX_HOST="${INFLUX_HOST:-http://influxdb:8086}"
INFLUX_ORG="${INFLUX_ORG:-solar}"
INFLUX_BUCKET="${INFLUX_BUCKET:-solar-manager}"
ENGINE_PATH="/var/lib/influxdb2/engine"
NAS_BASE="/nas/solar-manager-backup"
BACKUP_AT="${BACKUP_AT:-23:57}"         # hora local (TZ del contenedor)

RETRY_AFTER_FAILURE_S=1800              # tras un fallo, no reintentar en 30 min
HEALTH_MAX_AGE_H=26                     # healthcheck: margen sobre las 24 h

# El share de la NAS tiene WriteOnce (Synology): cada fichero queda inmutable
# desde 2 h después de su última escritura y durante 30 días → no se puede borrar
# hasta ~30 d 2 h después de crearlo. Si la rotación intenta borrar una copia aún
# bloqueada, `rm` falla y el backup acaba en ERROR cada noche. Regla al cambiar el
# bloqueo en el DSM: RETENTION_DAILY ≥ días_bloqueo + 1 y 7·RETENTION_WEEKLY >
# días_bloqueo (mensual y anual nunca chocan).
RETENTION_DAILY=31    # ≥ 30 + 1
RETENTION_WEEKLY=5    # 5·7 = 35 > 30
RETENTION_MONTHLY=11
RETENTION_YEARLY=1

# Resultado para la app (app/backup_watch.py). Sin el montaje (pruebas sueltas) no
# se escribe nada.
STATUS_DIR="/logs"
STATUS_FILE="${STATUS_DIR}/backup_status.json"   # último intento: ok | error
LAST_OK_FILE="${STATUS_DIR}/backup_last_ok"      # hora del último backup correcto
RC_BUSY=75                                       # otro backup en curso: no es un fallo

LOG_PREFIX="[solar-manager-backup]"
log() { echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - $*"; }
err() { echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - ERROR: $*" >&2; }

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\t'/ }"
    s="${s//$'\n'/\\n}"
    printf '%s' "${s}"
}

# Deja el resultado para la app. Escritura atómica (tmp + mv): la app nunca lee un
# fichero a medias. Un fallo aquí se avisa pero no cambia el resultado del backup.
#   $1 = ok | error   $2 = nombre del backup   $3 = mensaje (error)
write_status() {
    [[ -d "${STATUS_DIR}" ]] || return 0
    local now
    now="$(date -Iseconds)"
    if printf '{"status": "%s", "at": "%s", "backup": "%s", "message": "%s"}\n' \
            "$1" "${now}" "$2" "$(json_escape "$3")" > "${STATUS_FILE}.tmp" \
        && mv "${STATUS_FILE}.tmp" "${STATUS_FILE}"; then
        if [[ "$1" == "ok" ]]; then
            echo "${now}" > "${LAST_OK_FILE}.tmp" && mv "${LAST_OK_FILE}.tmp" "${LAST_OK_FILE}" \
                || err "No se pudo escribir ${LAST_OK_FILE}"
        fi
    else
        err "No se pudo escribir ${STATUS_FILE}"
    fi
}

# Epoch del diario más reciente en la NAS (0 si no hay ninguno). Los directorios
# se llaman YYYYMMDD_HHMMSS: el orden alfabético es el cronológico.
newest_daily_epoch() {
    local newest
    newest="$(find "${NAS_BASE}/daily" -mindepth 1 -maxdepth 1 -type d \
                   -name '[0-9]*_[0-9]*' 2>/dev/null | sort | tail -n 1 || true)"
    if [[ -z "${newest}" ]]; then
        echo 0
        return
    fi
    newest="$(basename "${newest}")"
    date -d "${newest:0:8} ${newest:9:2}:${newest:11:2}:${newest:13:2}" +%s
}

# Epoch de la franja programada más reciente que ya ha pasado: hoy a BACKUP_AT si
# ya es esa hora, si no ayer a BACKUP_AT. Toca backup cuando el último diario es
# anterior a esa franja. Esta única regla cubre el horario normal y la recuperación
# de una noche perdida (equipo apagado o dormido a esa hora, contenedor parado):
# en cuanto el contenedor vuelve a correr, ve la franja sin backup y lo hace.
last_slot_epoch() {
    local now today_slot
    now="$(date +%s)"
    today_slot="$(date -d "today ${BACKUP_AT}" +%s)"
    if (( now >= today_slot )); then
        echo "${today_slot}"
    else
        date -d "yesterday ${BACKUP_AT}" +%s
    fi
}

copy_and_rotate() {
    local work="$1" ts="$2" category="$3" retention="$4"
    local dest_dir="${NAS_BASE}/${category}/${ts}"

    log "Copiando backup a categoría '${category}' (${dest_dir#/nas/})"
    mkdir -p "${dest_dir}"
    cp -r "${work}/binary" "${dest_dir}/"
    cp "${work}/export.lp.gz" "${dest_dir}/"

    local count
    count="$(find "${NAS_BASE}/${category}" -mindepth 1 -maxdepth 1 -type d | wc -l)"
    if (( count > retention )); then
        local to_delete=$((count - retention))
        log "Rotando '${category}': ${count} backups presentes, reteniendo ${retention}, eliminando ${to_delete}"
        find "${NAS_BASE}/${category}" -mindepth 1 -maxdepth 1 -type d \
            | sort \
            | head -n "${to_delete}" \
            | while read -r old_dir; do
                log "Eliminando backup antiguo: ${old_dir#/nas/}"
                rm -rf "${old_dir}"
            done
    fi
}

# Un backup completo. Se ejecuta SIEMPRE en un subshell `( do_backup … )`: aísla
# el `set -e` y el trap de limpieza, de modo que un fallo termina este backup sin
# tumbar el bucle de `schedule`.
#
# $1 = epoch de la franja a la que corresponde: decide las categorías GFS. En una
# recuperación (p. ej. el lunes a las 08:00 por la franja del domingo perdida) las
# categorías son las del DOMINGO, no las del momento de ejecución.
do_backup() {
    local slot="$1" ts="$2"
    local work bucket_id

    exec 9>/tmp/backup.lock
    if ! flock -n 9; then
        err "Ya hay un backup en curso"
        return "${RC_BUSY}"
    fi

    work="$(mktemp -d /tmp/backup-XXXXXX)"
    trap 'rc=$?; rm -rf "'"${work}"'"; if (( rc != 0 )); then err "El backup terminó con errores (exit code ${rc})"; fi' EXIT

    if [[ -z "${INFLUXDB_TOKEN:-}" ]]; then
        err "Falta INFLUXDB_TOKEN en el entorno del contenedor"
        return 1
    fi

    # El ID del bucket (lo exige export-lp) se resuelve por nombre en cada backup.
    bucket_id="$(influx bucket list --host "${INFLUX_HOST}" --org "${INFLUX_ORG}" \
                    --token "${INFLUXDB_TOKEN}" --name "${INFLUX_BUCKET}" --hide-headers \
                 | awk '{print $1}')"
    if [[ -z "${bucket_id}" ]]; then
        err "No se encontró el bucket '${INFLUX_BUCKET}' en ${INFLUX_HOST}"
        return 1
    fi

    # 1. Backup binario, online por HTTP (incluye los metadatos .bolt/.sqlite)
    log "Generando backup binario (${INFLUX_BUCKET} vía ${INFLUX_HOST})"
    # Su log (una línea INFO por shard, por stderr) solo se muestra si falla.
    if ! influx backup --host "${INFLUX_HOST}" --org "${INFLUX_ORG}" \
            --bucket "${INFLUX_BUCKET}" --token "${INFLUXDB_TOKEN}" \
            "${work}/binary" >"${work}/backup.log" 2>&1; then
        cat "${work}/backup.log" >&2
        return 1
    fi

    # 2. Export line protocol. `influxd inspect export-lp` es del daemon, no del CLI
    # (no sale en `influx --help`), exige --bucket-id y lee el engine directamente,
    # de ahí el montaje de ./influxdb/data en solo lectura. --compress escribe gzip
    # pero no añade `.gz`: se le da el nombre final desde el principio.
    log "Generando export line protocol (bucket ${bucket_id})"
    # Igual que arriba: su log (JSON por stderr) solo se muestra si falla.
    if ! influxd inspect export-lp --bucket-id "${bucket_id}" --engine-path "${ENGINE_PATH}" \
            --output-path "${work}/export.lp.gz" --compress 2>"${work}/export-lp.log"; then
        cat "${work}/export-lp.log" >&2
        return 1
    fi

    # 3. Categorías GFS de la franja
    local day dow apply_weekly=0 apply_monthly=0 apply_yearly=0
    day="$(date -d "@${slot}" +%F)"
    dow="$(date -d "${day}" +%u)"   # 1=lunes … 7=domingo
    if (( dow == 7 )); then apply_weekly=1; fi
    if [[ "$(date -d "${day} + 1 day" +%d)" == "01" ]]; then
        apply_monthly=1
        if [[ "$(date -d "${day}" +%m)" == "12" ]]; then apply_yearly=1; fi
    fi
    log "Franja ${day} -> diario:1 semanal:${apply_weekly} mensual:${apply_monthly} anual:${apply_yearly}"

    # 4. Copia a la NAS y rotación
    mkdir -p "${NAS_BASE}"/{daily,weekly,monthly,yearly}
    copy_and_rotate "${work}" "${ts}" daily "${RETENTION_DAILY}"
    if (( apply_weekly ));  then copy_and_rotate "${work}" "${ts}" weekly  "${RETENTION_WEEKLY}";  fi
    if (( apply_monthly )); then copy_and_rotate "${work}" "${ts}" monthly "${RETENTION_MONTHLY}"; fi
    if (( apply_yearly ));  then copy_and_rotate "${work}" "${ts}" yearly  "${RETENTION_YEARLY}";  fi

    log "Backup completado correctamente (${ts})"
}

# Ejecuta un backup y deja el resultado para la app. Su código de salida queda en
# RUN_RC y la función devuelve SIEMPRE 0: como reactiva `set -e` (que es global),
# un `return` distinto de 0 haría saltar `set -e` en el llamante y mataría el bucle.
# $1 = epoch de la franja
RUN_RC=0
run_one() {
    local slot="$1" ts errfile rc
    ts="$(date +%Y%m%d_%H%M%S)"
    errfile="$(mktemp /tmp/backup-stderr-XXXXXX)"

    # Ojo con `set -e`: ni `if ! ( do_backup … )` ni `( … ) || …` sirven, porque
    # bash lo desactiva DENTRO del subshell cuando es la condición de un `if` o un
    # operando de `||`; y el `set +e` necesario para recoger el código también lo
    # hereda el subshell. Por eso se reactiva dentro. Sin esto, un paso fallido no
    # paraba el backup y acababa en «completado correctamente» sin haber copiado
    # nada (probado). stderr se guarda aparte para enviar el motivo a la app.
    set +e
    ( set -e; do_backup "${slot}" "${ts}" ) 2>"${errfile}"
    rc=$?
    set -e
    cat "${errfile}" >&2

    if (( rc == 0 )); then
        write_status ok "${ts}" ""
    elif (( rc != RC_BUSY )); then
        # Las últimas líneas de stderr: el mensaje de la herramienta que falló y el
        # ERROR del trap con el código de salida.
        write_status error "${ts}" "$(grep -v '^[[:space:]]*$' "${errfile}" | tail -n 4 \
            | sed 's/^\[solar-manager-backup\] [0-9-]* [0-9:]* - //')"
    fi
    rm -f "${errfile}"
    RUN_RC="${rc}"
}

schedule() {
    log "Programador activo: backup diario a las ${BACKUP_AT} (${TZ:-UTC}) en ${NAS_BASE#/nas/}"

    # Primer arranque con resultado para la app: si aún no hay `backup_last_ok`
    # pero la NAS ya tiene diarios, se toma la hora del más reciente. Sin esto la
    # app avisaría de «ningún backup» hasta el siguiente backup.
    local newest
    newest="$(newest_daily_epoch)"
    if [[ -d "${STATUS_DIR}" && ! -f "${LAST_OK_FILE}" ]] && (( newest > 0 )); then
        date -d "@${newest}" -Iseconds > "${LAST_OK_FILE}"
        log "Inicializado ${LAST_OK_FILE#/logs/} con el último diario de la NAS"
    fi

    local last_failure=0 now slot
    while true; do
        now="$(date +%s)"
        slot="$(last_slot_epoch)"
        if (( $(newest_daily_epoch) < slot && now - last_failure >= RETRY_AFTER_FAILURE_S )); then
            log "Backup pendiente de la franja $(date -d "@${slot}" '+%Y-%m-%d %H:%M')"
            run_one "${slot}"
            if (( RUN_RC != 0 && RUN_RC != RC_BUSY )); then
                last_failure="${now}"
                err "Se reintentará en $((RETRY_AFTER_FAILURE_S / 60)) min"
            fi
        fi
        # Sondeo por reloj de pared en vez de un único `sleep` largo: si el equipo
        # se suspende, un sleep largo se alarga con él y el backup llegaría tarde.
        sleep 60
    done
}

check() {
    local newest age_h
    newest="$(newest_daily_epoch)"
    if (( newest == 0 )); then
        echo "No hay ningún backup diario en ${NAS_BASE#/nas/}"
        exit 1
    fi
    age_h=$(( ($(date +%s) - newest) / 3600 ))
    if (( age_h > HEALTH_MAX_AGE_H )); then
        echo "Último backup diario hace ${age_h} h (máx ${HEALTH_MAX_AGE_H} h)"
        exit 1
    fi
    echo "Último backup diario hace ${age_h} h"
}

case "${1:-}" in
    schedule) schedule ;;
    run)      run_one "$(date +%s)"; exit "${RUN_RC}" ;;
    check)    check ;;
    *)
        echo "Uso: $0 {schedule|run|check}" >&2
        exit 2
        ;;
esac
