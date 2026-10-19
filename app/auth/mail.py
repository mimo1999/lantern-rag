import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from fastapi import Depends
from fastapi.concurrency import run_in_threadpool

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class Mailer(Protocol):
    async def send_password_reset(self, to: str, link: str) -> None: ...


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _send(self, message: EmailMessage) -> None:
        s = self._settings
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password.get_secret_value())
            smtp.send_message(message)

    async def send_password_reset(self, to: str, link: str) -> None:
        message = EmailMessage()
        message["From"] = self._settings.smtp_from
        message["To"] = to
        message["Subject"] = "Reset your password"
        message.set_content(
            f"Use this link to set a new password. It works once and expires soon:\n\n{link}\n\n"
            "If you did not ask for this, ignore this email."
        )
        await run_in_threadpool(self._send, message)


class UnconfiguredMailer:
    """No SMTP server configured. In development the link is logged so the flow can be tried;
    elsewhere nothing is sent (a reset link is a credential and must not reach the logs)."""

    def __init__(self, development: bool) -> None:
        self._development = development

    async def send_password_reset(self, to: str, link: str) -> None:
        if self._development:
            logger.warning("password_reset_link_dev_only", extra={"link": link})
        else:
            logger.error("password_reset_not_sent_smtp_unconfigured")


def get_mailer(settings: Settings = Depends(get_settings)) -> Mailer:
    if settings.smtp_host:
        return SmtpMailer(settings)
    return UnconfiguredMailer(settings.app_env == "development")
