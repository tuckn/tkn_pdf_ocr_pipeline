# CLI output contract (0.8.0)

This contract applies to `run`, `convert`, `verify`, `auth login`, `config init`,
and `config list`. Help/version keep their ordinary text. It changes presentation,
not Python API returns, OCR JSON, persisted reports, configuration schemas or HTTP.

## Selection and streams

Default stdout is one `key=value` entry per line. Nested mappings use dots, lists
use `[0]`, empty containers use `[]`/`{}`, booleans use `true`/`false`, and unset values
use `null`. Strings are unquoted; Windows paths keep single backslashes and are never
truncated. Control characters are escaped, including newlines, tabs and ANSI escapes.
`--json` replaces this with exactly one valid JSON document. Logs, warnings and
progress use stderr. TTY, pipes, redirection, `--quiet` and `--verbose` do not select
or suppress result output. Color is confined to stderr logs under the existing rules.

`--json` and `--full-output` work before/after commands, including between levels:

```powershell
tkn-pdf-ocr --json config list
tkn-pdf-ocr config --json list
tkn-pdf-ocr config list --json
$result = tkn-pdf-ocr run --dry-run --json --full-output | ConvertFrom-Json
$code = $LASTEXITCODE
$result.counts
```

Argument/config/file errors, per-file failures and interruption keep the selected
format where possible. Options after `--` are positional values. Argument parsing
errors still return a result; help/version keep standard argparse behavior.
Unexpected exceptions return a sanitized error without source text or credentials.

## Result fields

The text entries flatten the same result object emitted as JSON. Existing fields
are kept; optional fields depend on the operation. CLI results add a top-level
string `status` where the Python return value did not already have one.

| Operation | Status and fields |
| --- | --- |
| `run`, `convert` | `status`: `success` or `failed` (any failed/needs_review file); `schema_version`: report schema string `1.0.0`; `dry_run`: boolean; `counts`: object of integer created/replaced/skipped/planned/failed/needs_review counts; `sources`: counts by source ID; `files`: array of results. |
| File result | `status`: created/replaced/skipped/planned/failed/needs_review; `source`, `output`: path strings; optional `source_id`, `json_output`, hashes, page data, source action, `reason` or safe `error`. |
| `verify` | `status`: `verified`; `path`: path string; `pages`: integer; `text_pages`: array; other PDF inspection fields retain their existing types. |
| `auth login` | `status`: `planned`/`authenticated`; `auth_mode`: string; `dry_run`, `resource_access_checked`: booleans. No tokens/credentials. |
| `config init` | `status`: created/replaced/unchanged; `path`: string; optional `dry_run`: boolean and `backup`: path string. |
| `config list` | `status`: `success`; effective_schema_version/user_config_path: strings; settings/winning_sources: objects; sources: array of source metadata; has_in_memory_migrations: boolean. No secret values. |
| Top-level error | `status`: `failed`; `error`: safe string; known target in separate `path`, `input`, `output`, `json_output`, `config_path` fields; optional `source_id`. |
| Interrupted | `status`: `interrupted`; known target fields as above. Inspect completed outputs before restarting. |

For multiple files, default output omits only individual `skipped` records.
All failed/needs_review records and all changed/planned records remain visible.
`counts` and `sources` always count all records. `omitted_files` is an integer;
`skipped_reasons` maps reason strings to counts; `details_hint` describes how to see
full details. A single file is never omitted. `--full-output` includes every file in
both formats and does not alter processing. A completed non-dry-run's `run_report`
path points to the unchanged, full saved report. Dry-run never writes a report;
select `--full-output` at invocation time if all details are needed.

Exit codes remain: 0 success/no targets, 1 per-file failure/needs_review or unexpected
exception, 2 argument/configuration/file-access error, 130 interrupted. A `planned`
dry-run result is successful unless validation/file results require attention.

## OCR artifacts and result display

`--json` selects the stdout format independently of OCR artifacts and uploads.
`--save-json` saves OCR JSON alongside the searchable PDF; `--only-json` saves
only the OCR JSON artifact. `--json-output` specifies an exact artifact destination
for `convert`. `--save-json` and `--only-json` are mutually exclusive; either can
combine with stdout `--json`. Python `Options(json=True)` requests the OCR JSON
artifact.

```powershell
# PDF and OCR JSON artifacts, machine-readable execution result
tkn-pdf-ocr run --save-json --json
# PDF-only processing, machine-readable execution result
$result = tkn-pdf-ocr --json run | ConvertFrom-Json
# JSON artifact only, human-readable execution result
tkn-pdf-ocr convert input.pdf --only-json --json-output extracted.json
```
