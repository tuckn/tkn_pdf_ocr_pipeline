from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import TextIO

SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

RESET = "\x1b[0m"
LEVEL_COLORS = {
    SUCCESS: "\x1b[32m",
    logging.ERROR: "\x1b[31m",
    logging.CRITICAL: "\x1b[31m",
}


def _enable_windows_virtual_terminal(stream: TextIO) -> bool:
    if os.name != "nt":
        return True
    try:
        handle_number = -12 if stream is sys.stderr else -11
        handle = ctypes.windll.kernel32.GetStdHandle(handle_number)
        mode = ctypes.c_uint()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enabled = mode.value | 0x0004
        return bool(ctypes.windll.kernel32.SetConsoleMode(handle, enabled))
    except (AttributeError, OSError):
        return False


def supports_color(stream: TextIO) -> bool:
    try:
        if not stream.isatty():
            return False
        if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
            return False
        return _enable_windows_virtual_terminal(stream)
    except (AttributeError, OSError):
        return False


class ColorFormatter(logging.Formatter):
    def __init__(self, *, use_color: bool) -> None:
        super().__init__("[%(levelname)s] %(message)s")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        color = LEVEL_COLORS.get(record.levelno) if self.use_color else None
        return f"{color}{rendered}{RESET}" if color else rendered


def configure_logging(
    *, quiet: bool, verbose: bool, stream: TextIO | None = None
) -> logging.Logger:
    # Identity SDK warnings can contain raw sign-in responses and account details.
    # Keep CLI diagnostics in our sanitized adapter messages, even with --verbose.
    for namespace in ("azure.identity", "azure.core", "msal"):
        sdk_logger = logging.getLogger(namespace)
        sdk_logger.handlers.clear()
        sdk_logger.addHandler(logging.NullHandler())
        sdk_logger.propagate = False
    output_stream = sys.stderr if stream is None else stream
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    handler = logging.StreamHandler(output_stream)
    handler.setFormatter(ColorFormatter(use_color=supports_color(output_stream)))
    logger = logging.getLogger("pdf_ocr_pipeline")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(level)
    handler.setLevel(level)
    logger.addHandler(handler)
    return logger


def log_success(logger: logging.Logger, message: str, *args: object) -> None:
    logger.log(SUCCESS, message, *args)
