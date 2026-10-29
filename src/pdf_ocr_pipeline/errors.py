from pathlib import Path


class OcrError(Exception):
    """An actionable error safe to display without source text or credentials."""

    def __init__(self, message: str, *, path: Path | str | None = None) -> None:
        super().__init__(message)
        self.path = str(path) if path is not None else None
