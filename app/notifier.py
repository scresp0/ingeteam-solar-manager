"""
notifier.py — envío de email con el resumen de cada ciclo de ejecución.

Implementado como un handler de logging en memoria que acumula todos los
mensajes del ciclo y los envía en un único email al finalizar.

El email incluye cuerpo HTML enriquecido (con fallback en texto plano):
  - Cabecera con color semántico según resultado (OK / carga activada / error)
  - Barra de SOC con color por nivel
  - Tabla de datos clave extraídos del log (SOC, forecast, modo inversor…)
  - Bloque de log completo con coloreado por nivel

Configuración en config.yaml bajo system.email:
  enabled:       true/false
  smtp_host:     smtp.gmail.com
  smtp_port:     587
  smtp_user:     tu@email.com
  smtp_password: tu-contraseña
  mail_from:     solar-manager@tudominio.com
  mail_to:       destinatario@email.com
  use_tls:       true   (STARTTLS, puerto 587)
  use_ssl:       false  (SSL directo, puerto 465)
"""

import logging
import logging.handlers
import re
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO

from app.config import EmailConfig

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Paleta de colores (compatible con la mayoría de clientes de email)
# ─────────────────────────────────────────────────────────────────────────────
_C = {
    "ok":      "#27ae60",
    "warn":    "#e67e22",
    "err":     "#c0392b",
    "bg":      "#f0f4f8",
    "card":    "#ffffff",
    "text":    "#1a2332",
    "muted":   "#6b7a8d",
    "border":  "#dce4ed",
    "mono_bg": "#1e2a3a",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers HTML (inline styles — sin CSS externo para compatibilidad email)
# ─────────────────────────────────────────────────────────────────────────────

def _log_line_color(line: str) -> str:
    if "ERROR" in line:   return "#ff6b6b"
    if "WARNING" in line: return "#ffd93d"
    if "INFO" in line:    return "#a8c4e0"
    return "#6b8cae"


# Patrón que identifica una línea de log estándar: empieza con timestamp
_LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _render_log_lines(log_content: str) -> str:
    """
    Convierte el texto de log en HTML coloreado distinguiendo:
    - Líneas estándar (con timestamp): color por nivel (INFO/WARNING/ERROR)
    - Líneas de bloque/continuación (sin timestamp): color tenue + indentación
      Dentro de estas, las que empiezan por → o contienen === se destacan.
    """
    html = ""
    for raw_line in log_content.strip().splitlines():
        escaped = raw_line.replace("&", "&amp;").replace("<", "&lt;")

        if _LOG_LINE_RE.match(raw_line):
            color  = _log_line_color(raw_line)
            indent = ""
        elif raw_line.strip() == "":
            html += '<div style="height:4px;"></div>\n'
            continue
        elif raw_line.lstrip().startswith("→"):
            color  = "#ffffff"
            indent = "padding-left:16px;"
        elif "===" in raw_line:
            color  = "#7ecbff"
            indent = ""
        else:
            color  = "#5d8aaa"
            indent = "padding-left:16px;"

        html += (
            f'<div style="padding:1px 0;font-size:11.5px;color:{color};'
            f'line-height:1.55;white-space:pre-wrap;word-break:break-word;{indent}">'
            f'{escaped}</div>\n'
        )
    return html


def _parse_decision(log: str) -> tuple:
    """
    Extrae del log: soc (float|None), forecast (float|None),
    charge (bool|None), mode (str|None).
    """
    soc = charge = forecast = mode = None
    for line in log.splitlines():
        m = re.search(r'SOC[^\d]*([\d.]+)\s*%', line, re.I)
        if m:
            soc = float(m.group(1))
        m = re.search(r'[Ff]orecast[^\d]*([\d.]+)\s*kWh', line)
        if m:
            forecast = float(m.group(1))
        if re.search(r'charge_needed\s*=\s*True|carga.*activ', line, re.I):
            charge = True
        if re.search(r'charge_needed\s*=\s*False|sin carga|desactiv', line, re.I):
            charge = False
        m = re.search(r'[Mm]odo[^\w]+([\w\s%]+?)(?:\s*(?:→|select|configur|aplicad)|\s*$)', line)
        if m:
            mode = m.group(1).strip()
    return soc, forecast, charge, mode


def _soc_bar(soc: float | None) -> str:
    if soc is None:
        return ""
    pct   = int(max(0, min(100, soc)))
    color = _C["ok"] if pct >= 60 else (_C["warn"] if pct >= 35 else _C["err"])
    return f"""
      <div style="margin:0 0 20px;">
        <div style="display:flex;justify-content:space-between;margin-bottom:5px;">
          <span style="font-size:12px;color:{_C['muted']};font-weight:600;
                       text-transform:uppercase;letter-spacing:.5px;">Estado batería</span>
          <span style="font-size:12px;font-weight:700;color:{color};">{pct}%</span>
        </div>
        <div style="background:{_C['border']};border-radius:99px;height:10px;overflow:hidden;">
          <div style="width:{pct}%;background:{color};height:10px;border-radius:99px;"></div>
        </div>
      </div>"""


def _kv_row(label: str, value: str, value_color: str | None = None) -> str:
    vc = value_color or _C["text"]
    return f"""
      <tr>
        <td style="padding:9px 0;border-bottom:1px solid {_C['border']};
                   font-size:13px;color:{_C['muted']};">{label}</td>
        <td style="padding:9px 0;border-bottom:1px solid {_C['border']};
                   font-size:13px;font-weight:600;color:{vc};
                   text-align:right;">{value}</td>
      </tr>"""


def _build_html(
    success: bool,
    log_content: str,
    duration_s: int,
    start_time: datetime,
    dry_run: bool,
) -> str:
    soc, forecast, charge, mode = _parse_decision(log_content)

    # Cabecera semántica
    if not success:
        accent = _C["err"];  badge = "✗ Error";          title = "Ciclo finalizado con errores"
    elif dry_run:
        accent = _C["warn"]; badge = "Dry run";           title = "Ciclo completado (simulación)"
    elif charge is True:
        accent = _C["warn"]; badge = "🔌 Carga activada"; title = "Carga desde la red programada"
    elif charge is False:
        accent = _C["ok"];   badge = "☀️ Sin carga";      title = "No se requiere carga desde la red"
    else:
        accent = _C["ok"];   badge = "✓ Completado";      title = "Ciclo completado correctamente"

    date_str = start_time.strftime("%d/%m/%Y")
    time_str = start_time.strftime("%H:%M")

    # Filas tabla de resumen
    rows = ""
    if soc is not None:
        bar_color = _C["ok"] if soc >= 60 else (_C["warn"] if soc >= 35 else _C["err"])
        rows += _kv_row("SOC batería", f"{soc:.0f}%", bar_color)
    if forecast is not None:
        rows += _kv_row("Forecast solar (mañana)", f"{forecast:.1f} kWh")
    if mode:
        rows += _kv_row(
            "Modo inversor configurado",
            f'<span style="background:{accent};color:#fff;padding:2px 10px;'
            f'border-radius:99px;font-size:12px;">{mode}</span>',
        )
    rows += _kv_row("Duración del ciclo", f"{duration_s}s")
    rows += _kv_row("Timestamp", f"{date_str} {time_str}")

    # Log renderizado con coloreado por tipo de línea
    log_lines_html = _render_log_lines(log_content)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Solar Manager — {date_str}</title>
</head>
<body style="margin:0;padding:0;background:{_C['bg']};
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:{_C['bg']};padding:40px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">

  <!-- CABECERA -->
  <tr>
    <td style="background:{accent};border-radius:14px 14px 0 0;
               padding:30px 36px 26px;text-align:left;">
      <div style="font-size:11px;font-weight:700;color:rgba(255,255,255,.6);
                  letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;">
        ⚡ Solar Manager
      </div>
      <div style="display:inline-block;background:rgba(0,0,0,.18);border-radius:99px;
                  padding:4px 14px;font-size:12px;font-weight:700;color:#fff;
                  margin-bottom:14px;">
        {badge}
      </div>
      <h1 style="margin:0;font-size:21px;font-weight:700;color:#fff;line-height:1.3;">
        {title}
      </h1>
      <p style="margin:8px 0 0;font-size:13px;color:rgba(255,255,255,.7);">
        {date_str} · {time_str}
      </p>
    </td>
  </tr>

  <!-- CUERPO — resumen (ancho acotado) -->
  <tr>
    <td style="background:{_C['card']};border-radius:0 0 14px 14px;
               padding:28px 36px 32px;
               border:1px solid {_C['border']};border-top:none;">

      {_soc_bar(soc)}

      <table width="100%" cellpadding="0" cellspacing="0">
        {rows}
      </table>

    </td>
  </tr>

</table><!-- fin tabla 560px -->

<!-- LOG — tabla independiente al 90% del wrapper para mantener márgenes -->
<table width="90%" cellpadding="0" cellspacing="0" style="margin-top:16px;margin-left:auto;margin-right:auto;">
  <tr>
    <td style="padding:0 0 4px;">
      <div style="font-size:11px;font-weight:700;color:{_C['muted']};
                  letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px;">
        Log de ejecución
      </div>
      <div style="background:{_C['mono_bg']};border-radius:10px;padding:18px 22px;
                  font-family:'SF Mono','Fira Code','Consolas',monospace;
                  overflow-x:auto;border:1px solid #2d3f55;width:100%;
                  box-sizing:border-box;">
        {log_lines_html}
      </div>
    </td>
  </tr>
</table>

<!-- PIE -->
<table width="100%" cellpadding="0" cellspacing="0">
  <tr>
    <td style="padding:18px 0 4px;text-align:center;">
      <p style="margin:0;font-size:11px;color:{_C['muted']};">
        Ingeteam INGECON SUN STORAGE 1PLAY TL-M &nbsp;·&nbsp; solar-manager
      </p>
    </td>
  </tr>
</table>

</td></tr>
</table><!-- fin wrapper exterior -->
</body>
</html>"""


def _build_plain(
    success: bool,
    log_content: str,
    duration_s: int,
    start_time: datetime,
) -> str:
    status = "✓ Completado correctamente" if success else "✗ Finalizado con errores"
    return (
        f"solar-manager — resumen del ciclo\n"
        f"{'=' * 50}\n"
        f"Fecha     : {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Duración  : {duration_s}s\n"
        f"Estado    : {status}\n"
        f"{'=' * 50}\n\n"
        f"LOG COMPLETO:\n\n"
        f"{log_content}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Clase pública — API idéntica a la versión anterior
# ─────────────────────────────────────────────────────────────────────────────

class CycleEmailNotifier:
    """
    Acumula los logs de un ciclo completo y los envía por email al finalizar.

    Uso:
        notifier = CycleEmailNotifier(cfg.email)
        notifier.attach()            # empieza a capturar logs
        ... ejecutar el ciclo ...
        notifier.send(success=True)  # envía el email y desconecta
    """

    def __init__(self, cfg: EmailConfig):
        self.cfg = cfg
        self._buffer = StringIO()
        self._handler: logging.Handler | None = None
        self._start_time = datetime.now()

    def attach(self) -> None:
        """Conecta el notifier al root logger para capturar todos los mensajes."""
        if not self.cfg.enabled:
            return

        self._buffer = StringIO()
        self._start_time = datetime.now()

        self._handler = logging.StreamHandler(self._buffer)
        self._handler.setLevel(logging.DEBUG)
        self._handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
        )
        logging.getLogger().addHandler(self._handler)
        logger.debug("Email notifier activado")

    def send(self, success: bool) -> None:
        """Envía el email con los logs acumulados y desconecta el handler."""
        if not self.cfg.enabled:
            return

        # Desconectar antes de enviar para no capturar el propio envío
        if self._handler:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None

        if not self._validate_config():
            return

        duration    = datetime.now() - self._start_time
        log_content = self._buffer.getvalue()

        try:
            self._send_email(success, log_content, duration.seconds)
            logger.info(f"Email de notificación enviado a {self.cfg.mail_to}")
            print(f"[notifier] Email enviado correctamente a {self.cfg.mail_to}", flush=True)
        except Exception as e:
            logger.error(f"Error al enviar email de notificación: {e}")
            print(f"[notifier] ERROR al enviar email: {e}", flush=True)

    # ── privados ──────────────────────────────────────────────────────────────

    def _validate_config(self) -> bool:
        if not all([self.cfg.smtp_host, self.cfg.mail_from, self.cfg.mail_to]):
            logger.warning(
                "Email notifier activado pero faltan parámetros "
                "(smtp_host, mail_from, mail_to)"
            )
            return False
        return True

    def _send_email(self, success: bool, log_content: str, duration_s: int) -> None:
        dry_run = "[DRY RUN]" in log_content
        dry     = " [DRY RUN]" if dry_run else ""
        status  = "OK" if success else "ERROR"
        subject = (
            f"[solar-manager] Ciclo {status}{dry} — "
            f"{self._start_time.strftime('%Y-%m-%d %H:%M')}"
        )

        html_body  = _build_html(success, log_content, duration_s, self._start_time, dry_run)
        plain_body = _build_plain(success, log_content, duration_s, self._start_time)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = self.cfg.mail_from
        msg["To"]      = self.cfg.mail_to

        # Texto plano primero (fallback), HTML después (preferido por los clientes)
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body,  "html",  "utf-8"))

        self._smtp_send(msg)

    def _smtp_send(self, msg: MIMEMultipart) -> None:
        """Envía el mensaje vía SMTP con TLS o SSL según configuración."""
        host     = self.cfg.smtp_host
        port     = self.cfg.smtp_port
        user     = self.cfg.smtp_user
        password = self.cfg.smtp_password

        ctx = ssl.create_default_context()
        if not self.cfg.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE

        SMTP_TIMEOUT = 15

        if self.cfg.use_ssl:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=SMTP_TIMEOUT) as server:
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as server:
                if self.cfg.use_tls:
                    server.starttls(context=ctx)
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
