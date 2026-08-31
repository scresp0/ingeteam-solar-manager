# ARCHITECTURE.md — solar-manager

**Versión de la aplicación:** 1.76 (`app/version.py`)
**Fecha de generación:** 2026-08-18
**Método:** escrito leyendo el código fuente de `app/`, `scripts/`, `config.example.yaml`,
`docker-compose.yml`, `Dockerfile` y `Makefile`. Todo lo que aquí se afirma sale del
código; lo que no es determinable leyéndolo está marcado explícitamente como
**no verificable desde el código**.

Este documento describe **lo que el código hace**, no lo que pretende hacer. Donde el
código y sus propios comentarios/logs se contradicen, se señala la contradicción
(sección 10) en vez de reconciliarla.

**Fusión del addendum (2026-08-18):** el antiguo `ARCHITECTURE_addendum.md` se ha
integrado en la sección **9.6.2**. Ese contenido no procede del código sino del despliegue
real (servidor de producción y NAS), y se mantiene visiblemente separado del resto por esa
razón. El fichero de addendum queda obsoleto y debe retirarse.

---

## 1. Propósito y objetivos

Sistema automatizado de gestión de carga de una batería fotovoltaica doméstica
conectada a un inversor **Ingeteam INGECON SUN STORAGE 1PLAY TL-M**. Cada noche
decide si cargar la batería desde la red durante el tramo de tarifa valle
(00:00–08:00) en función de la previsión solar del día siguiente y del SOC actual,
y programa el inversor en consecuencia.

**Objetivos, por orden de prioridad declarado:**

1. **Minimizar el consumo de energía de red.**
2. **Minimizar los ciclos de carga/descarga de la batería.**

### Cómo se traducen esos objetivos en decisiones concretas

| Objetivo | Traducción en el código |
|---|---|
| Minimizar red | `decide_charge` solo carga de red el **déficit** calculado (`target_soc = min_soc + needed_for_day`), no hasta el 100 %. Lo que se espera cubrir con sol no se compra. |
| Minimizar red | Si mañana es día valle (fin de semana o festivo), **nunca** se carga de red: no hay ahorro tarifario que justificarlo (`valley_day_skip`). |
| Minimizar red | `decide_discharge` bloquea la descarga en días valle cuando el día siguiente (laborable) tendría déficit: preserva batería barata para cuando la red es cara. |
| Minimizar red | Calibración dinámica (`solar_bias_factor`, `risk_factor`, consumo nocturno y diario) para que el déficit estimado se acerque al real y no se compre de más ni de menos. |
| Minimizar ciclos | El objetivo de carga se expresa como **SOC objetivo** que el inversor mantiene durante el valle, no como una carga completa; y `to_charge_kwh = target_kwh − energy_stored` es 0 cuando ya hay energía suficiente. |
| (Objetivo no declarado, presente en el código) Minimizar calor | El controlador de corriente (sección 5) baja los amperios al **mínimo que llega a tiempo**, porque para una misma energía el calor es ∝ corriente. Este objetivo no está en la lista de dos, pero es el que rige un módulo entero. |

Un tercer criterio transversal, repetido en los comentarios del código, es
**"sin fallbacks silenciosos"**: un fallo de lectura debe producir una excepción o
un `None` que fuerce la ruta segura, nunca un valor por defecto plausible. Ver
`_get_select_value_by_label` (sección 7.3) y el validador `windows_must_cover_min_days`
(sección 6.4).

---

## 2. Estructura de módulos

### 2.1 Código de aplicación (`app/`)

| Fichero | Responsabilidad |
|---|---|
| `version.py` | Única línea: `VERSION = "1.76"`. Fuente de verdad del número de versión; se muestra en el log de arranque, en el HTML del dashboard y en `GET /api/version`. |
| `config.py` | Modelos Pydantic de toda la configuración, carga de `config.yaml`, aplicación de overrides por variables de entorno y validadores cruzados. Incluye la detección de claves renombradas (`find_deprecated_config_keys`). |
| `main.py` | Orquestación. Contiene `run` (ciclo nocturno), `run_recheck` (re-evaluación), `backfill_solar_history`, `run_charge_current_controller` (control de corriente) y `main()` (arranque del proceso). |
| `scheduler.py` | APScheduler `BlockingScheduler`: registra los jobs (ciclo nocturno, re-evaluaciones, backfill 00:30, control de corriente por intervalo, backup SCP) y maneja SIGTERM/SIGINT. |
| `decision.py` | Algoritmo puro, sin E/S: `decide_charge`, `decide_discharge`, `is_valley_day`, `reference_date` y los formateadores de log (`*_oneliner`, `*_summary`). |
| `solcast.py` | Cliente HTTP de la API de Solcast + caché JSON en disco. Agrega intervalos de 30 min a kWh/día (p10/p50/p90) y expone los intervalos de día 1 y de hoy. |
| `inverter.py` | Lectura MODBUS TCP del estado del inversor (SOC, SOH, tensión, potencia, temperatura, estados, SOC mínimo, corriente máxima de carga). Serializado con un lock global. |
| `automation.py` | Playwright/Chromium contra la web Vue.js del inversor: escribe 6.3.1 (carga), 6.3.2 (descarga) y 1.2 (corriente máxima); lee 6.3.1/6.3.2 y la versión de firmware. Perfiles de etiquetas por firmware. Serializado con un lock global. |
| `logger_reader.py` | Cliente HTTP del datalogger del inversor (registros minuto a minuto) y cálculo de los acumulados diarios y del perfil por franjas de 30 min. Define `house_power_w`. |
| `storage.py` | Escrituras y consultas en InfluxDB 2.x: 4 measurements, los cuatro parámetros dinámicos, el historial para la web y el último día con dato para el backfill. |
| `notifier.py` | Handler de logging en memoria que acumula el log de un ciclo y lo envía como email HTML (+ texto plano) por SMTP al terminar. |
| `backup.py` | Copia de seguridad por SCP a un servidor externo (DB + logs + config). **Implementado pero desactivado por defecto** (`backup.enabled: false`). |
| `web/server.py` | Aplicación FastAPI: dashboard + API JSON (estado, forecast, parámetros, historial, logs, config, export de BD, lanzamiento de ciclos/tests por SSE). |
| `web/templates/index.html` | Dashboard de una sola página (~2350 líneas): tiles, gráficos, tabla de corriente, visor de logs y formulario de configuración generado desde `CONFIG_SCHEMA` (6 grupos colapsables, 73 campos). |
| `web/templates/index-v1.html` | Versión antigua del dashboard, no referenciada por `server.py`. Código muerto. |
| `simulate_charge_current.py` | Ejecuta el controlador de corriente con `simulate=True` (lee y calcula, no escribe). Se lanza como subproceso desde la web. |
| `diag_forecast_bias.py` | Script de diagnóstico del sesgo del forecast. No lo invoca ningún job ni endpoint. |
| `test_*.py` | **Tests deterministas**: no tocan inversor, Solcast ni InfluxDB, así que un fallo siempre es una regresión. No son pytest: imprimen y devuelven código de salida. `make test` los ejecuta todos de una vez. |
| `diag_*.py` | **Diagnósticos**: necesitan el inversor o internet y solo informan de lo que encuentran (`diag_inverter`, `diag_solcast`, `diag_automation`, `diag_forecast_bias`). No afirman nada, así que un fallo suyo no es una regresión. Se separaron de `test_*` en v1.84. |
| `run_cycle.py` | Ejecuta el ciclo nocturno una vez (`--write` para el ciclo real). No es un test: `POST /api/cycle` lo lanza como subproceso, así que es camino de producción. Se llamaba `test_main.py` hasta v1.84. |

### 2.2 Fuera de `app/`

| Ruta | Responsabilidad |
|---|---|
| `scripts/backup_influxdb.sh` | Backup real de producción: binario + line protocol de InfluxDB a NAS por NFS, con rotación GFS. Independiente de la aplicación. |
| `scripts/systemd/*.{service,timer}` | Unidades systemd que ejecutan ese script a las 23:57 con `Persistent=true`. |
| `scripts/migrate_config.py` | Renombra las claves obsoletas de `config.yaml` (`make migrate-config`). |
| `Makefile` | `up/down/restart/build/logs/shell/migrate-config`. Inyecta `HOST_HOSTNAME=$(hostname)`, que `docker-compose.yml` requiere. |
| `docs/` | PDFs de Ingeteam (registros MODBUS, manual, API, notas de firmware) y un `.docx`. Documentación de referencia del fabricante, no código. |

---

## 3. Decisiones que toma el sistema

El sistema toma decisiones en **cinco** puntos independientes. Tres afectan al
inversor; dos solo a la base de datos.

| # | Decisión | Job | Escribe en el inversor | Escribe en InfluxDB |
|---|---|---|---|---|
| 3.1 | Cargar de red esta noche y hasta qué SOC | `run` (23:55) | 6.3.1 | `ciclo_carga`, `stats_diarias`, `solar_media_hora` |
| 3.2 | Bloquear la descarga mañana | `run` (23:55) | 6.3.2 | (lo anterior) |
| 3.3 | Re-evaluar ambas con el SOC del momento | `run_recheck` (19:00, 03:00) | 6.3.1 / 6.3.2 solo si difiere | **nada** |
| 3.4 | Corriente máxima de carga | `run_charge_current_controller` (cada `interval_min`) | 1.2 solo si difiere | `corriente_carga` (solo si cambia) |
| 3.5 | Qué días históricos rellenar | `backfill_solar_history` (arranque + 00:30) | nada | `stats_diarias`, `solar_media_hora` |

Todas las horas son locales (`system.timezone`, por defecto `Europe/Madrid`).

### 3.0 Entradas comunes: `_collect_decision_inputs` (`main.py`)

Las decisiones 3.1, 3.2 y 3.3 comparten la misma recolección de entradas, en este
orden:

1. **Previsión Solcast** (`get_two_day_forecast`, `get_day1_intervals`).
   Si `system.dry_run` está activo **no se llama a la API**: se usa una previsión
   ficticia fija `día1 = 10/20/30 kWh`, `día2 = 8/18/28 kWh`.
   Si la llamada real falla (`SolcastError`) se cae a `0/0/0 kWh` — un fallback
   deliberadamente pesimista que tiende a cargar.
2. **Estado del inversor por MODBUS** (`read_inverter_state`). Si falla, se usa
   `SOC = 50 %` como fallback y se registra un `ERROR`.
3. **`min_soc`**: el leído del inversor (holding 40126) si es > 0; si no, el de
   `charging.min_soc_pct`.
4. **Consumo nocturno** (dinámico o fallback), **consumo diario** (dinámico o
   fallback), **risk factor** (dinámico o fallback) y **factor de calibración
   solar** (dinámico o fallback). Ver sección 4.
5. **Guardia de coherencia:** si `night > daily`, se emite `WARNING` y se recorta
   `night = daily` (si no, `daytime = daily − night` se iría a 0 y el déficit
   colapsaría en silencio).

Con eso se construye un `DecisionInput` que alimenta a las dos funciones puras de
`decision.py`.

---

### 3.1 Decisión de carga desde red — `decide_charge`

**Cuándo:** en `run` (`tariff.schedule_at`, 23:55) y en cada `run_recheck`.

**Entradas:** `forecast_day1` (p10/p50/p90), `soc_actual_pct`, `battery_capacity_kwh`,
`min_soc_pct`, `max_soc_pct`, `daily_consumption_kwh`, `night_consumption_kwh`,
`safety_margin_kwh`, `risk_factor`, `solar_bias_factor`, `weekend_days`, `holidays`.

**Fecha de referencia.** `reference_date(night_cutoff_hour)`: si la hora local actual
es `< night_cutoff_hour` (por defecto 8), se resta un día. Así, a las 23:55 del jueves
y a las 03:00 del viernes, "mañana" (`día 1`) es el viernes en ambos casos.

**Lógica exacta:**

```
energy_stored  = soc_actual_pct / 100 * battery_capacity_kwh
energy_min     = min_soc_pct    / 100 * battery_capacity_kwh
tomorrow       = reference_date(night_cutoff_hour) + 1 día

# Puerta 1 — día valle
si is_valley_day(tomorrow):            # weekday ∈ weekend_days  OR  fecha ∈ holidays
    → NO CARGAR  (valley_day_skip = True), fin.

solar_effective = (p10 · risk_factor + p50 · (1 − risk_factor)) · solar_bias_factor
energy_at_dawn  = max(energy_min, energy_stored − night_consumption_kwh)
energy_usable   = max(0, energy_at_dawn − energy_min)
daytime_consumption = max(0, daily_consumption_kwh − night_consumption_kwh)
needed_for_day  = max(0, daytime_consumption + safety_margin_kwh − solar_effective)
deficit         = max(0, needed_for_day − energy_usable)

# Puerta 2 — sin déficit
si deficit == 0:  → NO CARGAR, fin.

# Objetivo
target_kwh_raw = energy_min + needed_for_day
target_soc_raw = target_kwh_raw / battery_capacity_kwh · 100
target_soc     = clamp(target_soc_raw, min_soc_pct, max_soc_pct)
clamped        = |target_soc − target_soc_raw| > 0.01
target_kwh     = target_soc / 100 · battery_capacity_kwh
to_charge_kwh  = max(0, target_kwh − energy_stored)
→ CARGAR hasta target_soc
```

**Notas de diseño recogidas de los comentarios del código:**

- Se resta `night_consumption` a `daily_consumption` porque el consumo nocturno ya
  está descontado de la batería vía `energy_at_dawn`; contarlo también dentro de
  `daily` lo contaría dos veces (corregido en v1.41 según el comentario).
- `energy_at_dawn_kwh` del resultado: **cuando se carga, se devuelve `target_kwh`**
  (la energía real que habrá al amanecer tras cargar), no el valor hipotético sin
  carga. Los dos campos se llaman igual pero significan cosas distintas según la
  rama — hay que leerlo con `charge_needed` delante.
- `to_charge_kwh` es 0 si `target_kwh ≤ energy_stored`. Eso **no significa que no
  entre energía de red**: el inversor mantiene el SOC en el objetivo durante el
  valle en vez de dejarlo caer, así que el consumo nocturno se cubre desde la red.
  Para medir el impacto real hay que mirar `target_soc_pct`.

**Qué escribe en el inversor:** sección **6.3.1** vía Playwright (`set_charge_schedule`),
y solo si `_needs_update` lo considera necesario (ver 3.6):

- `charge_needed = False` → Programación 1 y 2 = `Desactivado`.
- `charge_needed = True` → Prog 1 = *Entre semana (L-V)* y Prog 2 = *Fin de semana (S-D)*,
  ambas con `SOC Grid = round(target_soc_pct)` y franja **00:01 → 07:59**
  (`VALLEY_HOUR_ON=0, VALLEY_MIN_ON=1, VALLEY_HOUR_OFF=7, VALLEY_MIN_OFF=59`).

---

### 3.2 Decisión de bloqueo de descarga — `decide_discharge`

**Cuándo:** junto a `decide_charge`, en `run` y en `run_recheck`.

**Puerta de entrada:** si mañana (día 1) **no** es día valle → `discharge_blocked = False`
("mañana es día laborable — descarga libre"). El bloqueo solo se plantea en fines de
semana y festivos, cuando la tarifa es valle 24 h y no hay incentivo horario para
descargar.

**Lógica exacta — dos pasadas:**

```
solar_day1  = (p10₁·rf + p50₁·(1−rf)) · bias
solar_day2  = (p10₂·rf + p50₂·(1−rf)) · bias
needed_day2 = max(0, daily_consumption + safety_margin − solar_day2)

# Pasada 1 — CRITERIO (escenario sin bloqueo, conservador)
energy_end_no_block    = max(energy_min, energy_stored + solar_day1 − daily_consumption)
energy_usable_no_block = max(0, energy_end_no_block − energy_min)
deficit_no_block       = max(0, needed_day2 − energy_usable_no_block)

si deficit_no_block == 0:  → NO BLOQUEAR, fin.

# Pasada 2 — VALORES REPORTADOS (escenario con bloqueo)
energy_end_day1    = max(energy_min, energy_stored + solar_day1 − (daily_consumption − night_consumption))
energy_usable_day2 = max(0, energy_end_day1 − energy_min)
deficit_day2       = max(0, needed_day2 − energy_usable_day2)
→ BLOQUEAR
```

La segunda pasada existe porque, con la descarga bloqueada, el consumo nocturno pasa
a la red y la batería retiene `night_consumption_kwh` extra: el `deficit_day2_kwh`
reportado refleja el déficit **después** del bloqueo. El texto de `reason`, en cambio,
muestra siempre `deficit_no_block`, para explicar por qué se decidió bloquear.

**Qué escribe en el inversor:** sección **6.3.2** vía `set_discharge_schedule`.

Semántica del inversor (documentada en el código como verificada con el usuario el
2026-06-14): la franja `Hora On → Hora Off` es el periodo en que la descarga está
**PERMITIDA**; fuera de ella la batería no descarga. `Desactivado` = sin restricción =
descarga libre.

Valores que el código escribe (constantes `DISC_*` en `automation.py`):

| `discharge_blocked` | Programación 1 | Programación 2 |
|---|---|---|
| `False` | `Desactivado` | `Desactivado` |
| `True` | *Entre semana (L-V)*, **08:00 → 23:59** (bloquea el valle 00:00–08:00) | *Fin de semana (S-D)*, **00:00 → 00:01** (franja de 1 min → bloqueo de facto 24 h) |

> ⚠️ El *docstring* y el mensaje de log INFO de `set_discharge_schedule` dicen
> `L-V 00:01–07:59 · S-D 00:01–23:59`, que es **lo contrario** de lo que escriben las
> constantes. Ver incoherencia **I-2** en la sección 10.

**Verificación read-back:** tras pulsar "Escribir", `_verify_discharge_written` vuelve a
pulsar "Leer" y comprueba que 6.3.2 quedó exactamente en la configuración pretendida;
si no coincide, lanza `AutomationError`. La lectura (`_read_discharge_state`) devuelve
además `recognized=False` cuando la configuración activa no es ninguna de las dos
canónicas, lo que fuerza una reescritura correctiva.

---

### 3.3 Re-evaluación de la decisión — `run_recheck`

**Cuándo:** un job por cada hora de `tariff.schedule_recheck_at` (ejemplo del
`config.example.yaml`: `"19:00, 03:00"`). El validador de `TariffConfig` acepta un
string con comas, una lista YAML o una sola hora; vacío/`null` desactiva la
re-evaluación; un formato inválido **aborta el arranque** en vez de desactivarse en
silencio.

**Qué hace:** repite `_collect_decision_inputs` + `decide_charge` + `decide_discharge`
con el SOC del momento (que ya incorpora consumo y producción reales), lee el estado
real del inversor y **solo reconfigura si difiere**. Si no hay cambios y no es
`dry_run`, llama a `notifier.discard()` y sale sin enviar email.

**Qué NO hace:** no escribe en InfluxDB — ni `ciclo_carga`, ni `stats_diarias`, ni
`solar_media_hora`. El motivo, según el docstring: un segundo `ciclo_carga` del mismo
día tendría timestamp en madrugada UTC, y el JOIN con `stats_diarias`
(`forecast_date = ciclo_UTC.date() + 1`) apuntaría al día equivocado, envenenando el
risk factor y el bias dinámicos.

**Caveats que el propio código documenta:**

1. **Decisión optimista por la tarde.** `energy_at_dawn = energía_actual − night_consumption`,
   y `night_consumption` solo cubre 00:00–07:59. A las 19:00 el consumo de la
   tarde-noche no se descuenta en ninguna parte, así que el SOC de partida es
   demasiado alto y la decisión tiende a "no cargar". El ciclo de las 23:55 es el
   canónico y corrige.
2. **El bloqueo de descarga se adelanta.** Con `discharge_blocked=True`, la Prog 2
   (fin de semana) permite descargar solo 00:00–00:01, es decir bloquea ya. A las
   23:55 eso afecta a 5 minutos; a las 19:00 de un sábado, la casa tira de red desde
   las 19 h hasta medianoche.

---

### 3.4 Corriente máxima de carga

Sección 5 completa.

---

### 3.5 Backfill de producción histórica — `backfill_solar_history`

**Cuándo:** en un hilo daemon al arrancar el proceso, y en un job APScheduler a las
**00:30**.

**Lógica:**

```
si not influxdb.enabled → return
yesterday = hoy − 1
floor_day = yesterday − (MAX_BACKFILL_DAYS − 1)        # MAX_BACKFILL_DAYS = 59

# El día de arranque es el MÁS ATRASADO de los cuatro campos
last_by_field = [get_last_real_solar_date(field=f)
                 for f in (real_kwh, house_kwh, grid_import_kwh, grid_export_kwh)]
last_stored = None si algún campo es None, si no min(last_by_field)
start_day = (last_stored + 1) si last_stored else floor_day
start_day = max(start_day, floor_day)

si start_day > yesterday → sin huecos, fin.
para cada día en [start_day … yesterday]:
    stats = get_daily_stats(día)          # datalogger HTTP
    write_daily_stats(stats)              # stats_diarias
    write_half_hour_stats(stats)          # solar_media_hora (48 puntos)
```

Errores por día se registran como `WARNING` y el bucle continúa. No toca `ciclo_carga`
ni el día en curso. Reescribir un día ya presente es idempotente (mismo measurement +
mismo timestamp → InfluxDB sobrescribe).

El motivo de consultar **cada campo por separado** está en el comentario: los campos se
han ido añadiendo en momentos distintos (`house_kwh`, `grid_import_kwh` y
`grid_export_kwh` en v1.73), así que mirando solo `real_kwh` los días antiguos se
darían por completos y los campos nuevos no se rellenarían nunca.

`MAX_BACKFILL_DAYS = 59` está justificado en el código: el datalogger del inversor es
una **ventana rodante de ~59 días**, así que lo que no se copie antes de que un día
salga de ella se pierde para siempre.

---

### 3.6 Escritura condicionada al inversor — `_needs_update` / `_apply_inverter_decisions`

Antes de escribir, `run` y `run_recheck` leen el estado real vía
`read_inverter_schedule` (`_read_schedule_before`) y lo comparan:

```
charge_soc_target = round(target_soc_pct) si charge_needed, si no 0

charge_needs_update    = schedule_before is None
                       or schedule_before.charge_active != charge.charge_needed
                       or (charge.charge_needed and schedule_before.charge_soc_pct != charge_soc_target)

discharge_needs_update = schedule_before is None
                       or not schedule_before.discharge_recognized
                       or schedule_before.discharge_blocked != discharge.discharge_blocked
```

Se escribe si `dry_run` **o** el flag correspondiente. Es decir: **en `dry_run`
siempre se recorre el flujo de escritura** (navega y rellena, pero `set_*` no
pulsa "Escribir").

Que `schedule_before is None` fuerce la escritura es deliberado: si la lectura falla,
se reescribe (fail-safe). El fallo de lectura llega como `None` porque
`read_inverter_schedule` captura la excepción que lanza `_get_select_value_by_label`.

Se emiten dos líneas de log INFO que la web y el email parsean:

```
[ANTES]   Carga (6.3.1): DESACTIVADA | Descarga (6.3.2): LIBRE
[DESPUÉS] Carga (6.3.1): ACTIVA (SOC 85%) | Descarga (6.3.2): BLOQUEADA
```

`[DESPUÉS]` refleja la **intención** para la carga (6.3.1 no tiene read-back); para la
descarga sí hay verificación real (`_verify_discharge_written`).

---

## 4. Parámetros dinámicos (calibración desde InfluxDB)

Cuatro parámetros se calculan del histórico; el valor de `config.yaml` es solo el
fallback mientras no haya suficientes días. Todos siguen el mismo patrón: una ventana
`*_window_days` y un mínimo de días con dato dentro de ella,
`*_min_days_in_window`. Si no se alcanza el mínimo, se devuelve `None` y el llamante
usa el fallback (registrándolo en el log).

| Parámetro | Función | Fuente | Fórmula | Filtro |
|---|---|---|---|---|
| Consumo nocturno | `get_avg_night_consumption` | `stats_diarias.night_consumption_kwh` | media | `> 0.5` |
| Consumo diario | `get_avg_daily_consumption` | `stats_diarias.consumption_kwh` | media | `> 0.5` |
| Risk factor | `get_dynamic_risk_factor` | JOIN `ciclo_carga` × `stats_diarias` | por día `rf = (real − p50)/(p10 − p50)`, clamp `[0,1]`; media | descarta días con `p10 ≥ p50` |
| Bias solar | `get_dynamic_solar_bias` | mismo JOIN | por día `f = real / p50`; media, clamp `[0.5, 1.5]` | descarta `p50 ≤ 0.5` |

**El JOIN** (`_forecast_real_pairs`): para cada punto de `ciclo_carga` (que se escribe
~22–23 h UTC), `forecast_date = timestamp_UTC.date() + 1 día`, que coincide con el
timestamp de `stats_diarias` (medianoche UTC del día de datos). Se consultan
`window_days + 2` días "por margen UTC+1/+2". Un ciclo ejecutado entre 01:00 y 07:59
UTC quedaría emparejado con el día equivocado — de ahí que `run_recheck` no escriba
`ciclo_carga`.

**Dónde se aplica cada uno:**

- Consumo nocturno y diario → `DecisionInput` (`decide_charge` y `decide_discharge`).
- Risk factor → ponderación p10/p50 en `_solar_effective`.
- Bias solar → multiplicador dentro de `_solar_effective`, **y también** en el
  controlador de corriente (`_solar_surplus_window`) y en los totales que muestra la
  web (`_current_solar_bias`).

**Nunca se pre-aplica al dato persistido:** `write_cycle` y `write_half_hour_forecast`
guardan el forecast **crudo** de Solcast. Si se guardara ya calibrado, el cálculo del
bias sobre ese dato se realimentaría.

**Un quinto valor derivado**, `get_avg_post_valley_consumption`
(= `consumption_kwh − night_consumption_kwh` por día, filtro `> 0.5`), no calibra
ninguna decisión: solo es el fallback del consumo de la vivienda en el controlador de
corriente. Usa los parámetros de ventana del **consumo nocturno**.

---

## 5. Control dinámico de la corriente de carga (detalle)

Módulo: `main.run_charge_current_controller` + `_compute_target_charge_current` +
`_solar_surplus_window` + `_min_current_for_surplus` + `_house_power_estimate` +
`_productive_window_end`. Escritura en `automation.set_charge_current`.

**Propósito declarado en el código:** llevar la "Corriente Máxima de Carga" de la
batería al **mínimo necesario** para llegar al tope de carga a tiempo, porque para una
misma energía el **calor es ∝ corriente**. Cargar despacio cuando sobra tiempo reduce
el calentamiento del inversor y de las baterías.

> ⚠️ **Unidades.** La corriente es del lado **batería DC (~50 V)**, no de la red AC
> (230 V). El registro admite 1–66 A.

### 5.1 Flujo de un tick

```
si not charge_current.enabled y not simulate → return

state = read_inverter_state()                     # MODBUS; si falla → WARNING y return
si state.charge_current_max_a < 1 → WARNING "tope ilegible" y return   # no escribir a ciegas

now  = ahora (tz de config);  hour = now.hour + now.minute/60
current = round(state.charge_current_max_a)

si hour < _VALLE_END_HOUR:                        # v1.79: en valle la decisión nunca
    window, solar_end = None, cc.productive_window_end_hour   # consulta el excedente solar
si no:
    window    = _solar_surplus_window(cfg, now)   # perfil de excedente, o None
    solar_end = window.end_hour  si window else  _productive_window_end(cfg)
schedule_state = _load_schedule_state()           # /app/logs/inverter_schedule_state.json

target, calculated, mode = _compute_target_charge_current(...)

si simulate → log y return
si current == target → log DEBUG y return         # sin churn
set_charge_current(target)                        # Playwright, sección 1.2
verify = read_inverter_state()                    # read-back por MODBUS de 40087
write_charge_current(...)                         # InfluxDB, measurement corriente_carga
```

En `dry_run` se registra el cambio en InfluxDB con `dry_run=True, verified=False` y no
se verifica. En `simulate` no se escribe nada, ni en el inversor ni en InfluxDB.

### 5.2 Modos y sus condiciones de frontera

Constantes: `_VALLE_START_HOUR = 0.0`, `_VALLE_END_HOUR = 8.0`.

Variables auxiliares calculadas **antes** de elegir modo:

```
charging_valle = 0.0 ≤ hour < 8.0  AND  schedule_state is not None  AND  schedule_state.charge_needed
charging_solar = 8.0 ≤ hour < solar_end
window_end     = 8.0        si charging_valle
                 solar_end   si charging_solar
                 None        en otro caso
```

El orden de evaluación es **BALANCE → VALLE → SOLAR → IDLE**; el primero que cumple gana.

| Modo | Condición exacta | Corriente devuelta | Etiqueta (`mode`) |
|---|---|---|---|
| **BALANCE** | `battery_balance` **y** `window_end is not None` **y** `balance_soc_pct ≤ soc < max_soc_pct` | `amps_for((max_soc−soc)/100·cap, window_end−hour, floor=balance_floor_a)` | `BALANCE` |
| **BALANCE (fino)** | igual, y además `soc ≥ balance_soc_pct_2` | igual pero `floor = max(1, balance_floor_a // 2)` | `BALANCE (fino)` |
| **VALLE** | `0 ≤ hour < 8` y `schedule_state.charge_needed` y energía pendiente > 0 | `amps_for((target_soc−soc)/100·cap, 8.0−hour)` | `VALLE` |
| VALLE alcanzado | `0 ≤ hour < 8`, carga pedida, `energy ≤ 0` | **`current`** (no toca) | `VALLE (objetivo alcanzado — sin cambios)` |
| IDLE de valle | `0 ≤ hour < 8` sin `charge_needed` (o sin fichero de estado) | **`current`** | `IDLE (valle sin carga — sin cambios)` |
| SOLAR llena | `8 ≤ hour < solar_end`, `soc ≥ max_soc` | **`current`** | `SOLAR (batería llena — sin cambios)` |
| **SOLAR temp** | `8 ≤ hour < solar_end`, `temp_gate_enabled` y `battery_temp_c ≤ hot_threshold_c` | **`max_a`** | `SOLAR (temp X≤Yº → máx)` |
| **SOLAR simulado** | `8 ≤ hour < solar_end`, hay `window.surplus` | `_min_current_for_surplus(...)` acotado | `SOLAR (rampa al excedente previsto)` / `(máx — el excedente supera el tope de corriente)` / `(limitada por sol — más A no captan más)` |
| **SOLAR plano** | `8 ≤ hour < solar_end`, sin `window` | `amps_for((max_soc−soc)/100·cap, solar_end−hour)` | `SOLAR (rampa a fin de producción — sin forecast)` |
| **IDLE** | resto (`hour ≥ solar_end`) | **`current`** | `IDLE (sin carga — sin cambios)` |

**Fronteras que conviene tener presentes:**

- A `hour = solar_end` exacto se sale de SOLAR (comparación estricta `<`) y se pasa a
  IDLE, que **congela** el último valor escrito: no lo baja al `floor_a`. Si la ventana
  se cerró con una corriente alta, ese valor persiste hasta el valle siguiente.
- BALANCE necesita `window_end`, es decir, necesita estar dentro de una ventana de
  carga. Fuera de ella no actúa aunque el SOC esté en el rango.
- La puerta de temperatura se evalúa **antes** que la simulación de excedente y la
  cortocircuita por completo: con la batería fría se va a `max_a` sin mirar el sol.
- El modo SOLAR apunta siempre a `charging.max_soc_pct`, no al `target_soc` de la
  decisión nocturna. El objetivo nocturno solo se usa en VALLE.
- VALLE depende del fichero `inverter_schedule_state.json`, no de la decisión en
  memoria (ver incoherencia **I-4**).

### 5.3 Cascada de resolución del fin de la ventana productiva (`solar_end`)

Tres niveles. Cada nivel solo se usa si el anterior falla, y **cada nivel falla por un
motivo distinto**:

```
Nivel 1 — FORECAST (window.end_hour)
   _solar_surplus_window() → get_today_intervals() de la caché Solcast
   end_hour = fin de la última franja futura con  p50 · bias · 0.5h  > 0.02 kWh
   FALLA SI: get_today_intervals lanza SolcastError (caché ausente/expirada y la API
             no responde, 401/404/429, timeout, error de conexión) → devuelve None.
   CASO ESPECIAL: si el forecast SÍ carga pero ya no quedan franjas productivas hoy,
             devuelve SolarWindow(surplus=[], end_hour=0.0) — que NO es None:
             solar_end = 0.0 y el controlador cae directamente a IDLE.
             Este camino NO consulta los niveles 2 ni 3.

Nivel 2 — HISTÓRICO (storage.get_production_window_end_hour)
   Sobre solar_media_hora.real_kwh de los últimos charging.solar_bias_window_days días:
   suma la producción por franja de 30 min entre todos los días, y devuelve el fin de
   la primera franja cuya suma acumulada alcanza  productive_window_pct %  del total.
   Redondeado a 2 decimales, tope 24.0.
   FALLA SI: influxdb.enabled = False;  menos de 3 días distintos con dato
             (min_days=3, fijo en el código, no configurable);  total = 0;
             cualquier excepción de la consulta;  StorageError. → None.

Nivel 3 — CONFIG (charge_current.productive_window_end_hour, por defecto 17.0)
   Valor literal. No falla.
```

**Consecuencias de que se use el nivel 2 o el 3** (`window is None`, o sea sin
forecast):

- No hay perfil de excedente → la rama SOLAR usa el **reparto lineal**
  (`amps_for(energy, solar_end − hour)`), no la simulación.
- `solar_end` es el denominador de ese reparto: **cuanto más pequeño, más corriente**.
  Con 5 kWh pendientes a las 15:00 y V ≈ 50: `solar_end = 17` → ~50 A;
  `solar_end = 20` → ~33 A.
- `solar_end` delimita también el modo (`8 ≤ hour < solar_end`) y la ventana de BALANCE.
- El nivel 2 con `productive_window_pct = 90` devuelve por diseño una hora **anterior**
  a la puesta de sol: ignora el 10 % final de cola de producción. Es conservador
  (acorta el horizonte → sube la corriente → corta antes el modo SOLAR).

### 5.4 El perfil de excedente — `_solar_surplus_window`

```
intervals = solcast.get_today_intervals()          # franjas de 30 min de HOY (de la caché)
bias      = get_dynamic_solar_bias(...) o charging.solar_bias_factor
house_kw, house_source = _house_power_estimate()
house_per_interval = house_kw · 0.5                # kWh que consume la casa en 30 min (persistencia)
house_profile = get_house_power_profile(...)       # {"HH:MM": kWh/franja} o None

para cada franja con period_end > ahora:
    eff = pv_estimate · bias · 0.5                 # kWh de la franja, p50 CALIBRADO
    si eff > 0.02:
        gross   += eff
        house_kwh_slot = _house_consumption_for_slot(slot_start, ahora,
                                                       house_per_interval, house_profile)
        surplus.append(max(0, eff − house_kwh_slot))
        end_hour = hora local del fin de esa franja
```

Dos decisiones explícitas en los comentarios:

1. **Se usa p50 · bias, sin ponderar con p10 ni con `risk_factor`.** El pesimismo del
   risk factor tiene sentido en `decide_charge`, que decide una vez a las 23:55 y no
   revisa hasta las 08:00; en un lazo que se recalcula cada `interval_min` no compra
   seguridad, solo cuesta calor. El único margen deliberado es `charge_current.margin`.
2. **El fin de producción se calcula con la producción BRUTA**: una franja sigue
   produciendo aunque la casa se coma todo lo que da.

**Estimación del consumo de la vivienda — `_house_power_estimate`:**

- **Camino principal (`"medido"`):** `get_recent_house_power` devuelve la **mediana** de
  `house_power_w` sobre los últimos `house_power_window_min` registros del datalogger
  de HOY. Predictor de persistencia: lo consumido en la última hora es la mejor
  estimación de la próxima, porque el aire acondicionado (lo que domina la varianza en
  verano) tiene inercia de horas. Mediana y no media para que un horno de 20 min no deje
  el excedente previsto en cero.
- **Caché:** `house_power_cache_min` (por defecto 15) reutiliza la última lectura durante
  ese tiempo. Cada lectura descarga el **día completo** del datalogger (el endpoint no
  admite rangos), así que con `interval_min` bajo el tráfico contra el inversor se
  dispara. Un fallo **no** se cachea: se devuelve `None` y el siguiente tick reintenta.
- **Fallback (`"media"`):** `get_avg_post_valley_consumption() / 16.0` kW, o si tampoco
  hay, `(average_daily_consumption_kwh − night_consumption_kwh) / 16.0`. El propio
  docstring advierte de que **sobreestima**: reparte sobre las horas de sol un total que
  incluye la tarde-noche.

**Mezcla con el perfil histórico por franja — `_house_consumption_for_slot` (v1.77):**

La persistencia (mediana de la última hora) solo sabe lo que la casa ha consumido
**hoy hasta ahora**; aplicada de forma plana a las franjas de toda la tarde asume que
el ritmo de la mañana se mantiene, lo cual es falso en verano (el aire acondicionado
dispara el consumo de tarde muy por encima del de la mañana — caso real del
2026-08-18: persistencia matutina 0,5-0,6 kW → controlador a 33A (suelo) toda la
mañana con 21,5 kWh de excedente previsto → consumo real de tarde 1,1-2,3 kW →
batería se queda al 89%, nunca llega al 100%).

```
house_kwh_slot(slot_start, ahora, persistencia_kwh, profile):
    si profile es None o no tiene la franja "HH:MM" de slot_start:
        return persistencia_kwh
    horas_por_delante = max(0, (slot_start − ahora) en horas)
    w = max(0, 1 − horas_por_delante / 2.0)        # _HOUSE_PROFILE_BLEND_HOURS
    return w · persistencia_kwh + (1−w) · profile["HH:MM"]
```

Peso 1 (pura persistencia) en la franja actual — capta si hoy hace más o menos calor
de lo normal —, decayendo linealmente a peso 0 (puro histórico) a partir de 2 horas
vista. `get_house_power_profile` (storage.py) calcula ese histórico como la
**mediana** de `solar_media_hora.house_kwh` en cada franja de 30 min sobre los
últimos `house_profile_window_days` días (30 por defecto); devuelve `None` si la
franja peor cubierta tiene menos de `house_profile_min_days_in_window` (14) días —
mismo criterio conservador que el resto de parámetros dinámicos — y entonces el
controlador se comporta exactamente como antes de v1.77 (pura persistencia).

**Dos guards para no calcular la ventana cuando el resultado se va a tirar (v1.79):**
antes de v1.79 el tick construía la ventana solar completa (Solcast + `solar_bias`
de InfluxDB + lectura del datalogger + perfil histórico de InfluxDB) **en cada
ejecución, 24 h/día** — incluidas las ~13 h/día (valle + tarde-noche) en las que el
resultado nunca llega a usarse en la decisión. Verificado ejecutando
`_compute_target_charge_current` con `window`/`solar_end` contradictorios durante
el valle y comprobando que el resultado no cambia (mismo (amperios, modo) con
`window=None` que con una ventana disparatada — la rama VALLE usa `_VALLE_END_HOUR`
fijo, y `window_end` de BALANCE-en-valle también, nunca el `solar_end` real).

- **Valle (`hour < _VALLE_END_HOUR`):** guard por reloj en `run_charge_current_controller`,
  antes de llamar a `_solar_surplus_window` — cero riesgo, la decisión de esa franja
  horaria nunca consulta el excedente solar. No se llama ni siquiera al fallback
  barato `_productive_window_end` (también hace una query a InfluxDB): tan inútil en
  valle como la ventana completa. `solar_end` se deja en
  `cc.productive_window_end_hour` (constante de config, sin I/O) — valor sin uso real,
  solo para no dejar la variable sin definir.
- **Tarde-noche (tras el fin de producción):** el forecast en sí no basta para
  distinguir esto del valle — a las 02:00 el forecast de HOY sigue teniendo horas de
  sol por delante más tarde ese mismo día, así que "¿queda forecast > 0 hoy?" daría
  que sí. Por eso el guard vive DENTRO de `_solar_surplus_window`: tras pedir
  `intervals` (el primer paso, ya necesario), si **ninguna** franja futura tiene
  `pv_estimate > 0` — exacto `0.0` en Solcast de noche, sin ambigüedad de `bias` que
  valga — se corta ahí, antes de `get_dynamic_solar_bias`, `_house_power_estimate`
  (la lectura del datalogger, la costosa) y `get_house_power_profile`.

Los dos guards se probaron con `_solar_surplus_window` real, monkeypatcheando las
cuatro funciones que llama, para confirmar que con forecast todo a cero NO se llaman
(`app/test_charge_current.py`).

### 5.5 Las dos fórmulas de corriente

**(a) Reparto lineal — `amps_for(energy_kwh, hours, floor_a=None)`**

Usada en VALLE, BALANCE y SOLAR-sin-forecast.

```
hours = max(0.25, hours)                       # suelo de 15 min
i     = energy_kwh · 1000 / (V · hours) · margin
raw   = round(i)                               # "calculado"
target= max(floor_a or cc.floor_a, min(cc.max_a, raw))   # "fijado"
```

- `V` = `state.battery_voltage_v` si es > 30, si no **50.0** por defecto.
- El suelo de `hours` en 0.25 hace que, en el último cuarto de hora de la ventana, la
  fórmula se dispare y quede clamped a `max_a`.
- En VALLE, `energy = max(0, (target_soc − soc)/100 · cap)` con
  `target_soc = schedule_state.target_soc_pct or max_soc`, y las horas son `8.0 − hour`.
  El reparto lineal **sí es correcto aquí**: la red entrega potencia constante.

**(b) Simulación franja a franja — `_min_current_for_surplus(surplus, energy_kwh, v, cc)`**

Usada solo en SOLAR con forecast disponible.

```
target = energy_kwh · margin

captured(I) = Σ_i  min( I · V · 0.5 / 1000 ,  surplus[i] )      # kWh que se capturarían

para I = 1 … max_a:
    si captured(I) ≥ target → return (I, alcanzable=True)

# ningún I dentro del tope alcanza el objetivo:
peak = max(surplus)
return ( max(1, round(peak / 0.5 · 1000 / V)) , alcanzable=False )
```

El razonamiento, tal como está en el código: la batería carga a
`min(I·V, excedente)` — **la corriente es un techo, no un caudal garantizado**.
Repartir la energía linealmente asume implícitamente que hay `I·V` disponibles durante
todas las horas restantes, y la producción es una campana: a las 9:00 y a las 18:00 no
los hay aunque el total del día sobre. `captured(I)` es monótona creciente, así que el
primer `I` que alcanza el objetivo es el mínimo.

El retorno por **pico de excedente** cuando no se alcanza el objetivo es lo que evita
subir a `max_a` por unas décimas de kWh: por encima del pico previsto, más amperios no
captan ni un vatio más. Ese pico **puede quedar por encima de `max_a`** (excedente
concentrado al mediodía), y entonces el límite real es la corriente; las etiquetas
distinguen los dos casos (`máx — el excedente supera el tope` vs `limitada por sol`).

Coste: 66 × 48 iteraciones por tick.

### 5.6 Orden de aplicación de límites y márgenes

```
1. margin        — multiplica DENTRO del cálculo:
                     · amps_for:                i = E·1000/(V·h) · margin
                     · _min_current_for_surplus: target = E · margin
2. raw           — resultado sin acotar. Es lo que se persiste como `calculated_a`.
3. clamp de config — max(floor, min(max_a, raw)), donde floor es:
                     · floor_a                          en VALLE y SOLAR
                     · balance_floor_a                  en BALANCE
                     · max(1, balance_floor_a // 2)     en BALANCE (fino)
                   Es lo que se persiste como `current_a` y se escribe.
                   Los modos "sin cambios" (IDLE, batería llena, objetivo alcanzado)
                   y la puerta de temperatura NO pasan por este clamp: devuelven
                   `current` o `max_a` tal cual, con calculated == target.
4. clamp duro    — automation.set_charge_current hace max(1, min(66, round(amps)))
                   antes de escribir en la web, pase lo que pase.
```

Cuando el paso 3 recorta el resultado, `calculated_a ≠ current_a` y la web lo marca
con `·lím`.

**Validadores que impiden combinaciones imposibles** (`ChargeCurrentConfig` y `AppConfig`):

- `floor_a ≤ max_a` y `balance_floor_a ≤ max_a`.
- `balance_soc_pct_2 ≥ balance_soc_pct`.
- Con `battery_balance=true`: `balance_soc_pct < charging.max_soc_pct` (si no, el modo
  BALANCE nunca se alcanzaría).

### 5.7 Efecto de subir o bajar cada parámetro

| Parámetro | Subirlo | Bajarlo |
|---|---|---|
| `interval_min` | Menos tráfico y menos sesiones Playwright; el lazo reacciona más tarde a nubes o al arranque del AC. | Lazo más reactivo; más escrituras web, más lecturas MODBUS y (con caché corta) más descargas del datalogger. |
| `floor_a` | Garantiza más margen para terminar la carga; **más calor** en los días fáciles, porque es el valor que se aplica cuando el cálculo pide menos. | Cargas más suaves y frías; riesgo de no llegar al tope si el forecast falla por lo bajo. |
| `max_a` | Permite absorber picos de excedente altos; techo del inversor 66 A. | Corta el pico: si el excedente supera el tope, se pierde captación (aparece la etiqueta `máx — el excedente supera el tope`). |
| `margin` | Pide más corriente de la teóricamente necesaria: más seguridad de llenar, más calor. Rango válido `[1.0, 3.0]`. | Se ajusta al mínimo teórico: más frío, más sensible a que el forecast se quede corto. |
| `hot_threshold_c` | Con `temp_gate_enabled=true`, más días caen en "batería fría" → **más días a `max_a`**. | Menos días a máximo; la rampa suave entra antes. |
| `temp_gate_enabled` | `true`: prioriza capturar picos intermitentes (batería fría → `max_a`), a costa de calor cuando la temperatura aún es baja pero el día es soleado. Cortocircuita toda la simulación de excedente. | `false`: la temperatura no influye nunca; siempre rampa suave. Comportamiento preventivo, alineado con `calor ∝ corriente`. |
| `house_power_window_min` | Estimación más estable, más lenta en reaccionar a un cambio de consumo. Rango `[5, 240]`. | Más reactiva, más ruidosa (un pico puntual puede dominar la mediana). |
| `house_power_cache_min` | Menos tráfico contra el datalogger; el consumo asumido puede representar hasta `cache + window` minutos atrás. Rango `[0, 120]`; `0` = sin caché. | Datos más frescos, mucho más tráfico (cada lectura descarga el día completo). |
| `productive_window_pct` | Ventana histórica más larga (más cola de producción incluida) → menos corriente en el fallback. Rango `[10, 100]`. | Ventana más corta → más corriente y corte antes del modo SOLAR. |
| `productive_window_end_hour` | Ventana más larga en el fallback final → menos corriente, modo SOLAR activo hasta más tarde. Rango `[0, 24]`. | Ventana más corta → más corriente y salida antes a IDLE (que congela el valor). |
| `balance_soc_pct` | El balanceo empieza más tarde (tramo más corto). Debe ser `< max_soc_pct`. | Empieza antes: más tiempo cargando muy suave. |
| `balance_soc_pct_2` | La segunda etapa (mitad de suelo) entra más tarde. | Entra antes: el último tramo se hace aún más suave. |
| `balance_floor_a` | Balanceo más rápido y caliente. | Balanceo más suave; si es demasiado bajo puede no llegar a `max_soc` antes del fin de ventana (el cálculo sube la corriente si hace falta, pero nunca por debajo del suelo). |
| `enabled` | `false` desactiva el controlador salvo en modo simulación. | — |

### 5.8 Persistencia de los cambios

Cada cambio **efectivo** (`target != current`) escribe un punto en el measurement
`corriente_carga`: tag `mode` (primer token de la etiqueta: `VALLE`/`SOLAR`/`IDLE`/
`BALANCE`) y campos `current_a`, `calculated_a`, `previous_a`, `delta_a`, `soc_pct`,
`battery_temp_c`, `battery_voltage_v`, `detail` (etiqueta completa), `verified`,
`dry_run`. Timestamp: hora local etiquetada como UTC, al segundo.

Un `StorageError` al persistir se registra como `WARNING` y **no** rompe el tick.

---

## 6. Referencia de `config.yaml`

`config.yaml` está gitignoreado; la plantilla versionada es `config.example.yaml`.
Los rangos que siguen son los que imponen los modelos Pydantic de `config.py`; los
valores por defecto son los del modelo (entre paréntesis, el de la plantilla cuando
difiere).

> **No verificable desde el código:** los valores reales en producción. El
> `config.yaml` presente en el árbol de trabajo es el de desarrollo.

### 6.1 `solcast`

| Clave | Tipo / rango | Defecto | Significado |
|---|---|---|---|
| `api_key` | str, **requerido** | — | Clave de la API. Se envía como **parámetro de URL** (`?api_key=`), no como Bearer. Override: `SOLCAST_API_KEY`. |
| `resource_id` | str, **requerido** | — | ID del emplazamiento. Override: `SOLCAST_RESOURCE_ID`. |
| `base_url` | str | `https://api.solcast.com.au` | Base de la API. |
| `forecast_hours` | int | 48 | Horizonte pedido a la API (`?hours=`). |
| `cache_ttl_hours` | int | 4 | Validez de la caché local `/app/logs/solcast_cache.json`. Superado el TTL se vuelve a llamar a la API. |

### 6.2 `inverter`

| Clave | Tipo / rango | Defecto | Significado |
|---|---|---|---|
| `web_url` | str, **requerido** | — | URL de la web del inversor. Si `modbus_host` está vacío, de aquí se extrae el host MODBUS. Override: `INVERTER_WEB_URL`. |
| `username` / `password` | str, **requerido** | — | Credenciales de la web **y** del datalogger HTTP (Basic Auth). Overrides: `INVERTER_USERNAME`, `INVERTER_PASSWORD`. |
| `modbus_host` | str | `""` | Host MODBUS. Vacío → se deriva de `web_url`. Override: `INVERTER_MODBUS_HOST`. |
| `modbus_port` | int | 502 | Puerto MODBUS TCP. |
| `modbus_slave` | int | 1 | Unit ID. |
| `browser_timeout_seconds` | int | 30 | Timeout por defecto de Playwright (se multiplica por 1000 ms) **y** timeout del cliente MODBUS. |
| `device_id` | str | `""` | Serial del inversor para el datalogger. Sin él, `logger_reader` intenta autodescubrirlo y `GET /api/today_solar` devuelve 503. Override: `INVERTER_DEVICE_ID`. |

### 6.3 `installation`

| Clave | Tipo / rango | Defecto | Significado |
|---|---|---|---|
| `battery_capacity_kwh` | float, **requerido** | — | Capacidad nominal. Convierte SOC ↔ kWh en todo el algoritmo. Override: `BATTERY_CAPACITY_KWH`. |
| `average_daily_consumption_kwh` | float, **requerido** | — | Consumo diario de 24 h. **Solo fallback** desde v1.75: lo calibra `get_avg_daily_consumption`. Override: `DAILY_CONSUMPTION_KWH`. |
| `peak_power_kwp` | float | 0.0 | Potencia pico instalada. Informativo: no lo usa ningún cálculo. |

### 6.4 `charging`

| Clave | Tipo / rango | Defecto | Significado |
|---|---|---|---|
| `risk_factor` | float `[0,1]` | 0.7 | Peso de p10 frente a p50 en `solar_effective`. 1 = totalmente pesimista. Fallback del dinámico. Override: `RISK_FACTOR`. |
| `min_soc_pct` | float `[0,100]` | 15.0 | SOC mínimo. **Solo fallback**: si el inversor devuelve un valor > 0 en el holding 40126, gana el del inversor. Override: `MIN_SOC_PCT`. |
| `max_soc_pct` | float `[0,100]`, **> `min_soc_pct`** | 95.0 | Tope de carga. Override: `MAX_SOC_PCT`. |
| `safety_margin_kwh` | float `≥ 0` | 1.0 | Colchón añadido al consumo diurno antes de restar la solar. Override: `SAFETY_MARGIN_KWH`. |
| `night_consumption_kwh` | float `≥ 0` | 3.5 | Consumo 00:00–07:59. Fallback del dinámico. Override: `NIGHT_CONSUMPTION_KWH`. |
| `night_consumption_window_days` | int `≥ 7` | 30 | Ventana promediada. |
| `night_consumption_min_days_in_window` | int `≥ 1` | 14 | Días con dato exigidos dentro de la ventana. Alias aceptado: `night_consumption_min_days`. |
| `risk_factor_window_days` | int `≥ 7` | 30 | Ventana del risk factor dinámico. |
| `risk_factor_min_days_in_window` | int `≥ 1` | 14 | Mínimo de pares forecast/real. Alias: `risk_factor_min_days`. |
| `solar_bias_factor` | float `[0.5, 1.5]` | 1.0 | Multiplicador de calibración del forecast. Fallback del dinámico. Override: `SOLAR_BIAS_FACTOR`. |
| `solar_bias_window_days` | int `≥ 7` | 30 | Ventana del bias. **También** es la ventana que usa `_productive_window_end` (nivel 2 de la cascada). |
| `solar_bias_min_days_in_window` | int `≥ 1` | 14 | Mínimo de pares. Alias: `solar_bias_min_days`. |
| `daily_consumption_window_days` | int `≥ 7` | 30 | Ventana del consumo diario dinámico. |
| `daily_consumption_min_days_in_window` | int `≥ 1` | 14 | Mínimo de días. Sin alias antiguo. |

**Validador cruzado `windows_must_cover_min_days`:** para los cuatro pares,
`window_days ≥ min_days_in_window`. Con la ventana más corta que el mínimo, el contador
de días válidos nunca podría alcanzarlo y el parámetro dinámico quedaría clavado en el
fallback **en silencio**; por eso se rechaza el arranque (y el `POST /api/config`).

**Claves obsoletas:** las tres con alias siguen funcionando, pero
`find_deprecated_config_keys()` las detecta → `WARNING` por cada una al arrancar,
badge rojo en la web, y `POST /api/config` escribe el nombre canónico y **elimina** el
obsoleto. Migración por CLI: `make migrate-config`.

### 6.5 `tariff`

| Clave | Tipo / rango | Defecto | Significado |
|---|---|---|---|
| `schedule_at` | `"HH:MM"` | `"23:30"` (plantilla: `23:55`) | Hora del ciclo nocturno canónico. Formato inválido no aborta el arranque (v1.82): se registra `ERROR` + email de aviso (mismo mecanismo que el email del ciclo) y el job simplemente no se programa; el resto del scheduler (recheck/backfill/corriente/backup) sigue operativo. |
| `schedule_recheck_at` | `"HH:MM"`, lista o CSV | `[]` (plantilla: `"19:00, 03:00"`) | Horas de re-evaluación. Se normaliza a lista ordenada sin duplicados. Formato inválido → **el arranque falla**. |
| `weekend_days` | lista de int `0..6` | `[5, 6]` | 0 = lunes. Días con tarifa valle 24 h. |
| `holidays` | lista `"YYYY-MM-DD"` | `[]` | Festivos: valle 24 h. Las fechas parseadas por YAML como `date` se convierten a string. |
| `periods.valley/flat/peak` | dict de intervalos `{start, end}` | — (requerido) | Definición de tramos tarifarios. **El algoritmo no los lee**: el valle está codificado como 00:00–08:00 en las constantes de `automation.py` y `main.py`. Son documentación viva más `TariffConfig.get_valley_intervals`, que ningún módulo llama. |
| `night_cutoff_hour` | int `[0,12]` | 8 | Antes de esta hora, la ejecución cuenta como "la noche anterior". Debe ser ≤ hora de fin del valle. |

### 6.6 `charge_current`

Ver sección 5 para el detalle funcional.

| Clave | Tipo / rango | Defecto | Plantilla |
|---|---|---|---|
| `enabled` | bool | `true` | `true` |
| `interval_min` | int `≥ 1` | 15 | 15 |
| `temp_gate_enabled` | bool | `true` | `true` |
| `hot_threshold_c` | float | 30.0 | 30.0 |
| `house_power_window_min` | int `[5, 240]` | 60 | 60 |
| `house_power_cache_min` | int `[0, 120]` | 15 | 15 |
| `house_profile_window_days` | int `≥ 7`, `≥ house_profile_min_days_in_window` | 30 | 30 |
| `house_profile_min_days_in_window` | int `≥ 1` | 14 | 14 |
| `productive_window_pct` | int `[10, 100]` | 90 | 90 |
| `productive_window_end_hour` | float `[0, 24]` | 17.0 | 17.0 |
| `floor_a` | int `[1, 66]`, `≤ max_a` | 22 | 22 |
| `max_a` | int `[1, 66]` | 66 | 66 |
| `margin` | float `[1.0, 3.0]` | 1.33 | 1.33 |
| `battery_balance` | bool | `false` | `false` |
| `balance_soc_pct` | float `[0,100]`, `< charging.max_soc_pct` si `battery_balance` | 98.0 | 97 |
| `balance_soc_pct_2` | float `[0,100]`, `≥ balance_soc_pct` | 99.0 | 99 |
| `balance_floor_a` | int `[1, 66]`, `≤ max_a` | 10 | 12 |

> Editable desde la pestaña Configuración (grupo «Corriente máxima de carga»)
> **desde v1.83**; hasta entonces la sección entera se editaba a mano en el YAML.
> Como todo el formulario, los cambios requieren reiniciar el contenedor.
>
> Los defectos de `floor_a` y `margin` se alinearon con la plantilla en v1.83: el
> modelo se había quedado en los valores previos a la decisión del 2026-08-18
> (`margin` 1.2 → 1.33), así que una instalación sin esas claves en su YAML corría
> con un margen que la documentación daba por descartado.

### 6.7 `system`

| Clave | Tipo / rango | Defecto | Significado |
|---|---|---|---|
| `log_level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` | Se normaliza a mayúsculas; otro valor es error de validación. Override: `LOG_LEVEL`. |
| `log_file` | str | `/app/logs/solar-manager.log` | El directorio se crea si no existe. `GET /api/logs` y el backup SCP leen de aquí. |
| `dry_run` | bool | `false` | Ver sección 8.4. Override: `DRY_RUN`. |
| `timezone` | str (IANA) | `Europe/Madrid` | Zona del scheduler y de todos los cálculos horarios. |
| `web_port` | int | 8080 | Puerto de uvicorn (dentro del contenedor). |
| `web_enabled` | bool | `true` | `false` no arranca la interfaz web. |
| `web_api_key` | str | `""` | Protege los endpoints de escritura. **Vacío = sin autenticación.** Override: `WEB_API_KEY`. |
| `email.enabled` | bool | `true` | Activa el notificador. |
| `email.smtp_host` | str | `""` | Usar **hostname**, no IP, para que la verificación TLS funcione. Override: `SMTP_HOST`. |
| `email.smtp_port` | int | 587 | 587 = STARTTLS, 465 = SSL directo. Override: `SMTP_PORT`. |
| `email.smtp_user` / `smtp_password` | str | `""` | Si ambos están vacíos, no se hace `login()`. Overrides: `SMTP_USER`, `SMTP_PASSWORD`. |
| `email.mail_from` / `mail_to` | str | `""` | Sin `smtp_host`, `mail_from` o `mail_to` no se envía (WARNING). Overrides: `MAIL_FROM`, `MAIL_TO`. |
| `email.use_tls` | bool | `true` | STARTTLS. |
| `email.use_ssl` | bool | `false` | SSL directo; tiene prioridad sobre `use_tls`. |
| `email.verify_ssl` | bool | `true` | `false` desactiva la verificación de certificado (relay interno con cert autofirmado). |

### 6.8 `influxdb`

| Clave | Tipo | Defecto | Significado |
|---|---|---|---|
| `enabled` | bool | `false` (plantilla: `true`) | `false` hace que **todas** las funciones de `storage` sean no-op / devuelvan `None`, con lo que los cuatro parámetros dinámicos caen al fallback. |
| `url` | str | `http://localhost:8086` (plantilla: `http://influxdb:8086`) | Override: `INFLUXDB_URL`. |
| `org` | str | `solar` | Override: `INFLUXDB_ORG`. |
| `bucket` | str | `solar-manager` | Override: `INFLUXDB_BUCKET`. |
| `token` | str | `""` | **Solo por `.env`** (`INFLUXDB_TOKEN`); no editable desde la web. |

### 6.9 `backup` (SCP — inactivo por defecto)

| Clave | Tipo / rango | Defecto | Significado |
|---|---|---|---|
| `enabled` | bool | `false` | Con `true`, `host`, `user` y `remote_dir` pasan a ser obligatorios (validador `required_when_enabled`). |
| `schedule_at` | `"HH:MM"` | `04:00` | Hora del job diario. Un formato inválido registra `ERROR` + email de aviso (v1.82, mismo mecanismo que `tariff.schedule_at`) y desactiva el job (no aborta el arranque, a diferencia de `schedule_recheck_at`). |
| `host` / `port` / `user` / `remote_dir` | str / int `[1,65535]` / str / str | `""` / 22 / `""` / `""` | Destino SCP. |
| `ssh_key_path` | str | `/root/.ssh/id_backup` | Clave privada montada en el contenedor. `scp -i … -o BatchMode=yes` (nunca prompt interactivo). |
| `strict_host_key_checking` | bool | `true` | `false` desactiva la comprobación del host (inseguro; solo primer arranque). |
| `known_hosts_path` | str | `/root/.ssh/known_hosts` | — |
| `retention` | int `≥ 1` | 7 | Backups a mantener en el remoto. |
| `timeout_seconds` | int `≥ 30` | 600 | Timeout de `scp`/`ssh`. |

### 6.10 Jerarquía de configuración

`variables de entorno` > `config.yaml` > `defaults de Pydantic`. La tabla completa de
overrides está en `_apply_env_overrides` (`config.py`); el email va anidado bajo
`system.email`. `GET /api/config` devuelve además la lista `env_overrides` con los
campos que están siendo sobrescritos por el entorno, para que la web los marque.

La ruta del fichero se resuelve así: argumento explícito → `CONFIG_PATH` →
`/app/config.yaml` → `config.yaml` → `FileNotFoundError`.

---

## 7. Integración con el inversor

Tres canales, cada uno con su razón de ser.

### 7.1 MODBUS TCP — solo lectura (`inverter.py`)

Puerto 502, función 0x04 (input registers) y 0x03 (holding registers).
**Direccionamiento base-0**: registro `30016` → dirección `15`.

Una sola llamada lee `address=0, count=28` (registros 30001–30028) y se indexan:

| Registro | Índice | Campo | Escala / convención |
|---|---|---|---|
| 30016 | `regs[15]` | Inverter Status | código → tabla `INVERTER_STATUS` (0..10) |
| 30018 | `regs[17]` | Battery Voltage | `/10` → V |
| 30020 | `regs[19]` | Battery Power | INT16; **positivo = descargando, negativo = cargando** (convención Ingeteam) |
| 30021 | `regs[20]` | Battery SOC | % |
| 30022 | `regs[21]` | Battery SOH | % |
| 30027 | `regs[26]` | Battery Status | código → tabla `BATTERY_STATUS` (0..10) |
| 30028 | `regs[27]` | Battery Temperature | INT16, `/10` → ºC |

Más dos holding registers, leídos por separado y tolerantes a fallo:

| Registro | Dirección | Campo | Si falla |
|---|---|---|---|
| 40126 | 125 | SOC mínimo configurado en el inversor | `min_soc = 0.0` → el algoritmo usa el de config |
| 40087 | 86 | **Corriente máxima de carga** (A, 1–66) | `charge_current_max_a = 0.0` → el controlador considera el tope ilegible y **omite el tick** |

Todo el acceso MODBUS está serializado por `_MODBUS_LOCK` (decorador
`@_serialize_modbus`). Motivo documentado: el inversor admite muy pocas conexiones
simultáneas y, si el read-back del controlador y el poll del dashboard leen a la vez,
una conexión recibe el frame de la otra y se lee un snapshot caducado.

### 7.2 HTTP datalogger — solo lectura (`logger_reader.py`)

`GET http://{host}/inverter/log/{device_id}/{YYYY-MM-DD}` con Basic Auth. Devuelve el
día completo minuto a minuto (hasta 1440 registros). No admite rangos: **siempre se
descarga el día entero**.

Campos usados de cada registro: `Pdc1`, `Pdc2` (producción DC), `Pac` (salida AC del
inversor hacia vivienda/red), `PacMeter` (contador de red: + importando, − exportando),
`EPvToGrid` (contador acumulado), `Pbatt` (potencia batería, − cargando) y `Sbatt` (SOC).
`PacGrid` se lee pero no se usa en ningún cálculo — ver [8.1](#81-el-consumo-de-la-vivienda-no-es-pacgrid--pacmeter).

De aquí salen `DailyStats` (acumulados diarios + 4 perfiles de 48 franjas) y la lectura
en tiempo real del consumo de la vivienda.

### 7.3 Playwright / Chromium — escritura (`automation.py`)

Es el único canal de **escritura**, porque —según el propio código— la configuración de
programación horaria (6.3.1/6.3.2) **no está expuesta por MODBUS**, ni en input
registers ni en los holding documentados; y el registro 40087, aunque se lee por
MODBUS, se escribe por web.

| Operación | Sección de la web | Función |
|---|---|---|
| Escribir programación de carga | **6.3.1** Programación Horaria: Carga de Batería desde Red | `set_charge_schedule` |
| Escribir programación de descarga | **6.3.2** Programación Horaria: Descarga de Batería | `set_discharge_schedule` (+ read-back) |
| Escribir corriente máxima de carga | **1.2** Parámetros Batería con BMS, campo "Corriente Máxima de Carga (A)" | `set_charge_current` |
| Leer ambas programaciones | 6.3.1 + 6.3.2 en una sola sesión | `read_inverter_schedule` |
| Leer versión de firmware | menú **Actualización**, fila "Firmware" | `read_firmware_version` |

**Mecánica obligatoria** (aprendida a base de fallos, según los comentarios):

- Secuencia de navegación: login directo a `/#/login` → clic en "Configuración" →
  clic **por JavaScript** en el botón "Ajustes" dentro de `.inv-sett-top-cont` →
  clic en el texto `6.3.1` / `6.3.2` / "Parámetros Batería con BMS".
- Pulsar **"Leer"** (`button.btn-success`) antes de modificar: es lo que habilita
  "Escribir". Los botones se localizan por clase, no por texto, para ser independientes
  del idioma.
- Reactividad de Vue: para los inputs, `click(count=3)` + `Ctrl+A` + `Backspace` +
  `type()` + `dispatch_event("input")` + `dispatch_event("change")`. Para los selects,
  `select_option()` + `dispatch_event("change")`.
- "Escribir" (`button.btn-warning`) se pulsa **por JavaScript** para superar el atributo
  `disabled` que Vue mantiene aunque su estado interno ya esté actualizado.
- Flags de Chromium obligatorios sin entorno gráfico: `--no-sandbox`,
  `--disable-setuid-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`,
  `--no-first-run`, `--no-zygote`, `--single-process`.
- Todas las funciones web están serializadas por `_WEB_LOCK` (`@_serialize_web`): el
  inversor admite **una sola sesión web a la vez**, así que el controlador de corriente
  y el ciclo nocturno no pueden colisionar.
- Se guardan capturas de pantalla en `/app/logs/` (`screenshot_after_login.png`,
  `screenshot_after_write.png`, `screenshot_login_error.png`).

**Perfiles de etiquetas por firmware (`LabelProfile` / `FIRMWARE_PROFILES`):**
la única etiqueta que cambia entre firmwares es la fila del SELECT "Programación
Horaria N". Hay dos perfiles: `modo` (`"Programación Horaria N: Modo"`, firmware
`ABH1007AD`, verificado en vivo según el comentario) y `legacy`
(`"...: Carga de baterías desde la Red"` / `"...: Descarga de baterías"`, histórico sin
verificar). Las etiquetas de `SOC Grid` y de `Hora/Minuto On/Off` son estables.

Al arrancar, un hilo daemon (`_configure_firmware_profile`) lee el firmware y llama a
`configure_active_profile`, que fija `_ACTIVE_PROFILE`. Firmware desconocido → perfil más
reciente (`DEFAULT_PROFILE`) + `WARNING` pidiendo añadirlo a `FIRMWARE_PROFILES`.
Firmware no leído (`None`) → mismo perfil, solo `debug`.

**Fallo de lectura = excepción, nunca valor por defecto.**
`_get_select_value_by_label` lanza `AutomationError` si no encuentra la etiqueta o el
valor está vacío. El comentario documenta por qué: devolver `"0"` (Desactivado) ante un
fallo daba un estado falso pero plausible que el flujo de escritura condicionada
interpretaba como "ya correcto", omitiendo la reconfiguración. La excepción sube hasta
`read_inverter_schedule`, que devuelve `None`, y `_needs_update` fuerza la reescritura.

---

## 8. Gotchas técnicos

### 8.1 El consumo de la vivienda NO es `PacGrid + PacMeter`

El más importante del proyecto — y el que más ha costado fijar bien. `PacGrid` y
`PacMeter` son dos medidas redundantes del **mismo** flujo de red, con signos
opuestos: `PacGrid + PacMeter ≈ 0` casi siempre, sea cual sea el consumo real. La
fórmula que se dio por verificada el 2026-08-17 (`casa = PacGrid + PacMeter`) resultó
estar mal fundamentada — coincidencia en el ejemplo original, no relación real — y se
corrigió en v1.80 (2026-08-22). El consumo real es:

```
casa = max(0, Pac + PacMeter)        # PacMeter: + importando, − exportando
```

Ejemplos verificados contra el datalogger real (2026-08-22, docstring de `house_power_w`):

- Sin flujo de red: `Pac` 332.9 W, `PacMeter` 1.2 W → casa **334 W** (≈ 337 W del
  monitor del inversor; con la fórmula vieja salía **0 W** — así se detectó el fallo).
- Mediodía exportando: `Pac` 4011.7 W, `PacMeter` −3744.9 W → casa **267 W**.
- Madrugada, batería cubriendo la casa: `Pac` 347.4 W, `PacMeter` −0.5 W → casa
  **347 W** (≈ los 366.8 W que entregaba la batería esa hora).

Siempre hay que usar el helper `logger_reader.house_power_w(record)`. Lo usan
`_calculate_stats` (diario, nocturno y perfil de 30 min), `get_recent_house_power`
(controlador de corriente) y `GET /api/today_solar` (tile del dashboard). El bug
afectaba a todos estos consumidores desde que existen (`consumption_kwh`/
`night_consumption_kwh` desde v1.25, `house_kwh` desde v1.73): en cualquier momento
sin flujo de red significativo — la mayoría de las noches con batería sana, y buena
parte del día — el consumo se contabilizaba como ≈0, sesgando a la baja
`get_avg_night_consumption`/`get_avg_daily_consumption` y por tanto `decide_charge`.
Histórico recalculado el 2026-08-22 sobre los ~59 días disponibles en el datalogger
(`scripts/recompute_daily_stats.py`, reescritura idempotente de `stats_diarias` y
`solar_media_hora`).

**Pendiente en el código:** `grid_exported_kwh` diario se calcula del contador
`EPvToGrid` (`epv_end − epv_start`), no de `∫PacMeter`. El comentario de
`_calculate_stats` advierte de que los campos de media hora sí usan `∫PacMeter`, que es
lo coherente con el balance energético. Las dos cifras no cuadran entre sí.

### 8.2 Timestamps: "hora local etiquetada como UTC"

Decisión consciente y global: `stats_diarias`, `solar_media_hora` y `corriente_carga`
guardan la hora **local española** con designación UTC (`.replace(tzinfo=utc)`), para
que `t.hour` sea directamente la hora local al consultar.

La excepción es `ciclo_carga`, que usa `datetime.now(timezone.utc)` **real**. De ahí que
el JOIN entre ambos sea `forecast_date = ciclo_UTC.date() + 1 día` y que solo funcione
para ejecuciones alrededor de las 23:55 (o hasta ~01:00 UTC).

### 8.3 El datalogger es una ventana rodante de ~59 días

Lo que no se copie a InfluxDB antes de que un día salga de esa ventana **se pierde para
siempre**. Por eso `MAX_BACKFILL_DAYS = 59` y por eso el backfill mira campo por campo:
un campo nuevo que no se rellene a tiempo deja un hueco permanente.

### 8.4 Qué hace y qué no hace `dry_run`

`system.dry_run: true` **no** es un "no hacer nada":

- **No** llama a la API de Solcast: usa una previsión **ficticia** `10/20/30` y `8/18/28`.
- **Sí** lee MODBUS y el datalogger.
- **Sí** recorre todo el flujo de escritura Playwright (navega, rellena los campos), pero
  no pulsa "Escribir".
- **Sí** escribe en InfluxDB: `ciclo_carga` (con la previsión ficticia),
  `stats_diarias`, `solar_media_hora` y `corriente_carga` (con `dry_run=True`).
- **No** actualiza `inverter_schedule_state.json` (solo se guarda tras una escritura real).

Ver la incoherencia **I-1**, que es consecuencia directa de esto.

### 8.5 Solcast: la clave va en la URL

`api_key` se envía como **parámetro de URL** (`params={"api_key": ...}`), no como
cabecera Bearer. Los errores HTTP se traducen a mensajes específicos para 401, 404 y 429.

### 8.6 SMTP

Usar **hostname** y no IP para que la verificación TLS funcione; `verify_ssl: false`
para un relay interno con certificado autofirmado. El hostname del servidor aparece en
el asunto (`[solar-manager@host]`), en la cabecera HTML y en el pie; se obtiene de
`HOST_HOSTNAME` (inyectada por el Makefile) o de `socket.gethostname()`.

El notificador es un **handler de logging**: captura todo lo que se registre entre
`attach()` y `send()`, incluidos los mensajes de otros hilos. `discard()` lo desconecta
sin enviar (lo usa `run_recheck` cuando no hay cambios).

### 8.7 Docker

- `shm_size: '256mb'` en el servicio `solar-manager` es necesario para Chromium.
- El Dockerfile descarga la CLI `influx` 2.7.5 según arquitectura (amd64/arm64) para
  `GET /api/db/export`, e instala `openssh-client` para el backup SCP.
- El volumen de `config.yaml` está montado **sin `:ro`** a propósito: la pestaña
  Configuración de la web escribe ese fichero.
- **Arrancar con `make up`**, no con `docker compose up -d`: el Makefile inyecta
  `HOST_HOSTNAME=$(hostname)`, que el compose espera (`$HOSTNAME` no está exportada en
  macOS; `$(hostname)` funciona en ambos sistemas).
- InfluxDB usa bind mounts `./influxdb/data` y `./influxdb/config`. El contenedor corre
  como uid 1000: cambiar el propietario a otro usuario impide el arranque.

### 8.8 El fichero `inverter_schedule_state.json`

`/app/logs/inverter_schedule_state.json` guarda `{charge_needed, target_soc_pct,
discharge_blocked}` tras cada escritura real. **No** condiciona las escrituras del ciclo
nocturno (eso lo decide la lectura en vivo, sección 3.6), pero **sí** es la única fuente
del modo VALLE del controlador de corriente. Si está ausente o desfasado, el controlador
no entra en VALLE (`IDLE (valle sin carga)`) o usa un `target_soc` viejo.

### 8.9 Peak-Shaving por encima del bloqueo de descarga

Documentado como suposición asumida, no modelada: con Peak-Shaving activo (firmware
`ABH1007AB` y posteriores), el inversor usa la batería para cubrir los picos por encima
de la potencia contratada **incluso fuera del horario de descarga permitido**, y desde
`ABH1007AD` también dentro de la ventana de carga desde red. Es decir, `discharge_blocked=True`
**no garantiza** batería 100 % preservada. El efecto está acotado (solo el exceso sobre la
potencia contratada, y solo cuando salta el pico) y se considera absorbido por
`safety_margin_kwh`.

### 8.10 Sección 6.3.3 (carga desde FV)

Desde `ABH1007AC` la web separa la programación en tres secciones hermanas: 6.3.1
(carga desde red), 6.3.2 (descarga) y **6.3.3 (carga desde FV, nueva)**. El proyecto solo
toca 6.3.1 y 6.3.2. Como la navegación es por texto de etiqueta ("6.3.1"/"6.3.2") y no
por índice, la sección extra no descoloca la navegación.

### 8.11 `night_consumption_kwh` en registros antiguos

Ese campo de `stats_diarias` se añadió en v1.25; los registros anteriores no lo tienen,
así que el contador de días válidos arranca de cero aunque haya meses de histórico. El
risk factor usa `solar_kwh`, que es un campo original y acumula datos antes.

---

## 9. Infraestructura

### 9.1 Proceso y arranque (`main.main()`)

```
1. load_config()            → error de config = sys.exit(1)
2. setup_logging()          → StreamHandler(stdout) + FileHandler(log_file)
3. log de arranque:  "solar-manager v1.79 arrancando en <host> (dry_run=…)"
4. WARNING por cada clave de config obsoleta encontrada
5. si system.web_enabled → hilo daemon con uvicorn en 0.0.0.0:web_port
6. hilo daemon "firmware-profile-startup"  → lee firmware y fija el perfil de etiquetas
7. hilo daemon "backfill-startup"          → backfill_solar_history()
8. start_scheduler(cfg)     → BlockingScheduler; el proceso vive aquí
```

`CMD ["python", "-m", "app.main"]`.

### 9.2 Jobs del scheduler (`scheduler.start_scheduler`)

| id | Trigger | Gracia | Notas |
|---|---|---|---|
| `charge_schedule` | Cron a `tariff.schedule_at` | 300 s | Ciclo nocturno completo. |
| `charge_recheck_N` | Cron, uno por cada `schedule_recheck_at` | 300 s | Solo si la lista no está vacía. |
| `solar_backfill` | Cron 00:30 | 3600 s | Fijo en el código, no configurable. |
| `charge_current` | Interval `interval_min` | 120 s | `max_instances=1`, `coalesce=True`, y **`next_run_time` ≈ 20 s tras el arranque**, porque `IntervalTrigger` no dispara al inicio: sin eso, un `make restart` dejaría el inversor con el tope anterior hasta `interval_min` minutos. |
| `external_backup` | Cron a `backup.schedule_at` | 3600 s | Solo si `backup.enabled`. |

Además, `RUN_ON_START=true` en el entorno ejecuta un ciclo completo inmediatamente al
arrancar. Se manejan SIGTERM y SIGINT para un cierre limpio.

Cada wrapper de job captura `Exception` y la registra con `logger.exception`, de modo
que un fallo no mata el scheduler.

### 9.3 Docker Compose

Dos servicios:

- **`solar-manager`**: construido del `Dockerfile` (python:3.12-slim + Chromium +
  CLI `influx` + `openssh-client`). Monta `./config.yaml` (rw) y `./logs/`.
  `env_file: .env`, `TZ=Europe/Madrid`, `HOST_HOSTNAME=${HOST_HOSTNAME}`,
  `shm_size: 256mb`, puerto `${WEB_PORT:-8080}`. Depende de `influxdb` con
  `condition: service_healthy`. Su propio healthcheck es un no-op
  (`python -c "import sys; sys.exit(0)"`): solo comprueba que el intérprete arranca.
- **`influxdb`**: imagen `influxdb:2.7`, bind mounts `./influxdb/{data,config}`,
  inicialización por `DOCKER_INFLUXDB_INIT_*`, puerto 8086, healthcheck `influx ping`.

En `volumes:` quedan declarados `solar-manager-logs`, `influxdb-data` e
`influxdb-config`, que **ningún servicio usa** (los montajes son bind mounts). Residuo.

### 9.4 InfluxDB — modelo de datos

| Measurement | Tags | Timestamp | Escrito por | Campos |
|---|---|---|---|---|
| `ciclo_carga` | — | `now()` UTC real (~22–23 h UTC) | `run` únicamente | estado del inversor, forecast p10/p50/p90 crudo, `solar_effective_kwh`, `energy_stored/at_dawn/deficit`, `charge_needed`, `target_soc_pct`, `target_kwh`, `valley_day_skip`, `solcast_error`, `automation_ok`, `dry_run` |
| `stats_diarias` | `device_id` | medianoche UTC del día de datos | `run` (ayer) y backfill | `solar_kwh`, `grid_consumed_kwh`, `grid_exported_kwh`, `consumption_kwh`, `night_consumption_kwh`, `soc_start_pct`, `soc_end_pct`, `peak_soc_pct` (v1.78), `battery_charged_kwh` (v1.78), `records` |
| `solar_media_hora` | — | hora local etiquetada UTC, cada 30 min | `run` (forecast + real) y backfill (real) | `real_kwh`, `house_kwh`, `grid_import_kwh`, `grid_export_kwh`, `forecast_p50_kwh`, `forecast_p10_kwh`, `forecast_p90_kwh` |
| `corriente_carga` | `mode` | hora local etiquetada UTC, al segundo | controlador de corriente, solo si cambia | `current_a`, `calculated_a`, `previous_a`, `delta_a`, `soc_pct`, `battery_temp_c`, `battery_voltage_v`, `detail`, `verified`, `dry_run` |

Real y forecast comparten `solar_media_hora` a propósito: mismo timestamp de franja, así
que el cruce más útil (`real_kwh − house_kwh`, el excedente disponible para la batería)
sale de una sola consulta sin JOIN en Python. El precio asumido es que el nombre del
measurement se queda corto.

### 9.5 Interfaz web (FastAPI)

`docs_url` y `redoc_url` están desactivados. Los endpoints de escritura dependen de
`_require_api_key`, que compara la cabecera `X-API-Key` con `system.web_api_key`
— **si la clave configurada está vacía, no hay autenticación de ningún tipo**.

| Endpoint | Auth | Descripción |
|---|---|---|
| `GET /` | — | Dashboard HTML; sustituye `{{VERSION}}` y `{{HOSTNAME}}`. |
| `GET /api/version` | — | Versión. |
| `GET /api/status` | — | Estado MODBUS (SOC, SOH, potencia, tensión, temperatura, estados, corriente máx.). |
| `GET /api/forecast` | — | Forecast de mañana por hora + total crudo y calibrado. |
| `GET /api/today_solar` | — | Producción, consumo de casa y flujo de red de hoy, del datalogger. Requiere `device_id` (si no, 503). |
| `GET /api/params` | — | Parámetros dinámicos activos, su origen y los días válidos. |
| `GET /api/solar_history` | — | Historial forecast vs real (`view=day|week|month`). |
| `GET /api/charge_current_today` | — | Cambios de corriente registrados hoy. |
| `GET /api/logs` | — | Últimas N líneas del fichero de log. |
| `GET /api/config` | — | Config editable + `env_overrides` + `legacy_keys` + `default_keys` (claves ausentes del YAML, devueltas con el default del modelo). No expone secretos. |
| `POST /api/config` | ✔ | Aplica cambios con `ruamel.yaml` (round-trip: preserva comentarios y orden), valida el YAML completo contra `AppConfig` antes de escribir; si falla, 400 sin tocar el fichero. |
| `POST /api/cycle` | ✔ | Lanza `python -m app.run_cycle` como subproceso (`--write` si no es dry_run) y devuelve un `job_id`. |
| `POST /api/run/{test}` | ✔ | Lanza uno de los 11 módulos permitidos (`app.test_*`, `app.diag_*`, `app.run_cycle`, `app.simulate_charge_current`). Las claves de la API son estables aunque el módulo detrás se renombre; `main` pasó a `cycle` en v1.84. |
| `GET /api/stream/{job_id}` | — | SSE con la salida del job. |
| `GET /api/db/export` | ✔ | `influx backup` online del bucket, empaquetado en `.tar.gz`. El token va por env var, no en argv. |
| `POST /api/backup/run` | ✔ | Dispara el backup SCP bajo demanda. |

> `POST /api/cycle` con `dry_run=false` ejecuta el **ciclo completo real**, incluida la
> escritura de `ciclo_carga`. Ver incoherencia **I-6**.

#### 9.5.1 Pestaña Configuración (reescrita en v1.83)

Tres capas describen el mismo conjunto de parámetros y tienen que coincidir:

```
modelo Pydantic (config.py)  ↔  _EDITABLE_FIELDS (server.py)  ↔  CONFIG_SCHEMA (index.html)
```

Nada las ataba, y divergieron: al llegar la v1.82 el formulario no exponía **ningún**
campo de `charge_current` (17 parámetros añadidos entre v1.53 y v1.79) ni de `backup`
(11), ni `tariff.weekend_days`/`holidays` — 31 de 73 campos editables solo existían en
el YAML. `app/test_config_web.py` fija ahora esa correspondencia en las tres
direcciones: un campo nuevo en cualquier modelo hace fallar el test hasta que se expone
en la web o se declara excluido, con su motivo, en `_EXCLUDED_FIELDS` (secretos de
`.env` y `tariff.periods`, la única estructura anidada).

Decisiones de la reescritura:

- **73 campos en 6 grupos colapsables** (decisión de carga · corriente de carga ·
  planificación · integraciones · sistema · backup). La rejilla plana anterior no
  escalaba de 44 a 73 campos.
- **Se envía solo lo modificado.** El formulario guarda un baseline al cargar y
  `_collectDirty()` manda únicamente el diff. Antes se enviaban los 44 campos en cada
  guardado, y como `_cast_value` rechazaba la cadena vacía en los tipos `str`, un YAML
  con `mail_from: ""` (el caso recomendado: el valor real vive en `.env`) hacía que
  cambiar `dry_run` devolviera `400 email.mail_from: valor vacío` **sin escribir nada**.
  Ahora `""` es válido en `str`; los campos donde vaciar rompería el arranque
  (`log_level`, `timezone`, URLs de InfluxDB…) usan el tipo `str_required`.
- **Valor efectivo, no `null`.** `GET /api/config` resuelve el default del modelo para
  las claves ausentes del YAML (`_section_defaults`) y las marca en `default_keys`; la
  web las pinta con un badge `def`. Antes se devolvía `null`, que pintaba un input vacío
  y bloqueaba el guardado — y dejaba invisibles los parámetros que gobiernan el sistema
  sin estar escritos en el fichero.
- **Tipos nuevos**: `time_hhmm` (valida `HH:MM` antes de escribir), `int_list`
  (`weekend_days`, chips L–D, se escribe en flow style para no ocupar tres líneas) y
  `date_list` (`holidays`, chips con alta/baja; las fechas se escriben entrecomilladas
  para que YAML no las relea como `datetime.date`).
- **Las invariantes del modelo se validan también en cliente** (`window_days ≥
  min_days_in_window`, `floor_a ≤ max_a`, `balance_soc_pct < max_soc_pct`, campos
  obligatorios del backup, formato de las horas): señalan el campo concreto en vez de
  devolver un `ValidationError` de Pydantic al pie del formulario. La validación del
  backend sigue siendo la autoritativa.
- **`backup` pasa a ser editable** (no tiene secretos: la autenticación es por clave SSH
  montada) y el botón de disparo manual se mueve junto a su configuración. Antes el
  botón vivía en una pestaña que no dejaba ver ni corregir los ajustes que usa —
  y en producción la sección `backup:` ni siquiera existe en el YAML.

### 9.6 Backup — dos mecanismos, uno activo

> **Nota de procedencia.** Esta sección mezcla dos orígenes distintos y los mantiene
> separados a propósito. **9.6.1** sale de leer el repositorio, como el resto del
> documento. **9.6.2** procede del antiguo `ARCHITECTURE_addendum.md`, redactado durante
> el despliegue real: describe la máquina de producción y la NAS, no el código, e incluye
> configuración local **no versionada en git**. Nada de 9.6.2 es comprobable leyendo el
> repositorio.

#### 9.6.1 Los dos mecanismos, según el código

| | `app/backup.py` (SCP) | `scripts/backup_influxdb.sh` (NFS + systemd) |
|---|---|---|
| **Estado** | **Implementado, inactivo por defecto** (`backup.enabled: false`) | **Activo en producción** (verificado fuera del repo — ver 9.6.2) |
| Disparo | Job APScheduler diario + `POST /api/backup/run` | systemd timer 23:57, `Persistent=true` |
| Contenido | `influx backup` + `logs/` + `config.yaml` en un `.tar.gz` | Backup binario (`influx backup`) + export line protocol comprimido (`influxd inspect export-lp`) |
| Transporte | SCP con clave SSH | NFS efímero a NAS Synology (`mount` al empezar, `umount` en `trap cleanup EXIT`) |
| Rotación | `retention` últimos ficheros, borrados por SSH | GFS: daily 7, weekly 4 (domingo), monthly 11 (último día del mes), yearly 1 (31-dic) |
| Cobertura | Bucket + logs + config | Solo el bucket de datos (`influxd.bolt` / `influxd.sqlite` quedan fuera) |
| Requisitos | Montar la clave SSH (volumen comentado en el compose), clave pública en el destino | Regla `sudoers` NOPASSWD restringida a `mount`/`umount` de esa ruta, no versionada |

> **No verificable desde el código:** cuál de los dos está realmente activo en la máquina
> de producción. Depende de `backup.enabled` en el `config.yaml` de producción (que no
> está en el repo) y del estado del timer de systemd.

`Persistent=true` es la razón declarada para elegir systemd en vez de cron: si el
servidor está apagado a las 23:57, el backup se ejecuta en cuanto arranca.

Detalles del script que el código documenta como aprendidos a golpes:
`influxd inspect export-lp` pertenece al **daemon**, no al CLI cliente, y exige
`--bucket-id` (no el nombre); `--compress` deja el contenido en gzip pero **no** renombra
el fichero (el script añade `.gz` a mano); `INFLUXDB_TOKEN` **no existe** como variable de
entorno dentro del contenedor, así que se lee de `.env` en el host y se pasa con `--token`.

#### 9.6.2 Despliegue real del backup por NFS

> Origen: `ARCHITECTURE_addendum.md`, verificado en producción en agosto de 2026.
> Describe el estado de la máquina y de la NAS, no el contenido del repositorio.

En agosto de 2026 se comprobó que la sección `backup` **nunca se ha configurado** en el
`config.yaml` de producción (`grep -n "backup" config.yaml` → salida vacía), por lo que
`app/backup.py` está implementado pero no se ejecuta. El mecanismo que realmente corre es
el script externo con systemd timer y NFS. Aun así, no conviene darlo por sentado en
futuras revisiones: comprobar `backup.enabled` en el `config.yaml` de producción antes de
asumir cuál está activo.

**Componentes**

```
solar-manager/
└── scripts/
    ├── backup_influxdb.sh              # Script principal de backup
    └── systemd/
        ├── solar-manager-backup.service
        └── solar-manager-backup.timer
```

**Qué hace el script**

1. Lee `INFLUXDB_TOKEN` desde `.env` (nunca se expone en logs).
2. Genera **backup binario** con `docker exec ... influx backup` (formato nativo,
   restauración rápida vía `influx restore`).
3. Genera **export en line protocol comprimido** con
   `docker exec ... influxd inspect export-lp --bucket-id <id> --engine-path /var/lib/influxdb2/engine --compress`
   (texto legible y auditable).
4. Ambos ficheros se generan primero en `/tmp` **dentro del contenedor** y se extraen con
   `docker cp` a un staging local (`backups/tmp/`), sin tocar el bind mount de datos en
   vivo (`/home/scresp0/solar-manager/influxdb/data`).
5. Monta temporalmente por **NFS** el volumen `Intercambio` de la NAS Synology
   (`172.24.0.6:/volume1/Intercambio` → `/mnt/nas-intercambio`), copia los backups a la
   categoría de rotación que toque, y **desmonta siempre** al terminar
   (`trap cleanup EXIT`), tanto en éxito como en fallo.
6. Sin *fallbacks* silenciosos: `set -euo pipefail`; cualquier fallo interrumpe el script
   y queda registrado como `ERROR` en el journal.

**Rotación GFS**

| Categoría | Disparador | Backups conservados |
|---|---|---|
| `daily` | Cada ejecución | 7 |
| `weekly` | Domingo | 4 |
| `monthly` | Último día del mes | 11 |
| `yearly` | Último día de diciembre | 1 (solo año en curso) |

Destino en la NAS:
`/mnt/nas-intercambio/solar-manager-backup/{daily,weekly,monthly,yearly}/<timestamp>/{binary, export.lp.gz}`

**Programación**

- **systemd timer**, no cron, por `Persistent=true`: evita huecos silenciosos si el
  servidor está apagado a las 23:57.
- Horario: `23:57` diario.
- Logs vía `journalctl -u solar-manager-backup.service`.
- Habilitado con `sudo systemctl enable --now solar-manager-backup.timer`.

**Gotchas del despliegue**

- **Bucket ID concreto**: `export-lp` exige `--bucket-id`, no el nombre. Bucket
  `solar-manager` → ID `fa2d32fca4e9f966` (org `solar`).
- **`influxd inspect export-lp` es del daemon**: no aparece en `influx --help`, que solo
  lista subcomandos del CLI cliente. Está en el binario `influxd`, disponible dentro del
  mismo contenedor `influxdb:2.7`.
- **`--compress` no renombra el fichero**: el contenido queda en gzip (magic bytes
  `1f 8b`) pero conserva la extensión de `--output-path`; el `.gz` se añade a mano.
- **El token no vive en el contenedor**: dentro solo existen las variables
  `DOCKER_INFLUXDB_INIT_*`, y `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` queda vacío tras el
  primer arranque. El token real está solo en el `.env` del host y se pasa con `--token`
  en cada `docker exec`.
- **`influxd.bolt` y `influxd.sqlite` quedan fuera**: metadatos de usuarios, orgs, tokens
  y dashboards. Este backup cubre **solo el bucket de datos**. Reconstruir la instancia
  completa desde cero exigiría respaldar también esos ficheros o rehacer el `setup`
  inicial.
- **Regla `sudoers` restringida y no versionada**: el montaje/desmontaje NFS requiere
  `sudo` y el timer corre sin terminal interactiva. Se resolvió con una regla `NOPASSWD`
  acotada a `mount`/`umount` de esa ruta exacta (no `NOPASSWD: ALL`) en
  `/etc/sudoers.d/solar-manager-backup`. Es configuración local del servidor, **no está
  en git**.
- **Squash NFS restrictivo por defecto**: el primer montaje falló con `Permiso denegado`
  porque el mapeo NFS de la carpeta compartida `Intercambio` no daba acceso real al UID
  del cliente. Se corrigió en el DSM del Synology (Panel de control → Carpeta compartida →
  Permisos NFS).

**Verificación y mantenimiento**

- Prueba manual: `sudo systemctl start solar-manager-backup.service` seguido de
  `journalctl -u solar-manager-backup.service -n 100 --no-pager`.
- Próxima ejecución programada: `systemctl list-timers solar-manager-backup.timer`.
- **Pendiente de confirmar**: la lógica de rotación (borrado por encima del límite) aún no
  se ha visto ejercitada en producción con más backups acumulados que el límite de alguna
  categoría.
- **Mejora no implementada**: notificar por email (reutilizando `notifier.py`/SMTP) si el
  timer falla una noche. Hoy el único rastro de un fallo es el journal de systemd.

### 9.7 Batería de pruebas

Hasta v1.83 todo se llamaba `test_*`, mezclando dos cosas que no se parecen: la mitad
necesitaba el inversor o internet y no afirmaba nada. Eso hacía parecer que había once
tests cuando la red de seguridad real eran seis. v1.84 los separa por prefijo:

| Prefijo | Qué es | Un fallo significa |
|---|---|---|
| `test_*` | Determinista, sin dependencias externas | El código está roto |
| `diag_*` | Necesita inversor o internet; imprime, no afirma | El equipo no responde |
| `run_cycle` | Camino de producción (`POST /api/cycle`) | Depende de qué falle |

`make test` ejecuta los ocho deterministas en un solo contenedor (`--no-deps`: ninguno
necesita InfluxDB) y devuelve código de salida agregado. No hay CI.

**Qué está cubierto:**

| Test | Casos | Cubre |
|---|---|---|
| `test_config` | 64 | Carga, precedencia entorno > YAML > default, alias de claves renombradas, tarifas y festivos, `get_modbus_host`, todos los validadores cruzados |
| `test_decision` | 45 | `decide_charge`, `decide_discharge`, `reference_date`, `is_valley_day`, resúmenes |
| `test_charge_current` | 44 | `_compute_target_charge_current` (con y sin ventana), `_min_current_for_surplus`, perfil histórico, guards de v1.79 |
| `test_logger_reader` | 30 | `house_power_w`, `_calculate_stats`, caché de consumo |
| `test_charge_current_scenarios` | 22 | Informe narrado: excedente franja a franja, puerta de temperatura, fallback lineal, valle y BALANCE |
| `test_config_web` | 15 | Modelo ↔ `_EDITABLE_FIELDS` ↔ `CONFIG_SCHEMA`, casteos |
| `test_storage` | 59 | Los cuatro dinámicos, el JOIN `ciclo_carga`↔`stats_diarias`, el perfil de consumo por franja y los puntos que se escriben |
| `test_notifier` | 54 | El email: parseo de las líneas de decisión y de configuración, tarjetas, tabla ANTES/DESPUÉS, HTML y texto plano |

**Cómo se prueban `storage` y `notifier` sin sus dependencias** (v1.86):

- Todas las lecturas de InfluxDB pasan por `storage._query` y todas las escrituras por
  `storage._write_points`. Sustituyéndolos por dobles que devuelven tablas sintéticas, el
  JOIN y los cuatro dinámicos se prueban sin base de datos. `_forecast_real_pairs` abría
  su propio cliente en vez de usar `_query`; se unificó en v1.86 (una conexión más en una
  función que corre una vez al día, a cambio de poder cubrir el JOIN).
- El log que consume `notifier` **no se escribe a mano**: se genera llamando a
  `decision.charge_oneliner`/`discharge_oneliner` y reproduciendo los f-strings de
  `main.py`. Si alguien cambia un prefijo, el test falla. Verificado por mutación:
  cambiar `[CARGA] SÍ` por `[CARGA] SI` —solo la tilde— deja el email sin la tarjeta de
  carga, y el test lo detecta.

**Qué NO está cubierto** — ningún test importa estos módulos:

- **`scheduler.py`**: incluido `_parse_hhmm` y el fail-safe de v1.82.
- **`backup.py`** y la parte pura de `automation.py` (perfiles de etiquetas por firmware),
  que sería testeable sin inversor.
- **`solcast.py`**: la incoherencia **I-5** (el `NameError` del timeout) ilustra el
  patrón — un test unitario trivial la habría cazado, pero `diag_solcast` llama a la API
  real y solo recorre el camino feliz.

**Dos ficheros reescritos en v1.85:**

- `test_charge_current_scenarios` llamaba al controlador **sin `window`**: la firma cambió
  en v1.72 (el escalar `remaining` pasó a ser un `SolarWindow`) y el parámetro se
  descartaba en silencio, así que los doce escenarios recorrían el fallback lineal
  mientras el cuadro anunciaba "solar restante calibrada". Documentaban un modelo que ya
  no se ejecutaba. Ahora son 22 escenarios sobre perfiles de excedente reales —incluida
  la campana que motivó v1.72, donde el total sobra pero las colas no dan `I·V`— y cubren
  **BALANCE**, que estaba activo en producción sin un solo test.
- `test_config` cargaba el `config.yaml` real y afirmaba sobre su contenido
  (`assert len(holidays) > 0`), así que fallaba con `config.example.yaml` sin haber nada
  roto; y su `test_validation` imprimía "ERROR: debería haber fallado" **devolviendo 0**:
  un test que no podía fallar. Ahora monta su propio YAML temporal (64 casos) y solo
  comprueba del config real lo que es una afirmación sobre el código: que el modelo actual
  lo siga aceptando.

Verificado por mutación: romper `get_modbus_host` o la validación de horas de
`schedule_recheck_at` hace fallar `test_config`. Mutar solo `coerce_holidays_to_str` no
se nota, porque `load_config` hace la misma conversión antes de Pydantic — redundancia
defensiva, no un defecto.

---

## 10. Incoherencias y defectos detectados en el código

Ordenados por impacto. Son hallazgos de esta lectura; ninguno se ha corregido aquí.

### I-1 · Un ciclo en `dry_run` escribe forecast ficticio en `ciclo_carga` y contamina la calibración

`_collect_decision_inputs` sustituye la previsión real por `p10=10, p50=20, p90=30` cuando
`system.dry_run` está activo (`main.py`), y `run()` llama a `write_cycle` **sin condicionar
a `dry_run`** (`main.py:387`). El punto queda marcado con el campo `dry_run=True`, pero
`_forecast_real_pairs` (`storage.py`) **no filtra por ese campo**: esos pares ficticios
entran en el cálculo del `risk_factor` dinámico y del `solar_bias_factor` con el mismo
peso que los reales.

*Efecto:* cada ejecución real de un ciclo en modo `dry_run` degrada permanentemente dos
de los cuatro parámetros dinámicos durante toda la ventana de calibración
(`*_window_days`, típicamente 30 días).

### I-2 · El log y el docstring de `set_discharge_schedule` contradicen lo que escribe

`automation.py:524-525` (docstring) y `automation.py:534` (mensaje INFO, que acaba en el
log, en la web y en el email) dicen:

```
BLOQUEAR descarga valle (L-V 00:01–07:59 · S-D 00:01–23:59)
```

Las constantes que realmente se escriben (`automation.py:505-509`) son
**L-V 08:00–23:59** y **S-D 00:00–00:01**. El texto describe el modelo *anterior a v1.51*,
en el que la franja se interpretaba como "horas bloqueadas". El código es correcto según
la semántica verificada; el texto que ve el usuario, no.

### I-3 · Mismo texto obsoleto en `decision.discharge_summary`

`decision.py:338`: `"→ BLOQUEAR descarga mañana (6.3.2: 00:01–07:59)"`. Misma causa que
I-2, en otro módulo. Aparece en el resumen de nivel DEBUG.

### I-4 · El estado JSON no es "solo diagnóstico": gobierna el modo VALLE

`_load_schedule_state()` lee `/app/logs/inverter_schedule_state.json` y su resultado es
la **única** entrada que decide si el controlador de corriente entra en modo VALLE y con
qué `target_soc`. El fichero solo se actualiza tras una escritura **real** (nunca en
`dry_run`).

*Efecto:* con `dry_run` activo, o si el fichero se pierde o queda desfasado, las noches en
que sí hay que cargar el controlador se queda en `IDLE (valle sin carga — sin cambios)`
y no baja ni sube la corriente durante todo el valle.

### I-5 · `NameError` en el manejo de timeout de Solcast

`solcast.py:185-186`:

```python
except requests.exceptions.Timeout:
    raise SolcastError("Timeout al llamar a la API de Solcast (>30s)") from e
```

La cláusula no captura la excepción con `as e`, así que `e` no está ligada. Si la API de
Solcast agota el timeout de 30 s, se lanza un **`NameError`** en vez del `SolcastError`
esperado. Como los llamantes solo capturan `SolcastError`, el `NameError` sube: en
`_collect_decision_inputs` reventaría el ciclo entero (el wrapper del scheduler lo
registra y el ciclo termina sin decidir), y en `_solar_surplus_window` lo mismo.

### I-6 · Un ciclo manual real entre 01:00 y 07:59 UTC rompe el JOIN de calibración

`POST /api/cycle?dry_run=false` ejecuta `run()` completo, que escribe `ciclo_carga` con
timestamp `now()` UTC. El JOIN de calibración asume `forecast_date = timestamp + 1 día`,
válido solo para ejecuciones alrededor de las 23:55. Es exactamente el motivo por el que
`run_recheck` no escribe `ciclo_carga`, pero el ciclo manual sí lo hace y no tiene ninguna
guarda horaria.

### I-7 · `_read_charge_state` lee el SOC solo de la Programación 1

`automation.py`: `active = prog1 != DISABLED or prog2 != DISABLED`, pero el SOC se lee
siempre de `LABEL_SOC_1`. Si el inversor quedara con solo la Programación 2 activa (una
configuración que el sistema nunca escribe, pero que un cambio manual podría dejar),
`charge_soc_pct` reflejaría un valor equivocado y `_needs_update` podría decidir mal.

### I-8 · `get_recent_house_power` asume un registro por minuto

`records[-minutes:]` toma los últimos *N registros*, no los últimos *N minutos*. Es
correcto mientras el datalogger entregue exactamente un registro por minuto; con huecos,
la ventana efectiva es más larga de lo pedido. El parámetro se llama
`house_power_window_min`.

### I-9 · Franjas de producción marginal desaparecen del perfil de excedente

En `_solar_surplus_window`, una franja con `eff ≤ 0.02 kWh` **no se añade** a la lista
`surplus` (en vez de añadirse como 0). Como `_min_current_for_surplus` recorre la lista
sin noción de tiempo, un hueco intermedio de baja producción no se modela como un periodo
sin captación, sino que simplemente no existe. El efecto es pequeño (son franjas de
≤ 40 W medios), pero el modelo no es exactamente "el perfil temporal restante".

### I-10 · Documentación desalineada con el código

- El docstring de `get_two_day_forecast` dice "Usa caché de hasta 12 horas"; el TTL real
  es `solcast.cache_ttl_hours` (4 en la plantilla).
- `CLAUDE.md` afirma que "Battery Status (30022)"; en el código, 30022 es **SOH** y el
  Battery Status es **30027** (`inverter.py`, `regs[26]`).
- `CLAUDE.md` afirma que el caché JSON "ya no condiciona ninguna escritura — solo se
  mantiene para diagnóstico"; ver I-4.
- La cabecera de `storage.py` dice "Guarda tres tipos de puntos" y a continuación
  enumera cuatro.

### I-11 · Huecos funcionales (no defectos, pero conviene saberlos)

- ~~La sección `charge_current` no es editable desde la web~~ — **resuelto en v1.83**:
  `charge_current` y `backup` están en `_EDITABLE_FIELDS` y en `CONFIG_SCHEMA`.
- `GET /api/params` no expone el **consumo diario dinámico** (sí los otros tres), y el
  email tampoco le pone badge dinámico/config.
- `tariff.periods` (valley/flat/peak) se valida y se carga, pero **ningún cálculo lo
  usa**: el valle está codificado como 00:00–08:00 en constantes. `get_valley_intervals`
  no tiene llamantes.
- `app/web/templates/index-v1.html` y los volúmenes nombrados de `docker-compose.yml`
  son residuos sin usar.
- El healthcheck del contenedor `solar-manager` no comprueba nada real: un proceso
  colgado seguiría reportando "healthy".

---

## 11. Cosas no verificables desde el código

- **Valores de producción de `config.yaml` y `.env`**: ambos están gitignoreados.
- **Si el backup SCP está activo**: depende de `backup.enabled` en el config de
  producción. La comprobación de agosto de 2026 recogida en 9.6.2 dice que no lo está,
  pero eso se verificó en la máquina, no en el repositorio.
- **Si el timer de systemd está habilitado** y si la rotación GFS ha llegado a ejercitarse
  con más backups que el límite de alguna categoría.
- **Todo el contenido de 9.6.2**: rutas de la NAS, regla `sudoers`, permisos NFS del DSM y
  bucket ID son estado del servidor y de la NAS. Nada de eso está en git ni es
  reproducible desde el repositorio.
- **La semántica real del inversor** para 6.3.1/6.3.2 y para Peak-Shaving: el código la
  documenta como verificada con el usuario y con las notas de firmware, pero no es
  comprobable leyendo el repositorio.
- **El perfil de etiquetas `legacy`**: el propio comentario dice que es histórico y que no
  se ha podido verificar contra un inversor con firmware antiguo.
- **Que 40087 sea un tope duro**: el código lo trata como setpoint y lo verifica por
  read-back, pero no hay nada en el repositorio que demuestre que el inversor lo respeta
  en todas las circunstancias.
- **Rendimiento y consumo reales** (tiempos de backfill, tamaño de las descargas del
  datalogger, medidas de captación): las cifras que aparecen en los comentarios provienen
  de mediciones del autor, no de nada que el código calcule.
