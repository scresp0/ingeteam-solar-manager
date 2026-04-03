"""
notifier.py — envío de email con el resumen de cada ciclo de ejecución.

Implementado como un handler de logging en memoria (MemoryHandler + SMTPHandler)
que acumula todos los mensajes del ciclo y los envía en un único email al finalizar.

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
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO

from app.config import EmailConfig

logger = logging.getLogger(__name__)


class CycleEmailNotifier:
    """
    Acumula los logs de un ciclo completo y los envía por email al finalizar.

    Uso:
        notifier = CycleEmailNotifier(cfg.email)
        notifier.attach()          # empieza a capturar logs
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

        # Desconectar handler antes de enviar para no capturar el propio envío
        if self._handler:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None

        if not self._validate_config():
            return

        duration = datetime.now() - self._start_time
        log_content = self._buffer.getvalue()

        try:
            self._send_email(success, log_content, duration.seconds)
            logger.info(f"Email de notificación enviado a {self.cfg.mail_to}")
        except Exception as e:
            logger.error(f"Error al enviar email de notificación: {e}")

    def _validate_config(self) -> bool:
        required = [
            self.cfg.smtp_host,
            self.cfg.mail_from,
            self.cfg.mail_to,
        ]
        if not all(required):
            logger.warning(
                "Email notifier activado pero faltan parámetros "
                "(smtp_host, mail_from, mail_to)"
            )
            return False
        return True

    def _send_email(self, success: bool, log_content: str, duration_s: int) -> None:
        """Construye y envía el email."""
        status = "OK" if success else "ERROR"
        dry = " [DRY RUN]" if "[DRY RUN]" in log_content else ""
        subject = f"[solar-manager] Ciclo {status}{dry} — {self._start_time.strftime('%Y-%m-%d %H:%M')}"

        # Cuerpo en texto plano
        body = (
            f"solar-manager — resumen del ciclo\n"
            f"{'='*50}\n"
            f"Fecha     : {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Duración  : {duration_s}s\n"
            f"Estado    : {'✓ Completado correctamente' if success else '✗ Finalizado con errores'}\n"
            f"{'='*50}\n\n"
            f"LOG COMPLETO:\n\n"
            f"{log_content}"
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.cfg.mail_from
        msg["To"] = self.cfg.mail_to
        msg.attach(MIMEText(body, "plain", "utf-8"))

        self._smtp_send(msg)

    def _smtp_send(self, msg: MIMEMultipart) -> None:
        """Envía el mensaje vía SMTP con TLS o SSL según configuración."""
        host = self.cfg.smtp_host
        port = self.cfg.smtp_port
        user = self.cfg.smtp_user
        password = self.cfg.smtp_password

        # Para servidores internos con certificados autofirmados o sin hostname
        # usamos verify_ssl=False — aceptable en redes privadas
        if self.cfg.verify_ssl:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        if self.cfg.use_ssl:
            # SSL directo (puerto 465)
            with smtplib.SMTP_SSL(host, port, context=ctx) as server:
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            # STARTTLS (puerto 587) o sin cifrado
            with smtplib.SMTP(host, port) as server:
                if self.cfg.use_tls:
                    server.starttls(context=ctx)
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
