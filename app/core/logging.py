from __future__ import annotations

import contextvars
import logging
from logging.config import dictConfig
from uuid import uuid4

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIDContext:
    def set_request_id(self, request_id: str) -> None:
        request_id_var.set(request_id)

    def new_request_id(self) -> str:
        request_id = str(uuid4())
        self.set_request_id(request_id)
        return request_id


request_id_context = RequestIDContext()


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging() -> None:
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIDFilter}},
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["request_id"],
                }
            },
            "root": {"level": "INFO", "handlers": ["console"]},
        }
    )
