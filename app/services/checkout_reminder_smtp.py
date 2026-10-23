"""Explicitly gated SMTP transport. Durable reservation/retries belong to the caller.

SMTP has no exactly-once API. An interrupted DATA command has an unknown outcome
and MUST NOT be automatically retried. This module never logs message contents.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import smtplib
import ssl
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr

from app.core.config import settings


class DefinitiveReminderRejection(RuntimeError):
    """No message was accepted; the caller may make a bounded retry."""


class PermanentReminderRejection(RuntimeError):
    """An explicit permanent SMTP rejection; do not retry."""


class UnsupportedReminderRecipient(ValueError):
    """This recipient cannot be submitted by the configured SMTP adapter."""


def _address(value):
    # Require a single plain mailbox, never a display-name/header/list envelope.
    if (not isinstance(value, str) or len(value) > 254
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value)):
        raise ValueError('reminder_smtp_address_invalid')
    return value


def _response(code):
    if 400 <= code < 500:
        raise DefinitiveReminderRejection('reminder_smtp_temporarily_rejected')
    if 500 <= code < 600:
        raise PermanentReminderRejection('reminder_smtp_permanently_rejected')
    raise RuntimeError('reminder_smtp_response_unknown')


class CheckoutReminderSMTPProvider:
    is_fake = False

    async def send(self, *, delivery_key, email, message):
        # Check before starting a thread as well as immediately before connection.
        self._validate(email, delivery_key, message)
        await asyncio.to_thread(self._send, delivery_key, email, message)

    @staticmethod
    def _validate(email, delivery_key, message):
        if not (getattr(settings, 'checkout_reminder_live_enabled', False)
                and getattr(settings, 'checkout_reminder_launch_approved', False)
                and settings.email_notifications_enabled):
            raise RuntimeError('reminder_smtp_live_disabled')
        _address(settings.smtp_from_email)
        if (not settings.smtp_host or not re.fullmatch(r'[A-Za-z0-9.-]+', settings.smtp_host)
                or not 1 <= settings.smtp_port <= 65535
                or not 1 <= settings.smtp_timeout_seconds <= 120):
            raise ValueError('reminder_smtp_config_invalid')
        if not isinstance(delivery_key, str) or not 1 <= len(delivery_key) <= 256:
            raise ValueError('reminder_smtp_delivery_key_invalid')
        if not isinstance(message, EmailMessage):
            raise ValueError('reminder_smtp_message_invalid')
        if re.search(r'[\r\n\x00]', settings.smtp_from_name or ''):
            raise ValueError('reminder_smtp_sender_invalid')
        try:
            _address(email)
        except ValueError:
            raise UnsupportedReminderRecipient('reminder_smtp_recipient_unsupported') from None

    def _send(self, delivery_key, email, message):
        self._validate(email, delivery_key, message)
        # Clone: renderer/fake previews remain unchanged and retries retain content.
        outgoing = BytesParser(policy=policy.SMTP).parsebytes(message.as_bytes(policy=policy.SMTP))
        for header in ('To', 'From', 'Bcc', 'Cc', 'Message-ID', 'Resent-To', 'Resent-From', 'Resent-Bcc', 'Resent-Cc'):
            if header in outgoing:
                del outgoing[header]
        outgoing['To'] = email
        outgoing['From'] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
        identifier = hashlib.sha256(delivery_key.encode('utf-8')).hexdigest()
        outgoing['Message-ID'] = f'<checkout-reminder.{identifier}@soulcuts.invalid>'
        payload = outgoing.as_bytes(policy=policy.SMTP)
        smtp = None
        data_started = False
        accepted = False
        try:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds)
            code, _ = smtp.ehlo()
            if code != 250:
                _response(code)
            if settings.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
                code, _ = smtp.ehlo()
                if code != 250:
                    _response(code)
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            code, _ = smtp.mail(settings.smtp_from_email)
            if code != 250:
                _response(code)
            code, _ = smtp.rcpt(email)
            if code not in (250, 251, 252):
                _response(code)
            # Set before data(): disconnect/timeout anywhere in DATA is ambiguous.
            data_started = True
            code, _ = smtp.data(payload)
            if code != 250:
                _response(code)
            accepted = True
        except (DefinitiveReminderRejection, PermanentReminderRejection):
            raise
        except smtplib.SMTPResponseException as exc:
            # An explicit negative SMTP response proves rejection, including DATA.
            _response(exc.smtp_code)
        except Exception:
            if not data_started:
                raise DefinitiveReminderRejection('reminder_smtp_not_submitted') from None
            raise RuntimeError('reminder_smtp_delivery_uncertain') from None
        finally:
            if smtp is not None:
                if accepted:
                    try:
                        smtp.quit()
                    except Exception:
                        # A lost QUIT does not undo the explicit DATA 250 response.
                        pass
                try:
                    smtp.close()
                except Exception:
                    pass
