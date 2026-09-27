# Processing and Azure client contract

## Processing steps

1. Resolve settings and destinations; input, output and operational directories must not overlap.
2. Enumerate `.pdf` inputs in deterministic order. Subdirectories require `recursive: true`.
   Directory links are not followed, and file links escaping the input root are rejected.
3. For each input, check all requested output filenames. If any exists, return `skipped`
   without authentication or Azure access unless `--overwrite` is specified.
4. Read a stable source snapshot and validate size, encryption, syntax and page count.
   Files younger than `min_age_seconds` are deferred. Classify pages and select scan pages;
   `--redo-ocr` also selects standard invisible OCR text. Named sources pass through
   searchable PDFs and hold uncertain or text-free inputs for review.
5. Dry-run reports planned work without authentication, network access or writes.
6. Build and inspect the selected-page upload. Azure analyzes the PDF and returns its
   analysis and, when requested, a searchable PDF. The CLI composes invisible text onto
   the original pages without rendering or recompressing source images.
7. Validate the result, recheck the input and outputs, and back up existing destinations
   when `--overwrite` is used. Publish complete files atomically and verify their hashes.
   For named sources with `after_success: delete`, recheck the input and outputs before
   deleting the input. Save a per-run JSON report.

Files in a batch are independent. Individual failures do not stop later inputs.
Configuration and folder-layout errors stop the command before file processing.

## Optional OCR JSON

`--json` saves a `.json` file with the searchable PDF. It uses the
same Azure analysis request and selected OCR pages. If the input has no eligible
OCR pages, a named source sends the full PDF for text analysis and copies the
existing searchable PDF. For a mixed PDF, `analyzed_pages` lists the
pages present in JSON; it may be fewer than `source_pages`.
`--only-json` sends the full input PDF and does not publish a PDF.

JSON includes `TextRecognition.responsev2.predictionOutput.fullText`,
page-level `fullPageText` and line `text`/`boundingBox`, plus
the original `azureAnalyzeResult`. The envelope resembles AI Builder
recognition content but is not an AI Builder API response. Text and line order
may differ; compare representative receipts before changing downstream parsing.

JSON is written atomically and verified by hash. A named source checks every
requested output before configured input deletion. An interrupted two-output run
may leave only one output; inspect the files before retrying.
In a named queue, JSON-only analysis with a page lacking recognized words is
held for review and the input remains in place.

## Azure HTTP contract

Public Azure resource origin: `https://<resource-name>.cognitiveservices.azure.com`.

- POST `/documentintelligence/documentModels/prebuilt-read:analyze?api-version=2024-11-30&output=pdf`
- JSON-only requests omit the PDF output parameter and use the analysis result GET only.
- Body: raw PDF bytes, `Content-Type: application/pdf`; optional `locale` query.
- Expected response: HTTP 202 with `Operation-Location`.
- GET the operation URL until `status=succeeded`.
- GET the same result path plus `/pdf`, preserving `api-version`; expect PDF bytes.

The operation URL must match the configured origin, model path and API version.
Redirects are disabled, so credentials are not forwarded to a different origin.
Credentials and raw Azure error bodies/OCR text are excluded from application logs.
Analysis JSON is used for page/word validation. When `--json` or
`--only-json` is requested, it is saved in the source-aligned OCR JSON
file. Without either option, the analysis remains transient.
`--only-json` omits the PDF output request and downloads no searchable PDF from Azure.

GET transport failures and HTTP 408/429/500/502/503/504 have bounded retries.
Exponential delay honors numeric Retry-After up to 60 seconds per wait, subject to the
overall collection deadline. POST errors/timeouts never cause an automatic retry.
HTTP-date Retry-After is not interpreted.

This implementation follows Microsoft's
[Read searchable-PDF contract](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/read?view=doc-intel-4.0.0#searchable-pdfs)
and [Entra authentication guidance](https://learn.microsoft.com/en-us/azure/cognitive-services/authentication/).
The infrastructure handoff is [provided separately](../azure-platform-request.md).

## Recovery and state

The configured `state_dir` holds operational files:

| Location | Purpose |
| --- | --- |
| `runs/<run-id>.json` | Completed invocation's file statuses and counts. These reports are history and do not affect later runs. |
| `locks/<output-id>.lock`, `locks/source-<input-id>.lock` | Coordinate concurrent CLI processes that share this directory. |

The CLI stages output beside its destination and publishes complete bytes atomically. A forcibly stopped process can leave a
`.name.*.tmp` file; inspect it after confirming no process is running.

If an output exists on the next run, the input is skipped. This includes a run that
saved output but stopped before deleting its input. Confirm the output and remove the
input manually when appropriate. If no output exists, the input can be sent to Azure
again. A submission accepted before interruption may therefore be charged again.
When PDF and JSON are requested together, interruption can leave only JSON or only
PDF; either existing requested output causes a skip until the files are reviewed.

`--overwrite` backs up each existing requested output as `<name>.bak-<unique-id>`
then replaces it. Backups are retained. The input PDF cannot be overwritten.
The CLI rechecks source and output hashes before publication and deletion; external
editors do not honor its locks, so these checks cannot provide a multi-file transaction.
A sudden stop can prevent the final run report from being saved.

## Verification

Automatic publication checks include:

- Strict PDF parse, nonempty page collection and no encryption.
- Downloaded page count matches selected pages and Azure analysis metadata; final page count matches the original.
- Displayed crop dimensions, accounting for rotation/UserUnit, match within 2 points.
- Each page for which Azure reports words has extractable PDF text, mapped to its original page number.
- Every drawn source image retains its encoded stream and decoding settings, including masks
  and color profiles; the per-page sequence of image hashes is identical before/after.
- Nonselected pages retain their extracted text; source page content and document structures
  are cloned, not rendered. PDFium rendering is a development check, not a runtime dependency.
- Source/output have not changed during OCR; a final output conflict check precedes writing.

`verify INPUT` performs local parsing and lists page count, pages with text and displayed
sizes, page classifications and image hashes. `--expected-pages` adds a count check. `--require-text` requires text on at least
one page, allowing blank pages elsewhere.
It does not compare against a manifest, validate every recognized word, or prove visual
equivalence. Use a PDF viewer and representative Japanese scans to evaluate OCR quality,
reading order, selection alignment and image appearance.

## Implementation boundaries

- `cli.py`: command parsing, UTF-8 JSON/stdout and log/stderr contract.
- `config.py`: independently validated configuration layers and packaged defaults.
- `pipeline.py`: page selection, locks, validation and publication.
- `azure.py`: Azure authentication and HTTP; mockable without live credentials.
- `pdf.py`: PDF classification, invisible-text removal/composition and image-preservation validation.
- `io_utils.py`: hashes, backups and atomic file writing.
- `logging_config.py`: log levels and Windows-compatible terminal colors.

Runtime dependency roles/licenses:
Azure Identity and HTTPX (MIT) handle authentication/network; PyYAML (MIT) handles YAML;
pypdf (BSD-3-Clause) handles PDF structure; filelock (Unlicense) supplies OS-backed locks.
Development-only Pillow (MIT-CMU) and pypdfium2 (Apache-2.0/BSD-3-Clause) support
rendering checks. PDFium includes bundled third-party notices.
Distribution must retain dependency notices. The application itself is MIT.

No cloud infrastructure, scheduler registration, secret provisioning, OCR engine training,
PDF/A conversion, signing, receipt-field extraction or final document filing is performed.
Input deletion is available only for explicitly configured named queues.
Folder processing is configured only through `sources`; see the [configuration reference](configuration.md).

## Page classification and limits

Classification examines drawing commands, not just successful Unicode extraction:

| Kind | Meaning | OCR selection |
| --- | --- | --- |
| `scan` | Drawn image(s), no text drawing | Always |
| `ocr_text` | Drawn image(s), only standard invisible text (rendering mode 3) | Only `--redo-ocr` |
| `native_text` | Any other text drawing | Never |
| `no_scan_image` | No drawn scan image | Never |
| `unsupported` | No visible text, but text/visibility semantics are unsafe to replace | Never |

A native-text page with an embedded photo is retained as a whole. Native and scanned
pages in the same PDF are supported independently. A skipped page remains in a composed
output. `convert` produces no copy for an entirely skipped file. Named
queues pass through already-searchable PDFs, preserving all bytes, and hold unsupported or
unsearchable inputs for review. JSON results retain page decisions on initial processing.

Nested/shared Form XObjects are traversed with recursion checks. Replaced forms are copied
so a shared form on a retained page is not mutated. Inline images retain encoded bytes.
Adobe `/Suspect` confidence markers are supported. Missing/Type3 fonts, tagged structure
on candidate pages, ActualText/other marked-content properties, and optional-content
visibility cannot be safely interpreted as standard OCR; affected candidates are retained.
Text hidden using transparency, white paint, clipping, or placement behind an image is
conservatively treated as native text, not removed. The CLI does not claim a general PDF
visibility detector. Corrupt/unbalanced drawing streams fail before upload.

Azure text must use standard invisible rendering mode; visible or unsupported text in its
response fails before publication. Original images remain in the output even when Azure
returns recompressed or resized images: those Azure images are not grafted. A dimension
mismatch beyond 2 points fails; smaller provider rounding differences are scaled only in
the text overlay. Original page boxes, rotation, UserUnit and images are unchanged.

## Original content and retention

The encoded source images and their interpretation are verified; source pages are never
rasterized or recompressed. Document cloning retains visible page content, bookmarks,
links, annotations and metadata. The output is a rewritten PDF, so it is not byte-identical
to the original file. Digital signatures, PDF/A and accessibility conformance are not
validated or guaranteed. General PDF feature/renderer equivalence is not exhaustively proven.

Separate, non-nested input/output roots prevent output reprocessing and source overwrites.
Single-file `convert` retains input. Named sources default to `keep`;
`after_success: delete` opts into verified input deletion. `--redo-ocr` authorizes replacement of standard invisible OCR text;
`--overwrite` separately authorizes replacing an existing output with a backup.


## Named queue delivery

Each named source processes its current inputs independently of earlier results.
The source ID selects the queue and labels the run report; changing it does not
require any migration. An existing requested output is skipped by default,
including when an earlier run created it. If a downstream process moves the output
while the input remains, a later run may produce it again.

An unsupported input or a selected OCR page without extractable text is
`needs_review`; the input is kept and no new output is published. Recognition
correctness is not judged. With `after_success: delete`, the CLI removes the
local input only after requested outputs have been saved and verified. It checks
source and output hashes immediately before deletion. If deletion fails, the
result is `needs_review`; later runs see the output and skip, so the user must
inspect and tidy the input manually.

Deletion is direct, without the recycle bin. Local verification does not prove
OneDrive upload. `run --dry-run` makes no writes and reports the planned
`source_action` and `azure_action`. Exit code 1 means `failed` or `needs_review`;
invalid arguments or configuration cause exit code 2.
