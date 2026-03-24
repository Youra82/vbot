# src/vbot/utils/telegram.py
import os
import requests
import logging

logger = logging.getLogger(__name__)


def send_message(bot_token, chat_id, message):
    """Sendet eine Textnachricht an einen Telegram-Chat."""
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token oder Chat-ID nicht konfiguriert. Nachricht nicht gesendet.")
        return

    escape_chars = r'_*[]()~`>#+-=|{}.!'
    escaped = message
    for char in escape_chars:
        escaped = escaped.replace(char, f'\\{char}')

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': escaped, 'parse_mode': 'MarkdownV2'}

    try:
        response = requests.post(api_url, data=payload, timeout=10)
        response.raise_for_status()
        logger.debug("Telegram-Nachricht erfolgreich gesendet.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Fehler beim Senden der Telegram-Nachricht: {e}")
    except Exception as e:
        logger.error(f"Unerwarteter Fehler beim Telegram-Versand: {e}")


def send_document(bot_token, chat_id, file_path, caption=""):
    """Sendet eine Datei (z.B. Excel) an einen Telegram-Chat."""
    if not bot_token or not chat_id:
        logger.warning("Telegram nicht konfiguriert. Datei nicht gesendet.")
        return
    if not os.path.exists(file_path):
        logger.error(f"Datei nicht gefunden: {file_path}")
        return
    try:
        with open(file_path, 'rb') as f:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendDocument",
                data={'chat_id': chat_id, 'caption': caption},
                files={'document': (os.path.basename(file_path), f)},
                timeout=30,
            ).raise_for_status()
        logger.debug(f"Telegram-Dokument gesendet: {file_path}")
    except Exception as e:
        logger.error(f"Fehler beim Senden des Telegram-Dokuments: {e}")
