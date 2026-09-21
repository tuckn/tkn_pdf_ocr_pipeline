from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from . import __version__
from .auth import SCOPE, BrowserCredential
from .config import SCHEMA_VERSION, init_config, resolve_config, user_config_path
from .errors import OcrError
from .logging_config import configure_logging, log_success
from .pdf import inspect_pdf
from .pipeline import Options, discover, run


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise OcrError(message)


def _common(parser: argparse.ArgumentParser) -> None:
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
        help="Only ERROR/CRITICAL logs; JSON output is retained",
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
    parser.add_argument("--output-dir", help="Output folder; required if not configured")
    parser.add_argument("--state-dir", help="Persistent job/lock directory")
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
        help="Read/validate locally only; no auth, network, writes, or OCR",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace a conflicting OUTPUT with a backup; never overwrite the source",
    )
    parser.add_argument(
        "--retry-uncertain",
        action="store_true",
        help="Resubmit an uncertain/expired job; may incur another Azure charge",
    )


def parser() -> Parser:
    root = Parser(
        description="Create searchable PDFs with Azure prebuilt-read. Normal OCR uploads "
        "PDFs and writes results; --dry-run is local and read-only."
    )
    root.add_argument("--version", action="version", version=__version__)
    _common(root)
    commands = root.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Create or inspect YAML settings")
    _common(config)
    sub = config.add_subparsers(dest="config_command", required=True)
    show = sub.add_parser("show", help="Show merged non-secret settings and winning sources")
    _common(show)
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
    batch = commands.add_parser("run", help="Process a folder once; suitable for Task Scheduler")
    _ocr_options(batch)
    batch.add_argument("--input-dir", help="Input folder; required if not configured")
    batch.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Include subfolders and preserve relative paths (default false)",
    )
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
        "input_dir",
        "output_dir",
        "state_dir",
        "recursive",
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
    if args.command == "convert":
        if args.output:
            output = args.output
        elif config.output_dir:
            output = config.output_dir / args.input.name
        else:
            raise OcrError("Specify --output or configure output_dir")
        pairs = [(args.input, output)]
    else:
        pairs = discover(config)
    return run(
        pairs, config, Options(args.dry_run, args.overwrite, args.retry_uncertain, args.redo_ocr)
    )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logger = configure_logging(quiet=False, verbose=False)
    try:
        args = parser().parse_args(argv)
        if getattr(args, "quiet", False) and getattr(args, "verbose", False):
            raise OcrError("--quiet and --verbose cannot be combined")
        logger = configure_logging(
            quiet=getattr(args, "quiet", False), verbose=getattr(args, "verbose", False)
        )
        logger.debug("Dispatching %s", args.command)
        result = execute(args)
        failed = result.get("counts", {}).get("failed", 0)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if failed:
            logger.error("Finished with %s failed file(s)", failed)
            return 1
        log_success(logger, "Completed %s", args.command)
        return 0
    except KeyboardInterrupt:
        logger.error("Interrupted; rerun to resume accepted jobs")
        print(json.dumps({"status": "interrupted"}))
        return 130
    except (OcrError, OSError) as exc:
        message = (
            str(exc) if isinstance(exc, OcrError) else "File access failed; check paths/permissions"
        )
        logger.error("%s", message)
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False))
        return 2
