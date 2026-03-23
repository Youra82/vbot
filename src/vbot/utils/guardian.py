# src/vbot/utils/guardian.py
import logging
import functools
import traceback

logger = logging.getLogger(__name__)


def guardian_decorator(func):
    """
    Decorator der unerwartete Exceptions abfaengt und loggt,
    ohne den gesamten Prozess zu beenden.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(
                f"[Guardian] Unerwarteter Fehler in '{func.__name__}': {e}\n"
                f"{traceback.format_exc()}"
            )
            return None
    return wrapper
