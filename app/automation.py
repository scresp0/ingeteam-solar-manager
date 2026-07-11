"""
automation.py — automatización Playwright para configurar y leer la programación
horaria de carga/descarga de baterías en la interfaz web del inversor Ingeteam.

Ruta de navegación:
  Login → Configuración → Ajustes avanzados →
  6.3.1- Programación Horaria: Carga de Batería desde Red → Leer/Escribir
  6.3.2- Programación Horaria: Descarga de Batería → Leer/Escribir

Campos en la tabla — 6.3.1 (textos exactos de las etiquetas):
  "Programación Horaria 1: Modo"  → select   (antes "...: Carga de baterías desde la Red")
  "SOC Grid 1: Carga máxima para mantener las baterías desde la Red" → input
  "Hora On"    (aparece 2 veces, una por cada programación) → input
  "Minuto On"  (ídem) → input
  "Hora Off"   (ídem) → input
  "Minuto Off" (ídem) → input
  "Programación Horaria 2: Modo"  → select
  "SOC Grid 2: Carga máxima para mantener las baterías desde la Red" → input

Campos en la tabla — 6.3.2 (textos exactos de las etiquetas):
  "Programación Horaria 1: Modo"  → select   (antes "...: Descarga de baterías")
  "Hora On"    (aparece 2 veces, una por cada programación) → input
  "Minuto On"  (ídem) → input
  "Hora Off"   (ídem) → input
  "Minuto Off" (ídem) → input
  "Programación Horaria 2: Modo"  → select

Opciones del desplegable de tipo:
  0 = Desactivado
  1 = Toda la semana
  2 = Entre semana (L-V)
  3 = Fin de semana (S-D)

Diseño de bloqueo de descarga (6.3.2) — la franja Hora On→Off es cuando la
descarga está PERMITIDA, no bloqueada (ver constantes DISC_*):
  discharge_blocked=False → Prog 1 = Desactivado, Prog 2 = Desactivado (libre)
  discharge_blocked=True  → Prog 1 = Entre semana (L-V), 08:00–23:59 (bloquea valle)
                            Prog 2 = Fin de semana (S-D), 00:00–00:01 (bloquea 24h)

Corriente máxima de carga (sección 1.2 Parámetros Batería con BMS):
  campo "Corriente Máxima de Carga (A)" — input directo de amperios (1–66).
  Solo escritura por aquí; la lectura/verificación se hace por MODBUS (holding 40087).
"""

import functools
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

from app.config import InverterConfig

logger = logging.getLogger(__name__)

# El inversor admite UNA sola sesión web a la vez (igual que su MODBUS). Este lock
# serializa todas las operaciones Playwright para que el controlador de corriente
# (cada 15 min) y el ciclo nocturno no abran sesiones simultáneas y colisionen.
_WEB_LOCK = threading.Lock()


def _serialize_web(fn):
    """Serializa el acceso a la web del inversor (una sesión Playwright a la vez)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _WEB_LOCK:
            return fn(*args, **kwargs)
    return wrapper

# ---------------------------------------------------------------------------
# Estado persistente de la programación aplicada al inversor
# ---------------------------------------------------------------------------

_STATE_FILE = "/app/logs/inverter_schedule_state.json"


@dataclass
class _ScheduleState:
    charge_needed: bool = False
    target_soc_pct: int = 0
    discharge_blocked: bool = False


@dataclass
class ScheduleState:
    """Estado de programación leído directamente del inversor vía web."""
    charge_active: bool
    charge_soc_pct: int      # SOC objetivo configurado (0 si desactivado)
    discharge_blocked: bool
    # False si la config de 6.3.2 no es ninguna de las dos canónicas (libre /
    # bloqueo canónico) — p.ej. el horario invertido de versiones <=1.50. Fuerza
    # la reescritura aunque discharge_blocked coincida con la decisión.
    discharge_recognized: bool = True

    def charge_str(self) -> str:
        return f"ACTIVA (SOC {self.charge_soc_pct}%)" if self.charge_active else "DESACTIVADA"

    def discharge_str(self) -> str:
        return "BLOQUEADA" if self.discharge_blocked else "LIBRE"


def _load_schedule_state(path: str = _STATE_FILE) -> Optional[_ScheduleState]:
    """Lee el último estado de programación aplicado desde disco."""
    try:
        data = json.loads(Path(path).read_text())
        return _ScheduleState(
            charge_needed=bool(data.get("charge_needed", False)),
            target_soc_pct=int(data.get("target_soc_pct", 0)),
            discharge_blocked=bool(data.get("discharge_blocked", False)),
        )
    except Exception:
        return None


def _save_schedule_state(state: _ScheduleState, path: str = _STATE_FILE) -> None:
    """Persiste el estado de programación aplicado al inversor."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "charge_needed": state.charge_needed,
            "target_soc_pct": state.target_soc_pct,
            "discharge_blocked": state.discharge_blocked,
        }, indent=2))
    except Exception as e:
        logger.warning(f"No se pudo guardar estado de programación: {e}")

# Horario valle a programar (6.3.1 carga)
VALLEY_HOUR_ON  = 0
VALLEY_MIN_ON   = 1
VALLEY_HOUR_OFF = 7
VALLEY_MIN_OFF  = 59

# Valores del desplegable
SCHEDULE_DISABLED = "0"  # Desactivado
SCHEDULE_ALLWEEK  = "1"  # Toda la semana
SCHEDULE_WEEKDAY  = "2"  # Entre semana (L-V)
SCHEDULE_WEEKEND  = "3"  # Fin de semana (S-D)

# Etiquetas ESTABLES entre firmwares (no dependen del perfil):
#   - SOC Grid conserva el texto largo en todas las versiones conocidas.
#   - Hora On/Off y Minuto On/Off aparecen dos veces — se localizan por índice.
LABEL_SOC_1  = "SOC Grid 1: Carga máxima para mantener las baterías desde la Red"
LABEL_SOC_2  = "SOC Grid 2: Carga máxima para mantener las baterías desde la Red"


# ---------------------------------------------------------------------------
# Perfiles de etiquetas por versión de firmware (6.3.1 / 6.3.2)
# ---------------------------------------------------------------------------
# La única etiqueta que ha cambiado entre firmwares es la fila del SELECT de
# "Programación Horaria N": antes decía "...: Carga de baterías desde la Red" (6.3.1)
# y "...: Descarga de baterías" (6.3.2); desde ABH1007AD dice "...: Modo" en ambas
# secciones (verificado por dump del DOM el 2026-07-11). El resto de campos (SOC Grid,
# Hora/Minuto On/Off) son estables. Por eso el perfil solo modela esas 4 etiquetas.
#
# Solo el perfil "Modo" (ABH1007AD) está verificado en vivo; el perfil "legacy"
# documenta las etiquetas anteriores (histórico, sin inversor viejo para verificar).
# Firmware desconocido → se usa DEFAULT (el más reciente) con un WARNING para revisar.

@dataclass(frozen=True)
class LabelProfile:
    """Etiquetas de los selects de Programación Horaria que varían por firmware."""
    name: str
    charge_prog_1: str   # 6.3.1 Programación 1
    charge_prog_2: str   # 6.3.1 Programación 2
    disc_prog_1: str     # 6.3.2 Programación 1
    disc_prog_2: str     # 6.3.2 Programación 2


_PROFILE_MODO = LabelProfile(
    name="modo",
    charge_prog_1="Programación Horaria 1: Modo",
    charge_prog_2="Programación Horaria 2: Modo",
    disc_prog_1="Programación Horaria 1: Modo",
    disc_prog_2="Programación Horaria 2: Modo",
)
_PROFILE_LEGACY = LabelProfile(
    name="legacy",
    charge_prog_1="Programación Horaria 1: Carga de baterías desde la Red",
    charge_prog_2="Programación Horaria 2: Carga de baterías desde la Red",
    disc_prog_1="Programación Horaria 1: Descarga de baterías",
    disc_prog_2="Programación Horaria 2: Descarga de baterías",
)

# Mapa firmware exacto → perfil. Añadir aquí cada versión conocida.
FIRMWARE_PROFILES: dict[str, LabelProfile] = {
    "ABH1007AD": _PROFILE_MODO,
}
# Perfil por defecto para firmware desconocido/no leído: el más reciente verificado.
DEFAULT_PROFILE = _PROFILE_MODO

# Perfil activo del proceso, configurable al arrancar con configure_active_profile().
# Las funciones web usan este perfil salvo que se les pase uno explícito.
_ACTIVE_PROFILE = DEFAULT_PROFILE


def resolve_label_profile(firmware: Optional[str]) -> LabelProfile:
    """Devuelve el LabelProfile para una versión de firmware.

    Firmware conocido → su perfil. Desconocido → DEFAULT (más reciente) con WARNING.
    firmware=None (no se pudo leer) → DEFAULT sin warning ruidoso (solo debug)."""
    if firmware is None:
        logger.debug("Firmware no leído — usando perfil de etiquetas por defecto "
                     f"'{DEFAULT_PROFILE.name}'")
        return DEFAULT_PROFILE
    profile = FIRMWARE_PROFILES.get(firmware)
    if profile is None:
        logger.warning(
            f"Firmware '{firmware}' no reconocido — usando perfil de etiquetas más "
            f"reciente '{DEFAULT_PROFILE.name}'. Verifica las etiquetas de 6.3.1/6.3.2 "
            "si la web ha cambiado y añade el firmware a FIRMWARE_PROFILES."
        )
        return DEFAULT_PROFILE
    return profile


def configure_active_profile(firmware: Optional[str]) -> LabelProfile:
    """Fija el perfil de etiquetas activo del proceso a partir del firmware leído.
    Se llama una vez al arrancar (tras read_firmware_version). Devuelve el perfil."""
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = resolve_label_profile(firmware)
    logger.info(f"Perfil de etiquetas activo: '{_ACTIVE_PROFILE.name}' "
                f"(firmware={firmware or 'desconocido'})")
    return _ACTIVE_PROFILE


def _profile(profile: Optional[LabelProfile]) -> LabelProfile:
    """Resuelve el perfil a usar: el explícito si se pasa, si no el activo del proceso."""
    return profile if profile is not None else _ACTIVE_PROFILE


class AutomationError(Exception):
    """Error durante la automatización de la interfaz web."""


@_serialize_web
def set_charge_schedule(
    cfg: InverterConfig,
    charge_needed: bool,
    target_soc_pct: float = 0.0,
    dry_run: bool = False,
    profile: Optional[LabelProfile] = None,
) -> None:
    """
    Configura la programación horaria de carga en la web del inversor.

    Si charge_needed=False: desactiva ambas programaciones (Desactivado)
    Si charge_needed=True:
        Programación 1: Entre semana (L-V), SOC objetivo, 00:01 - 07:59
        Programación 2: Fin de semana (S-D), SOC objetivo, 00:01 - 07:59

    Args:
        cfg:            configuración del inversor
        charge_needed:  si False, desactiva la carga de red
        target_soc_pct: SOC objetivo (solo relevante si charge_needed=True)
        dry_run:        si True, navega y rellena pero NO pulsa Escribir
        profile:        perfil de etiquetas; None → perfil activo del proceso

    Raises:
        AutomationError: si no se puede completar la operación
    """
    prof = _profile(profile)
    soc = int(round(target_soc_pct)) if charge_needed else 0
    action = f"SOC objetivo = {soc}%" if charge_needed else "DESACTIVAR carga de red"
    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Configurando carga horaria: {action}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
            ]
        )
        context = browser.new_context(
            ignore_https_errors=True,
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        page = context.new_page()
        page.set_default_timeout(cfg.browser_timeout_seconds * 1000)

        try:
            _login(page, cfg)
            _navigate_to_charge_schedule(page)
            _read_current_values(page)

            if not charge_needed:
                # Desactivar ambas programaciones — no cargar de red
                _set_select_by_exact_label(page, prof.charge_prog_1, SCHEDULE_DISABLED)
                _set_select_by_exact_label(page, prof.charge_prog_2, SCHEDULE_DISABLED)
            else:
                # Programación 1 — entre semana
                _set_select_by_exact_label(page, prof.charge_prog_1, SCHEDULE_WEEKDAY)
                _set_input_by_exact_label(page, LABEL_SOC_1, str(soc))
                _set_input_by_index(page, "Hora On",    0, str(VALLEY_HOUR_ON))
                _set_input_by_index(page, "Minuto On",  0, str(VALLEY_MIN_ON))
                _set_input_by_index(page, "Hora Off",   0, str(VALLEY_HOUR_OFF))
                _set_input_by_index(page, "Minuto Off", 0, str(VALLEY_MIN_OFF))

                # Programación 2 — fin de semana
                _set_select_by_exact_label(page, prof.charge_prog_2, SCHEDULE_WEEKEND)
                _set_input_by_exact_label(page, LABEL_SOC_2, str(soc))
                _set_input_by_index(page, "Hora On",    1, str(VALLEY_HOUR_ON))
                _set_input_by_index(page, "Minuto On",  1, str(VALLEY_MIN_ON))
                _set_input_by_index(page, "Hora Off",   1, str(VALLEY_HOUR_OFF))
                _set_input_by_index(page, "Minuto Off", 1, str(VALLEY_MIN_OFF))

            if dry_run:
                logger.info("[DRY RUN] Valores rellenados correctamente — NO se pulsa Escribir")
            else:
                _click_write(page)
                state = _load_schedule_state() or _ScheduleState()
                state.charge_needed = charge_needed
                state.target_soc_pct = soc
                _save_schedule_state(state)
                logger.info(f"Programación horaria guardada (SOC={soc}%)")

        except PlaywrightTimeout as e:
            raise AutomationError(f"Timeout en la interfaz web: {e}") from e
        except AutomationError:
            raise
        except Exception as e:
            raise AutomationError(f"Error inesperado en la automatización: {e}") from e
        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# Pasos de navegación
# ---------------------------------------------------------------------------

def _login(page: Page, cfg: InverterConfig) -> None:
    """Fuerza el login navegando directamente a la URL de login."""
    login_url = cfg.web_url.rstrip("/") + "/#/login"
    logger.debug(f"Abriendo login: {login_url}")
    page.goto(login_url)
    page.wait_for_load_state("networkidle")

    # Esperar a que Vue renderice el formulario
    try:
        page.wait_for_selector("input[placeholder='user']", timeout=15000)
    except Exception:
        page.screenshot(path="/app/logs/screenshot_login_error.png")
        raise AutomationError(
            "No apareció el formulario de login. "
            "Captura guardada en /app/logs/screenshot_login_error.png"
        )

    logger.debug("Rellenando credenciales")
    page.locator("input[placeholder='user']").fill(cfg.username)
    page.locator("input[placeholder='password']").fill(cfg.password)
    page.locator("button.btn-info.ml-auto").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    logger.debug("Login completado")


def _navigate_to_charge_schedule(page: Page) -> None:
    """Navega a Configuración → Ajustes avanzados → 6.3.1."""
    logger.debug("Navegando a Configuración")

    # Captura para diagnóstico del idioma/estado de la página
    page.screenshot(path="/app/logs/screenshot_after_login.png")
    logger.debug("Captura guardada en /app/logs/screenshot_after_login.png")

    # Navegación por clics secuenciales — Vue necesita los clics para montar componentes
    logger.debug("Clic en Configuración (menú lateral)")
    page.wait_for_selector("text=Configuración", timeout=15000)
    page.locator("text=Configuración").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    # Clic en "Ajustes avanzados" via JavaScript — independiente del idioma
    page.evaluate("""
        () => {
            const btns = Array.from(document.querySelectorAll('.inv-sett-top-cont button'));
            const btn = btns.find(b => b.innerText.includes('Ajustes') || b.innerText.includes('Advanced'));
            if (btn) btn.click();
            else throw new Error('Botón Ajustes avanzados no encontrado');
        }
    """)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    # Clic en 6.3.1
    page.locator("text=6.3.1").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    # Pulsar "Leer" para cargar los valores actuales y habilitar "Escribir"
    # El botón Leer puede estar en español o inglés según el idioma del navegador
    logger.debug("Pulsando Leer para cargar valores y habilitar Escribir")
    # Leer = btn-success, Escribir = btn-warning — identificamos por clase, independiente del idioma
    page.locator("button.btn-success").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    logger.debug("En pantalla 6.3.1 Programación Horaria: Carga de Batería desde Red")


def _read_current_values(page: Page) -> None:
    """Lee y registra en el log los valores actuales antes de modificar."""
    rows = page.locator("table tr").all()
    current = []
    for row in rows:
        el = row.locator("input, select")
        if el.count() > 0:
            label = row.locator("td").first.inner_text().strip()[:40]
            try:
                value = el.first.input_value()
            except Exception:
                value = "?"
            current.append(f"{label}={value}")
    logger.info("Valores actuales antes de modificar: " + " | ".join(current))


def _set_input_by_exact_label(page: Page, label: str, value: str) -> None:
    """Localiza un input por el texto exacto de su etiqueta y establece el valor."""
    row = page.locator(f"table tr").filter(has_text=label).first
    inp = row.locator("input").first
    inp.click(click_count=3)
    inp.press("Control+a")
    inp.press("Backspace")
    inp.type(value)
    inp.dispatch_event("input")
    inp.dispatch_event("change")
    logger.debug(f"  '{label[:40]}' → {value}")


def _set_input_by_index(page: Page, label: str, index: int, value: str) -> None:
    """
    Localiza un input por etiqueta cuando aparece varias veces en la tabla.
    index=0 → primera ocurrencia (Programación 1)
    index=1 → segunda ocurrencia (Programación 2)
    """
    rows = page.locator(f"table tr").filter(has_text=label).all()
    if len(rows) <= index:
        raise AutomationError(
            f"Se esperaba al menos {index+1} filas con etiqueta '{label}', "
            f"solo se encontraron {len(rows)}"
        )
    inp = rows[index].locator("input").first
    inp.click(click_count=3)
    inp.press("Control+a")
    inp.press("Backspace")
    inp.type(value)
    inp.dispatch_event("input")
    inp.dispatch_event("change")
    logger.debug(f"  '{label}' [{index}] → {value}")


def _set_select_by_exact_label(page: Page, label: str, value: str) -> None:
    """Localiza un select por el texto exacto de su etiqueta y selecciona la opción."""
    row = page.locator("table tr").filter(has_text=label).first
    sel = row.locator("select")
    sel.select_option(value=value)
    sel.dispatch_event("change")
    logger.debug(f"  '{label[:40]}' → {value}")


def _click_write(page: Page) -> None:
    """Pulsa el botón Escribir vía JavaScript (evita comprobación de disabled)."""
    logger.debug("Pulsando Escribir")
    # btn-warning = botón Escribir — usar JS para ignorar el atributo disabled
    # (Vue lo marca disabled hasta que se hace Leer, pero tras rellenar los campos
    #  el estado interno de Vue ya está actualizado aunque el DOM tarde en reflejarlo)
    page.evaluate("""
        () => {
            const btn = document.querySelector('button.btn-warning');
            if (btn) btn.click();
            else throw new Error('Botón Escribir (btn-warning) no encontrado');
        }
    """)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    page.screenshot(path="/app/logs/screenshot_after_write.png")
    logger.debug("Captura post-escritura guardada en /app/logs/screenshot_after_write.png")
    logger.debug("Escribir completado")


# ---------------------------------------------------------------------------
# 6.3.2 — Programación Horaria Descarga de Batería
# ---------------------------------------------------------------------------

# Las etiquetas de los selects de 6.3.2 (Programación Horaria N) dependen del
# firmware y viven en LabelProfile.disc_prog_1/2 (ver sección de perfiles arriba).

# Bloqueo de descarga. MODELO REAL DEL INVERSOR (verificado con el usuario el
# 2026-06-14): la franja "Hora On → Hora Off" es el periodo en que la descarga
# está PERMITIDA; FUERA de ella la batería NO descarga. "Desactivado" = sin
# restricción = descarga libre. Por tanto, para bloquear el valle hay que permitir
# la descarga SOLO fuera del valle (hasta v1.50 se hacía al revés: la franja se
# ponía DENTRO del valle, que es justo lo único que dejaba descargar — bug).
#
# Prog 1 — Entre semana (L-V): permitir 08:00–23:59 → bloquea el valle 00:00–08:00.
DISC_WEEKDAY_ON_H,  DISC_WEEKDAY_ON_M  = 8, 0
DISC_WEEKDAY_OFF_H, DISC_WEEKDAY_OFF_M = 23, 59
# Prog 2 — Fin de semana (S-D): franja nula 00:00–00:01 → bloquea todo el día (valle 24h).
DISC_WEEKEND_ON_H,  DISC_WEEKEND_ON_M  = 0, 0
DISC_WEEKEND_OFF_H, DISC_WEEKEND_OFF_M = 0, 1


@_serialize_web
def set_discharge_schedule(
    cfg: InverterConfig,
    discharge_blocked: bool,
    dry_run: bool = False,
    profile: Optional[LabelProfile] = None,
) -> None:
    """
    Configura la programación horaria de descarga en la web del inversor (6.3.2).

    Si discharge_blocked=False: desactiva ambas programaciones (descarga libre siempre)
    Si discharge_blocked=True:
        Prog 1 Entre semana (L-V): 00:01–07:59  → solo bloquea el valle; a las 08:00 permite descargar
        Prog 2 Fin de semana (S-D): 00:01–23:59 → bloquea todo el día (fin de semana es valle 24h)

    Args:
        cfg:               configuración del inversor
        discharge_blocked: si True, bloquea descarga durante el horario valle
        dry_run:           si True, navega y rellena pero NO pulsa Escribir
        profile:           perfil de etiquetas; None → perfil activo del proceso
    """
    prof = _profile(profile)
    action = "BLOQUEAR descarga valle (L-V 00:01–07:59 · S-D 00:01–23:59)" if discharge_blocked else "Descarga libre"
    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Configurando descarga horaria: {action}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
            ]
        )
        context = browser.new_context(
            ignore_https_errors=True,
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        page = context.new_page()
        page.set_default_timeout(cfg.browser_timeout_seconds * 1000)

        try:
            _login(page, cfg)
            _navigate_to_discharge_schedule(page)
            _read_current_values(page)

            if not discharge_blocked:
                _set_select_by_exact_label(page, prof.disc_prog_1, SCHEDULE_DISABLED)
                _set_select_by_exact_label(page, prof.disc_prog_2, SCHEDULE_DISABLED)
            else:
                # La franja Hora On→Off = horas en que la descarga está PERMITIDA.
                # Prog 1 — Entre semana (L-V): permitir 08:00–23:59 (bloquea el valle 00:00–08:00)
                _set_select_by_exact_label(page, prof.disc_prog_1, SCHEDULE_WEEKDAY)
                _set_input_by_index(page, "Hora On",    0, str(DISC_WEEKDAY_ON_H))
                _set_input_by_index(page, "Minuto On",  0, str(DISC_WEEKDAY_ON_M))
                _set_input_by_index(page, "Hora Off",   0, str(DISC_WEEKDAY_OFF_H))
                _set_input_by_index(page, "Minuto Off", 0, str(DISC_WEEKDAY_OFF_M))

                # Prog 2 — Fin de semana (S-D): franja nula 00:00–00:01 (bloquea todo el día)
                _set_select_by_exact_label(page, prof.disc_prog_2, SCHEDULE_WEEKEND)
                _set_input_by_index(page, "Hora On",    1, str(DISC_WEEKEND_ON_H))
                _set_input_by_index(page, "Minuto On",  1, str(DISC_WEEKEND_ON_M))
                _set_input_by_index(page, "Hora Off",   1, str(DISC_WEEKEND_OFF_H))
                _set_input_by_index(page, "Minuto Off", 1, str(DISC_WEEKEND_OFF_M))

            if dry_run:
                logger.info("[DRY RUN] Valores descarga rellenados — NO se pulsa Escribir")
            else:
                _click_write(page)
                _verify_discharge_written(page, discharge_blocked, prof)
                state = _load_schedule_state() or _ScheduleState()
                state.discharge_blocked = discharge_blocked
                _save_schedule_state(state)
                logger.info(f"Programación descarga guardada y verificada (blocked={discharge_blocked})")

        except PlaywrightTimeout as e:
            raise AutomationError(f"Timeout en la interfaz web (6.3.2): {e}") from e
        except AutomationError:
            raise
        except Exception as e:
            raise AutomationError(f"Error inesperado en automatización descarga: {e}") from e
        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# Helpers de lectura de valores del formulario
# ---------------------------------------------------------------------------

def _get_select_value_by_label(page: Page, label: str) -> str:
    """
    Devuelve el valor actual de un select localizado por texto de etiqueta.

    Lanza AutomationError si la fila/select no se encuentra o el valor está vacío.
    NUNCA asume un valor por defecto: un fallo de lectura devolviendo "Desactivado"
    (SCHEDULE_DISABLED) daría un estado falso pero plausible que el flujo de escritura
    condicionada interpretaría como "ya correcto" y omitiría la reconfiguración. El
    caller (read_inverter_schedule) convierte esta excepción en None → main fuerza la
    reescritura de forma segura (fail-safe).
    """
    try:
        row = page.locator("table tr").filter(has_text=label).first
        value = row.locator("select").input_value()
    except Exception as e:
        raise AutomationError(f"No se pudo leer el select '{label[:50]}': {e}") from e
    if not value:
        raise AutomationError(f"Select '{label[:50]}' encontrado pero sin valor")
    return value


def _get_input_value_by_label(page: Page, label: str) -> str:
    """Devuelve el valor actual de un input localizado por texto de etiqueta."""
    row = page.locator("table tr").filter(has_text=label).first
    return row.locator("input").first.input_value()


def _get_input_value_by_label_index(page: Page, label: str, index: int) -> str:
    """Devuelve el valor de un input cuando la etiqueta aparece varias veces
    (index=0 → Programación 1, index=1 → Programación 2)."""
    rows = page.locator("table tr").filter(has_text=label).all()
    if len(rows) <= index:
        raise AutomationError(f"No se encontró la fila '{label}' [{index}]")
    return rows[index].locator("input").first.input_value()


def _discharge_config_is_blocked(page: Page, profile: LabelProfile) -> bool:
    """
    True solo si 6.3.2 coincide EXACTAMENTE con la configuración canónica de
    bloqueo: Prog1 = Entre semana (L-V) 08:00–23:59 y Prog2 = Fin de semana (S-D)
    00:00–00:01. Cualquier otra cosa (Desactivado o un horario distinto/antiguo)
    devuelve False.
    """
    if (_get_select_value_by_label(page, profile.disc_prog_1) != SCHEDULE_WEEKDAY
            or _get_select_value_by_label(page, profile.disc_prog_2) != SCHEDULE_WEEKEND):
        return False
    try:
        weekday = (
            _get_input_value_by_label_index(page, "Hora On",    0),
            _get_input_value_by_label_index(page, "Minuto On",  0),
            _get_input_value_by_label_index(page, "Hora Off",   0),
            _get_input_value_by_label_index(page, "Minuto Off", 0),
        )
        weekend = (
            _get_input_value_by_label_index(page, "Hora On",    1),
            _get_input_value_by_label_index(page, "Minuto On",  1),
            _get_input_value_by_label_index(page, "Hora Off",   1),
            _get_input_value_by_label_index(page, "Minuto Off", 1),
        )
        weekday_i = tuple(int(float(v)) for v in weekday)
        weekend_i = tuple(int(float(v)) for v in weekend)
    except (AutomationError, ValueError, TypeError) as e:
        logger.warning(f"No se pudieron leer las franjas de 6.3.2: {e}")
        return False
    return (
        weekday_i == (DISC_WEEKDAY_ON_H, DISC_WEEKDAY_ON_M, DISC_WEEKDAY_OFF_H, DISC_WEEKDAY_OFF_M)
        and weekend_i == (DISC_WEEKEND_ON_H, DISC_WEEKEND_ON_M, DISC_WEEKEND_OFF_H, DISC_WEEKEND_OFF_M)
    )


def _verify_discharge_written(page: Page, intended_blocked: bool, profile: LabelProfile) -> None:
    """
    Tras pulsar Escribir, recarga los valores desde el inversor (Leer) y comprueba
    que 6.3.2 quedó como se pretendía. Lanza AutomationError si no coincide.

    Hasta v1.50 el log [DESPUÉS] reflejaba la *intención*, no el estado real: una
    escritura que no persistía pasaba desapercibida. Esto cierra ese hueco.
    """
    try:
        page.locator("button.btn-success").click()   # Leer: recarga desde el inversor
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
    except Exception as e:
        logger.warning(f"No se pudo releer 6.3.2 para verificar la escritura: {e}")
        return
    if intended_blocked:
        ok = _discharge_config_is_blocked(page, profile)
    else:
        ok = (_get_select_value_by_label(page, profile.disc_prog_1) == SCHEDULE_DISABLED
              and _get_select_value_by_label(page, profile.disc_prog_2) == SCHEDULE_DISABLED)
    if not ok:
        raise AutomationError(
            "Verificación de 6.3.2 fallida tras Escribir: el inversor no quedó "
            f"{'BLOQUEADA' if intended_blocked else 'LIBRE'} como se pretendía"
        )
    logger.info(f"6.3.2 verificada tras escritura: {'BLOQUEADA' if intended_blocked else 'LIBRE'}")


def _read_charge_state(page: Page, profile: LabelProfile) -> tuple[bool, int]:
    """Lee el estado actual de 6.3.1. Devuelve (charge_active, soc_pct)."""
    prog1 = _get_select_value_by_label(page, profile.charge_prog_1)
    prog2 = _get_select_value_by_label(page, profile.charge_prog_2)
    active = prog1 != SCHEDULE_DISABLED or prog2 != SCHEDULE_DISABLED
    soc = 0
    if active:
        try:
            soc = int(float(_get_input_value_by_label(page, LABEL_SOC_1)))
        except Exception:
            pass
    return active, soc


def _read_discharge_state(page: Page, profile: LabelProfile) -> tuple[bool, bool]:
    """
    Lee el estado actual de 6.3.2. Devuelve (discharge_blocked, recognized):
    - discharge_blocked: True solo si coincide con la config canónica de bloqueo.
    - recognized: True si la config es una de las dos canónicas (libre = ambas
      Desactivado, o bloqueo canónico). False para cualquier otra (p.ej. el horario
      invertido de versiones <=1.50) → el caller fuerza la reescritura.
    Loguea los valores brutos para diagnóstico.
    """
    prog1 = _get_select_value_by_label(page, profile.disc_prog_1)
    prog2 = _get_select_value_by_label(page, profile.disc_prog_2)
    logger.debug(f"  6.3.2 Prog1={prog1!r} Prog2={prog2!r}")
    if prog1 == SCHEDULE_DISABLED and prog2 == SCHEDULE_DISABLED:
        return False, True            # descarga libre (canónica)
    if _discharge_config_is_blocked(page, profile):
        return True, True             # bloqueo canónico
    logger.warning(
        "6.3.2 tiene una programación de descarga activa pero NO canónica "
        "(posible config antigua invertida <=1.50) — se reescribirá"
    )
    return False, False               # config rara → no bloqueada + forzar reescritura


# ---------------------------------------------------------------------------
# Corriente máxima de carga (sección 1.2 Parámetros Batería con BMS)
# ---------------------------------------------------------------------------

LABEL_CHARGE_CURRENT_MAX = "Corriente Máxima de Carga"


def _navigate_to_battery_params(page: Page) -> None:
    """Navega a Configuración → Ajustes avanzados → 1.2 Parámetros Batería con BMS."""
    logger.debug("Navegando a Configuración (1.2 Parámetros Batería con BMS)")
    page.wait_for_selector("text=Configuración", timeout=15000)
    page.locator("text=Configuración").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    page.evaluate("""
        () => {
            const btns = Array.from(document.querySelectorAll('.inv-sett-top-cont button'));
            const btn = btns.find(b => b.innerText.includes('Ajustes') || b.innerText.includes('Advanced'));
            if (btn) btn.click();
            else throw new Error('Botón Ajustes avanzados no encontrado');
        }
    """)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    page.locator("text=Parámetros Batería con BMS").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    page.locator("button.btn-success").click()   # Leer
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    logger.debug("En pantalla 1.2 Parámetros Batería con BMS")


@_serialize_web
def set_charge_current(cfg: InverterConfig, amps: int, dry_run: bool = False) -> None:
    """
    Escribe la corriente máxima de carga de batería (sección 1.2) en amperios (1–66).

    Solo escribe; la verificación read-back la hace el caller por MODBUS (holding
    40087), que es más fiable que releer la web.

    Args:
        cfg:     configuración del inversor
        amps:    corriente máxima de carga (se acota a [1, 66])
        dry_run: si True, navega y rellena pero NO pulsa Escribir
    """
    amps = max(1, min(66, int(round(amps))))
    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Configurando corriente máxima de carga: {amps} A")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
            ],
        )
        context = browser.new_context(
            ignore_https_errors=True,
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        page = context.new_page()
        page.set_default_timeout(cfg.browser_timeout_seconds * 1000)

        try:
            _login(page, cfg)
            _navigate_to_battery_params(page)
            _read_current_values(page)
            _set_input_by_exact_label(page, LABEL_CHARGE_CURRENT_MAX, str(amps))

            if dry_run:
                logger.info("[DRY RUN] Corriente de carga rellenada — NO se pulsa Escribir")
            else:
                _click_write(page)
                logger.info(f"Corriente máxima de carga escrita: {amps} A")

        except PlaywrightTimeout as e:
            raise AutomationError(f"Timeout en la interfaz web (1.2): {e}") from e
        except AutomationError:
            raise
        except Exception as e:
            raise AutomationError(f"Error inesperado configurando corriente de carga: {e}") from e
        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# Lectura del estado real del inversor (sin escribir nada)
# ---------------------------------------------------------------------------

@_serialize_web
def read_inverter_schedule(
    cfg: InverterConfig, profile: Optional[LabelProfile] = None
) -> Optional[ScheduleState]:
    """
    Lee el estado actual de 6.3.1 (carga) y 6.3.2 (descarga) del inversor vía web.

    Navega en una sola sesión de Playwright a ambas secciones, pulsa Leer en cada
    una y extrae los valores configurados. No escribe nada.

    Args:
        profile: perfil de etiquetas; None → perfil activo del proceso.

    Returns:
        ScheduleState con la configuración actual, o None si no se puede leer.
    """
    prof = _profile(profile)
    logger.info("Leyendo programación del inversor vía web (6.3.1 y 6.3.2)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
            ],
        )
        context = browser.new_context(
            ignore_https_errors=True,
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        page = context.new_page()
        page.set_default_timeout(cfg.browser_timeout_seconds * 1000)
        try:
            _login(page, cfg)

            _navigate_to_charge_schedule(page)
            charge_active, charge_soc = _read_charge_state(page, prof)

            _navigate_to_discharge_schedule(page)
            _read_current_values(page)   # loguea etiquetas y valores reales para diagnóstico
            discharge_blocked, discharge_recognized = _read_discharge_state(page, prof)

            state = ScheduleState(
                charge_active=charge_active,
                charge_soc_pct=charge_soc,
                discharge_blocked=discharge_blocked,
                discharge_recognized=discharge_recognized,
            )
            logger.info(
                f"Programación leída del inversor: "
                f"6.3.1 Carga={state.charge_str()} | "
                f"6.3.2 Descarga={state.discharge_str()}"
            )
            return state
        except Exception as e:
            logger.warning(f"No se pudo leer la programación del inversor: {e}")
            return None
        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# Versión de firmware (menú Actualización)
# ---------------------------------------------------------------------------
# El firmware NO se expone por MODBUS (comprobado por escaneo ASCII de los input y
# holding registers 0..2000). La única forma de leerlo es vía web, en el menú
# "Actualización", fila "Firmware" de la tabla (ej. "ABH1007AD"). "Web" es la versión
# del frontend (ej. "6.1.0"), que no nos interesa para las decisiones de etiquetas.
LABEL_MENU_UPDATE = "Actualización"
LABEL_ROW_FIRMWARE = "Firmware"


def _read_firmware_from_page(page: Page) -> Optional[str]:
    """Extrae el valor de la fila 'Firmware' de la tabla del menú Actualización.
    Devuelve la cadena (p.ej. 'ABH1007AD') o None si no se encuentra."""
    row = page.locator("tr", has_text=LABEL_ROW_FIRMWARE).first
    cells = [c.strip() for c in row.locator("td").all_inner_texts() if c.strip()]
    # Estructura: ['Firmware', 'ABH1007AD']. Descartamos la etiqueta y nos quedamos
    # con el primer valor distinto de la propia etiqueta.
    for c in cells:
        if c != LABEL_ROW_FIRMWARE:
            return c
    return None


@_serialize_web
def read_firmware_version(cfg: InverterConfig) -> Optional[str]:
    """
    Lee la versión de firmware del inversor desde el menú Actualización de la web.

    Devuelve la cadena de firmware (p.ej. "ABH1007AD") o None si no se pudo leer.
    Sesión Playwright propia (un solo login), serializada con el resto de operaciones
    web. NO escribe nada en el inversor.
    """
    logger.debug("Leyendo versión de firmware vía web (menú Actualización)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
            ],
        )
        context = browser.new_context(
            ignore_https_errors=True,
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        page = context.new_page()
        page.set_default_timeout(cfg.browser_timeout_seconds * 1000)
        try:
            _login(page, cfg)
            page.wait_for_selector(f"text={LABEL_MENU_UPDATE}", timeout=15000)
            page.locator(f"text={LABEL_MENU_UPDATE}").first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(3000)
            firmware = _read_firmware_from_page(page)
            if firmware:
                logger.info(f"Firmware del inversor: {firmware}")
            else:
                logger.warning("No se encontró la fila 'Firmware' en el menú Actualización")
            return firmware
        except Exception as e:
            logger.warning(f"No se pudo leer la versión de firmware: {e}")
            return None
        finally:
            context.close()
            browser.close()


def _navigate_to_discharge_schedule(page: Page) -> None:
    """Navega a Configuración → Ajustes avanzados → 6.3.2."""
    logger.debug("Navegando a Configuración (6.3.2)")

    page.screenshot(path="/app/logs/screenshot_after_login.png")
    logger.debug("Captura guardada en /app/logs/screenshot_after_login.png")

    logger.debug("Clic en Configuración (menú lateral)")
    page.wait_for_selector("text=Configuración", timeout=15000)
    page.locator("text=Configuración").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    page.evaluate("""
        () => {
            const btns = Array.from(document.querySelectorAll('.inv-sett-top-cont button'));
            const btn = btns.find(b => b.innerText.includes('Ajustes') || b.innerText.includes('Advanced'));
            if (btn) btn.click();
            else throw new Error('Botón Ajustes avanzados no encontrado');
        }
    """)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    # Clic en 6.3.2
    page.locator("text=6.3.2").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    logger.debug("Pulsando Leer")
    page.locator("button.btn-success").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    logger.debug("En pantalla 6.3.2 Programación Horaria: Descarga de Batería")
