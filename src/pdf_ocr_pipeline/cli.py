from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, NoReturn

from . import __version__
from .auth import SCOPE, BrowserCredential
from .config import SCHEMA_VERSION, init_config, resolve_config, user_config_path
from .errors import OcrError
from .logging_config import configure_logging, log_success
from .output import emit, public_result
from .pdf import inspect_pdf
from .pipeline import Options, run, run_sources


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        raise OcrError(message)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print the final result as JSON (default: key=value; does not save OCR JSON)",
    )
    parser.add_argument(
        "--full-output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Include every skipped file as well as failures and results (text or --json)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="Explicit YAML overlay (after user and CWD settings)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Only ERROR/CRITICAL logs; result output is retained",
    )
    group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Include DEBUG logs without credentials or OCR content",
    )


def _ocr_options(parser: argparse.ArgumentParser) -> None:
    _common(parser)
    parser.add_argument("--state-dir", help="Persistent job/lock directory")
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--save-json",
        action="store_true",
        help="Also save OCR text JSON (default: PDF only; independent of --json display)",
    )
    output_mode.add_argument(
        "--only-json",
        action="store_true",
        help="Analyze all input pages and save JSON without creating a PDF",
    )
    parser.add_argument(
        "--redo-ocr",
        action="store_true",
        help="Replace invisible OCR text on scanned pages; preserve images and native text pages",
    )
    parser.add_argument(
        "--min-age-seconds",
        type=int,
        help="Skip files modified more recently than this (default 30)",
    )
    parser.add_argument("--endpoint", help="Azure Document Intelligence HTTPS resource origin")
    parser.add_argument(
        "--auth-mode",
        choices=("browser", "default_credential", "key"),
        help="Authentication (default browser; no external CLI credentials)",
    )
    parser.add_argument("--locale", help="OCR locale hint; default auto-detect")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read/validate locally only; no auth, network, writes, deletion, or OCR",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing output file(s) after making backups; default is skip",
    )


def parser() -> Parser:
    root = Parser(
        description="Create searchable PDFs with Azure prebuilt-read. Normal OCR uploads "
        "PDFs and writes results; --dry-run is local and read-only. "
        "Results use key=value by default, "
        "or --json for machine processing. Logs go to stderr. Exit: 0 success, 1 partial "
        "failure/unexpected error, 2 argument/config/file error, 130 interrupted."
    )
    root.add_argument("--version", action="version", version=__version__)
    _common(root)
    commands = root.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Create or inspect YAML settings")
    _common(config)
    sub = config.add_subparsers(dest="config_command", required=True)
    listing = sub.add_parser(
        "list", help="List merged non-secret settings and winning sources as key=value lines"
    )
    _common(listing)
    init = sub.add_parser("init", help="Create the packaged example in the user settings directory")
    _common(init)
    init.add_argument("path", nargs="?", type=Path)
    init.add_argument("--force", action="store_true", help="Replace edited settings with a backup")
    init.add_argument("--dry-run", action="store_true", help="Preview without creating files")
    auth = commands.add_parser("auth", help="Sign in through your browser without uploading PDFs")
    _common(auth)
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser(
        "login", help="Acquire an Azure token; does not test OCR access"
    )
    _common(login)
    login.add_argument("--reauthenticate", action="store_true", help="Open account selection again")
    login.add_argument(
        "--dry-run", action="store_true", help="No browser, auth cache, network or writes"
    )
    convert = commands.add_parser("convert", help="Upload one PDF and save its searchable copy")
    _ocr_options(convert)
    convert.add_argument("input", type=Path)
    convert.add_argument("--output", type=Path, help="Exact destination .pdf path")
    convert.add_argument("--json-output", type=Path, help="Exact destination .json path")
    batch = commands.add_parser(
        "run", help="Process enabled queues once; suitable for Task Scheduler"
    )
    _ocr_options(batch)
    batch.add_argument("--source", help="Process only this enabled source ID")
    verify = commands.add_parser(
        "verify", help="Inspect PDF page count and extractable text locally"
    )
    _common(verify)
    verify.add_argument("input", type=Path)
    verify.add_argument("--expected-pages", type=int, help="Fail on a different page count")
    verify.add_argument(
        "--require-text",
        action="store_true",
        help="Fail if no page contains extractable text (blank pages may be valid)",
    )
    return root


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in (
        "state_dir",
        "min_age_seconds",
    ):
        value = getattr(args, key, None)
        if value is not None:
            values[key] = value
    azure = {
        key: getattr(args, key)
        for key in ("endpoint", "auth_mode", "locale")
        if getattr(args, key, None) is not None
    }
    if azure:
        values["azure"] = azure
    return values


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "config" and args.config_command == "init":
        return init_config(args.path, force=args.force, dry_run=args.dry_run)
    if args.command == "verify":
        path = args.input.expanduser().resolve()
        info = inspect_pdf(path.read_bytes())
        if args.expected_pages is not None and info.pages != args.expected_pages:
            raise OcrError(f"Expected {args.expected_pages} pages, found {info.pages}")
        if args.require_text and not info.text_pages:
            raise OcrError("No extractable text found")
        return {"status": "verified", "path": str(path), **info.to_dict()}
    resolved = resolve_config(getattr(args, "config", None), _overrides(args))
    if args.command == "config":
        return {
            "effective_schema_version": SCHEMA_VERSION,
            "user_config_path": str(user_config_path()),
            "settings": resolved.values,
            "sources": resolved.sources,
            "winning_sources": resolved.winning_sources,
            "has_in_memory_migrations": False,
        }
    config = resolved.config
    if args.command == "auth":
        if config.azure.auth_mode != "browser":
            raise OcrError("auth login requires azure.auth_mode: browser")
        if not config.azure.endpoint:
            raise OcrError("Set azure.endpoint before auth login")
        result = {
            "status": "planned" if args.dry_run else "authenticated",
            "auth_mode": "browser",
            "dry_run": args.dry_run,
            "resource_access_checked": False,
        }
        if not args.dry_run:
            credential = BrowserCredential(config.azure)
            try:
                if args.reauthenticate:
                    credential.authenticate(SCOPE)
                credential.get_token(SCOPE)
            finally:
                credential.close()
        return result
    options = Options(
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        redo_ocr=args.redo_ocr,
        json=args.save_json,
        only_json=args.only_json,
        json_output=getattr(args, "json_output", None),
    )
    if args.command == "run":
        return run_sources(config, options, args.source)
    if args.json_output and not (args.save_json or args.only_json):
        raise OcrError("--json-output requires --save-json or --only-json")
    if args.output is None and not (args.only_json and args.json_output):
        raise OcrError("convert requires --output, or --json-output with --only-json")
    output = args.output or args.json_output.with_suffix(".pdf")
    return run([(args.input, output)], config, options)


def _error_result(exc: BaseException, args: argparse.Namespace | None) -> dict[str, Any]:
    if isinstance(exc, KeyboardInterrupt):
        result: dict[str, Any] = {"status": "interrupted"}
    else:
        message = (
            str(exc)
            if isinstance(exc, OcrError)
            else "File access failed; check paths/permissions"
            if isinstance(exc, OSError)
            else "Unexpected processing error; use --verbose for diagnostics"
        )
        result = {"status": "failed", "error": message}
    path = getattr(exc, "path", None) or getattr(exc, "filename", None)
    if args is not None:
        for field in ("input", "output", "json_output", "config"):
            value = getattr(args, field, None)
            if value is not None:
                result["config_path" if field == "config" else field] = str(
                    value.expanduser().absolute()
                )
        if args.command == "config" and args.config_command == "init":
            path = path or str((args.path or user_config_path()).expanduser().absolute())
        if getattr(args, "source", None) is not None:
            result["source_id"] = args.source
    path = path or result.get("input") or result.get("config_path")
    if path is not None:
        result["path"] = str(path)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Preserve the requested format even if argparse fails; -- terminates options.
    option_tokens = argv[: argv.index("--")] if "--" in argv else argv
    json_output = any(token.partition("=")[0] == "--json" for token in option_tokens)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logger = configure_logging(quiet=False, verbose=False)
    args = None
    try:
        args = parser().parse_args(argv)
        if getattr(args, "quiet", False) and getattr(args, "verbose", False):
            raise OcrError("--quiet and --verbose cannot be combined")
        logger = configure_logging(
            quiet=getattr(args, "quiet", False), verbose=getattr(args, "verbose", False)
        )
        logger.debug("Dispatching %s", args.command)
        result = execute(args)
        counts = result.get("counts", {})
        failed = sum(counts.get(key, 0) for key in ("failed", "needs_review"))
        emit(
            public_result(result, full_output=getattr(args, "full_output", False)),
            json_output=json_output,
        )
        if failed:
            logger.error("Finished with %s file(s) requiring attention", failed)
            return 1
        if args.command == "config" and args.config_command == "list":
            logger.info("Showing resolved configuration")
        else:
            log_success(logger, "Completed %s", args.command)
        return 0
    except KeyboardInterrupt as exc:
        logger.error("Interrupted; inspect outputs before rerunning")
        emit(_error_result(exc, args), json_output=json_output)
        return 130
    except (OcrError, OSError) as exc:
        result = _error_result(exc, args)
        logger.error("%s", result["error"])
        emit(result, json_output=json_output)
        return 2
    except Exception as exc:
        result = _error_result(exc, args)
        logger.error("%s", result["error"])
        logger.debug("Exception type: %s", type(exc).__name__)
        emit(result, json_output=json_output)
        return 1
