# src/vbot/utils/telegram.py
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
