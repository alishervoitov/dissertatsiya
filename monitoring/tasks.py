import logging

from celery import shared_task

security_logger = logging.getLogger("security")


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def send_security_alert(self, text):
    """Xavfsizlik xabarini Telegram'ga yuboradi; tarmoq xatosida har daqiqada qayta urinadi."""
    from . import telegram

    try:
        message_id = telegram.send(text, telegram.alert_chat_id())
    except telegram.TelegramError as e:
        security_logger.error("TELEGRAM-ALERT yuborilmadi (urinish %s): %s", self.request.retries + 1, e)
        if self.request.retries >= self.max_retries:
            return None
        raise self.retry(exc=e) from None
    security_logger.info("TELEGRAM-ALERT yuborildi | telegram_message_id=%s", message_id)
    return message_id
