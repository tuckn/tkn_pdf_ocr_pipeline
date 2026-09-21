import io
import logging

import pytest

from pdf_ocr_pipeline.io_utils import atomic_write
from pdf_ocr_pipeline.logging_config import (
    SUCCESS,
    ColorFormatter,
    configure_logging,
    supports_color,
)


def test_atomic_write_no_clobber_and_cleanup(tmp_path):
    target = tmp_path / "result"
    atomic_write(target, b"first", replace=False)
    with pytest.raises(FileExistsError):
        atomic_write(target, b"second", replace=False)
    assert target.read_bytes() == b"first"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "level,color",
    [
        (SUCCESS, "\x1b[32m"),
        (logging.ERROR, "\x1b[31m"),
        (logging.CRITICAL, "\x1b[31m"),
        (logging.INFO, ""),
    ],
)
def test_log_colors(level, color):
    record = logging.LogRecord("test", level, "", 0, "message", (), None)
    rendered = ColorFormatter(use_color=True).format(record)
    assert rendered.startswith(color + "[")
    if color:
        assert rendered.endswith("\x1b[0m")
    assert "\x1b" not in ColorFormatter(use_color=False).format(record)
    assert logging.INFO < SUCCESS < logging.WARNING


@pytest.mark.parametrize(
    "quiet,verbose,expected",
    [
        (False, False, ["INFO", "SUCCESS", "ERROR"]),
        (True, False, ["ERROR"]),
        (False, True, ["DEBUG", "INFO", "SUCCESS", "ERROR"]),
    ],
)
def test_log_filter(quiet, verbose, expected):
    stream = io.StringIO()
    logger = configure_logging(quiet=quiet, verbose=verbose, stream=stream)
    for level in (logging.DEBUG, logging.INFO, SUCCESS, logging.ERROR):
        logger.log(level, "message")
    assert [line.split("]")[0][1:] for line in stream.getvalue().splitlines()] == expected


def test_color_fallback(monkeypatch):
    import pdf_ocr_pipeline.logging_config as module

    stream = io.StringIO()
    assert not supports_color(stream)
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "")
    assert not supports_color(stream)
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert not supports_color(stream)
    monkeypatch.delenv("TERM")
    monkeypatch.setattr(module, "_enable_windows_virtual_terminal", lambda output: False)
    assert not supports_color(stream)
