"""
automation.py — automatización Playwright para configurar la programación
horaria de carga de baterías en la interfaz web del inversor Ingeteam.

Ruta de navegación:
  Login → Configuración → Ajustes avanzados →
  6.3.1- Programación Horaria: Carga de Batería desde Red → Escribir

Campos en la tabla (textos exactos de las etiquetas):
  "Programación Horaria 1: Carga de baterías desde la Red"  → select
  "SOC Grid 1: Carga máxima para mantener las baterías desde la Red" → input
  "Hora On"    (aparece 2 veces, una por cada programación) → input
  "Minuto On"  (ídem) → input
  "Hora Off"   (ídem) → input
  "Minuto Off" (ídem) → input
  "Programación Horaria 2: Carga de baterías desde la Red"  → select
  "SOC Grid 2: Carga máxima para mantener las baterías desde la Red" → input

Opciones del desplegable de tipo:
  0 = Desactivado
  1 = Toda la semana
  2 = Entre semana (L-V)
  3 = Fin de semana (S-D)
"""

import logging
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

from app.config import InverterConfig

logger = logging.getLogger(__name__)

# Horario valle a programar
VALLEY_HOUR_ON  = 0
VALLEY_MIN_ON   = 1
VALLEY_HOUR_OFF = 7
VALLEY_MIN_OFF  = 59

# Valores del desplegable
SCHEDULE_DISABLED = "0"  # Desactivado
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
    page.wait_for_selector(".inv-sett-top-cont", timeout=15000)
    page.wait_for_timeout(2000)

    # Captura para verificar que estamos en Configuración
    page.screenshot(path="/app/logs/screenshot_config_page.png")
    logger.debug("Captura de página de configuración guardada")

    # Clic en "Ajustes avanzados"
    logger.debug("Clic en Ajustes avanzados")
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
