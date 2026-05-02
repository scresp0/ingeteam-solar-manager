"""
automation.py — automatización Playwright para configurar y leer la programación
horaria de carga/descarga de baterías en la interfaz web del inversor Ingeteam.

Ruta de navegación:
  Login → Configuración → Ajustes avanzados →
  6.3.1- Programación Horaria: Carga de Batería desde Red → Leer/Escribir
  6.3.2- Programación Horaria: Descarga de Batería → Leer/Escribir

Campos en la tabla — 6.3.1 (textos exactos de las etiquetas):
  "Programación Horaria 1: Carga de baterías desde la Red"  → select
  "SOC Grid 1: Carga máxima para mantener las baterías desde la Red" → input
  "Hora On"    (aparece 2 veces, una por cada programación) → input
  "Minuto On"  (ídem) → input
  "Hora Off"   (ídem) → input
  "Minuto Off" (ídem) → input
  "Programación Horaria 2: Carga de baterías desde la Red"  → select
  "SOC Grid 2: Carga máxima para mantener las baterías desde la Red" → input

Campos en la tabla — 6.3.2 (textos exactos de las etiquetas):
  "Programación Horaria 1: Descarga de baterías"  → select
  "Hora On"    (aparece 2 veces, una por cada programación) → input
  "Minuto On"  (ídem) → input
  "Hora Off"   (ídem) → input
  "Minuto Off" (ídem) → input
  "Programación Horaria 2: Descarga de baterías"  → select

Opciones del desplegable de tipo:
  0 = Desactivado
  1 = Toda la semana
  2 = Entre semana (L-V)
  3 = Fin de semana (S-D)

Diseño de bloqueo de descarga (6.3.2):
  discharge_blocked=False → Prog 1 = Desactivado, Prog 2 = Desactivado
  discharge_blocked=True  → Prog 1 = Entre semana (L-V), 00:01–07:59
                            Prog 2 = Fin de semana (S-D), 00:01–23:59
  L-V: solo bloquea el valle nocturno; a partir de las 08:00 la batería puede descargar.
  S-D: bloquea todo el día porque el fin de semana la tarifa es valle las 24h.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

from app.config import InverterConfig

logger = logging.getLogger(__name__)

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

# Textos exactos de las etiquetas en la tabla
LABEL_PROG_1 = "Programación Horaria 1: Carga de baterías desde la Red"
LABEL_SOC_1  = "SOC Grid 1: Carga máxima para mantener las baterías desde la Red"
LABEL_PROG_2 = "Programación Horaria 2: Carga de baterías desde la Red"
LABEL_SOC_2  = "SOC Grid 2: Carga máxima para mantener las baterías desde la Red"
# Hora On/Off y Minuto On/Off aparecen dos veces — se localizan por índice


class AutomationError(Exception):
    """Error durante la automatización de la interfaz web."""


def set_charge_schedule(
    cfg: InverterConfig,
    charge_needed: bool,
    target_soc_pct: float = 0.0,
    dry_run: bool = False,
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

    Raises:
        AutomationError: si no se puede completar la operación
    """
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
                _set_select_by_exact_label(page, LABEL_PROG_1, SCHEDULE_DISABLED)
                _set_select_by_exact_label(page, LABEL_PROG_2, SCHEDULE_DISABLED)
            else:
                # Programación 1 — entre semana
                _set_select_by_exact_label(page, LABEL_PROG_1, SCHEDULE_WEEKDAY)
                _set_input_by_exact_label(page, LABEL_SOC_1, str(soc))
                _set_input_by_index(page, "Hora On",    0, str(VALLEY_HOUR_ON))
                _set_input_by_index(page, "Minuto On",  0, str(VALLEY_MIN_ON))
                _set_input_by_index(page, "Hora Off",   0, str(VALLEY_HOUR_OFF))
                _set_input_by_index(page, "Minuto Off", 0, str(VALLEY_MIN_OFF))

                # Programación 2 — fin de semana
                _set_select_by_exact_label(page, LABEL_PROG_2, SCHEDULE_WEEKEND)
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

# Etiquetas de 6.3.2 (misma estructura que 6.3.1)
LABEL_DISC_PROG_1 = "Programación Horaria 1: Descarga de baterías"
LABEL_DISC_PROG_2 = "Programación Horaria 2: Descarga de baterías"

# Horario de bloqueo de descarga — común a ambas programaciones
DISC_HOUR_ON  = 0
DISC_MIN_ON   = 1   # 00:01 (el inversor no acepta 00:00)

# Fin de bloqueo: distinto según el tipo de día
DISC_WEEKDAY_HOUR_OFF = 7    # 07:59 — L-V: solo el valle (08:00 en adelante puede descargar)
DISC_WEEKDAY_MIN_OFF  = 59
DISC_WEEKEND_HOUR_OFF = 23   # 23:59 — S-D: todo el día (fin de semana es valle 24h)
DISC_WEEKEND_MIN_OFF  = 59


def set_discharge_schedule(
    cfg: InverterConfig,
    discharge_blocked: bool,
    dry_run: bool = False,
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
    """
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
                _set_select_by_exact_label(page, LABEL_DISC_PROG_1, SCHEDULE_DISABLED)
                _set_select_by_exact_label(page, LABEL_DISC_PROG_2, SCHEDULE_DISABLED)
            else:
                # Prog 1 — Entre semana (L-V): 00:01–07:59 (solo el valle; puede descargar de 08:00 en adelante)
                _set_select_by_exact_label(page, LABEL_DISC_PROG_1, SCHEDULE_WEEKDAY)
                _set_input_by_index(page, "Hora On",    0, str(DISC_HOUR_ON))
                _set_input_by_index(page, "Minuto On",  0, str(DISC_MIN_ON))
                _set_input_by_index(page, "Hora Off",   0, str(DISC_WEEKDAY_HOUR_OFF))
                _set_input_by_index(page, "Minuto Off", 0, str(DISC_WEEKDAY_MIN_OFF))

                # Prog 2 — Fin de semana (S-D): 00:01–23:59 (todo el día es valle)
                _set_select_by_exact_label(page, LABEL_DISC_PROG_2, SCHEDULE_WEEKEND)
                _set_input_by_index(page, "Hora On",    1, str(DISC_HOUR_ON))
                _set_input_by_index(page, "Minuto On",  1, str(DISC_MIN_ON))
                _set_input_by_index(page, "Hora Off",   1, str(DISC_WEEKEND_HOUR_OFF))
                _set_input_by_index(page, "Minuto Off", 1, str(DISC_WEEKEND_MIN_OFF))

            if dry_run:
                logger.info("[DRY RUN] Valores descarga rellenados — NO se pulsa Escribir")
            else:
                _click_write(page)
                state = _load_schedule_state() or _ScheduleState()
                state.discharge_blocked = discharge_blocked
                _save_schedule_state(state)
                logger.info(f"Programación descarga guardada (blocked={discharge_blocked})")

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
    Devuelve SCHEDULE_DISABLED y loguea un warning si no se encuentra la fila o el valor.
    """
    try:
        row = page.locator("table tr").filter(has_text=label).first
        value = row.locator("select").input_value()
        if not value:
            logger.warning(f"Select '{label[:50]}' encontrado pero sin valor — asumiendo Desactivado")
            return SCHEDULE_DISABLED
        return value
    except Exception as e:
        logger.warning(f"No se pudo leer select '{label[:50]}': {e} — asumiendo Desactivado")
        return SCHEDULE_DISABLED


def _get_input_value_by_label(page: Page, label: str) -> str:
    """Devuelve el valor actual de un input localizado por texto de etiqueta."""
    row = page.locator("table tr").filter(has_text=label).first
    return row.locator("input").first.input_value()


def _read_charge_state(page: Page) -> tuple[bool, int]:
    """Lee el estado actual de 6.3.1. Devuelve (charge_active, soc_pct)."""
    prog1 = _get_select_value_by_label(page, LABEL_PROG_1)
    prog2 = _get_select_value_by_label(page, LABEL_PROG_2)
    active = prog1 != SCHEDULE_DISABLED or prog2 != SCHEDULE_DISABLED
    soc = 0
    if active:
        try:
            soc = int(float(_get_input_value_by_label(page, LABEL_SOC_1)))
        except Exception:
            pass
    return active, soc


def _read_discharge_state(page: Page) -> bool:
    """
    Lee el estado actual de 6.3.2. Devuelve True si la descarga está bloqueada.
    Loguea los valores brutos para diagnóstico.
    """
    prog1 = _get_select_value_by_label(page, LABEL_DISC_PROG_1)
    prog2 = _get_select_value_by_label(page, LABEL_DISC_PROG_2)
    logger.debug(f"  6.3.2 Prog1={prog1!r} Prog2={prog2!r}")
    return prog1 != SCHEDULE_DISABLED or prog2 != SCHEDULE_DISABLED


# ---------------------------------------------------------------------------
# Lectura del estado real del inversor (sin escribir nada)
# ---------------------------------------------------------------------------

def read_inverter_schedule(cfg: InverterConfig) -> Optional[ScheduleState]:
    """
    Lee el estado actual de 6.3.1 (carga) y 6.3.2 (descarga) del inversor vía web.

    Navega en una sola sesión de Playwright a ambas secciones, pulsa Leer en cada
    una y extrae los valores configurados. No escribe nada.

    Returns:
        ScheduleState con la configuración actual, o None si no se puede leer.
    """
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
            charge_active, charge_soc = _read_charge_state(page)

            _navigate_to_discharge_schedule(page)
            _read_current_values(page)   # loguea etiquetas y valores reales para diagnóstico
            discharge_blocked = _read_discharge_state(page)

            state = ScheduleState(
                charge_active=charge_active,
                charge_soc_pct=charge_soc,
                discharge_blocked=discharge_blocked,
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
