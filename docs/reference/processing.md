# Processing and Azure client contract

## Processing steps

1. Resolve settings and explicit destinations. Folder input/output/state roots cannot overlap.
2. Enumerate `.pdf` filenames case-insensitively, sorting deterministically.
   Subdirectories require `sources.<id>.recursive: true`; directory symlinks/junctions are not followed.
   File links escaping the input root are rejected.
3. Read a stable source snapshot. Validate file size, encryption, syntax, page count,
   displayed page dimensions and extractable text. Sources younger than
   `min_age_seconds` are deferred. Compare size/mtime around reads, then SHA-256
   before upload and publication. This reduces copying races, but is not a writer lock.
4. Classify each page; select image-only scans and, only with `--redo-ocr`, scans with
   standard invisible OCR text. Keep native, already-OCRed and unsupported pages as applicable.
   For single-file `convert`, no eligible pages means a skipped file. Named queues copy
   already-searchable PDFs without Azure and hold uncertain or text-free inputs for review.
   Match durable handoff history before evaluating a new OCR job.
5. Validate conflicts. Dry-run returns without authentication, API calls, folders, state,
   locks, backups or reports. It lists `page_kinds` and 1-based `ocr_pages`; it cannot
   validate cloud access, the eventual upload size or recognition quality.
6. Build a PDF containing only selected pages, removing old invisible text and excluding
   annotations from the upload. Verify original encoded image data before authenticating.
7. Persist submission intent, POST once, then persist the accepted operation URL.
8. Poll and download the searchable PDF. Validate its count against selected pages and
   Azure analysis metadata. Extract invisible text and its font/resources into Form XObjects;
   exclude Azure image drawing and other visible painting. Clone the original document,
   replace old invisible text on selected pages, and graft the new text onto those pages.
   Transform OCR coordinates to the original crop/rotation/UserUnit; keep all other pages.
9. Validate the composed PDF, including encoded image hashes, then recheck source/output.
   Record the output hash before atomic publication. Back up a replaced output, mark the
   job complete after publication and a read-back hash check. Named queues persist a
   handoff intent before publication, record verification, then apply keep/delete policy.
   Recheck the original hash immediately before deletion and record its completion.
   Save a per-run JSON report.

All files in a batch are independent. File failures are collected; later files continue.
Structural setup errors (such as overlapping roots) stop the command.
Jobs that cannot be accepted or completed are never represented as successful outputs.

## Azure HTTP contract

Public Azure resource origin: `https://<resource-name>.cognitiveservices.azure.com`.

- POST `/documentintelligence/documentModels/prebuilt-read:analyze?api-version=2024-11-30&output=pdf`
- Body: raw PDF bytes, `Content-Type: application/pdf`; optional `locale` query.
- Expected response: HTTP 202 with `Operation-Location`.
- GET the operation URL until `status=succeeded`.
- GET the same result path plus `/pdf`, preserving `api-version`; expect PDF bytes.

The operation URL must match the configured origin, model path and API version.
Redirects are disabled, so credentials are not forwarded to a different origin.
Credentials and raw Azure error bodies/OCR text are excluded from application logs/state.
Analysis JSON is used transiently for page/word validation, not persisted.

GET transport failures and HTTP 408/429/500/502/503/504 have bounded retries.
Exponential delay honors numeric Retry-After up to 60 seconds per wait, subject to the
overall collection deadline. POST errors/timeouts never cause an automatic retry.
HTTP-date Retry-After is not interpreted.

This implementation follows Microsoft's
[Read searchable-PDF contract](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/read?view=doc-intel-4.0.0#searchable-pdfs)
and [Entra authentication guidance](https://learn.microsoft.com/en-us/azure/cognitive-services/authentication/).
The infrastructure handoff is [provided separately](../azure-platform-request.md).

## Recovery and state

State lives under the configured directory:

| Location | Purpose | If missing |
| --- | --- | --- |
| `jobs/<job-id>.json` (`convert`), `jobs/<source-id>/<job-id>.json` (queues) | Source/settings identity, Azure operation URL, timestamps, stage and hashes | Cannot resume or establish identical output safely. |
| `handoffs/<source-id>/<delivery-id>.json` | Input/output hashes, verification and publication/deletion progress | Completed delivery can no longer prevent regeneration after a downstream move. |
| `runs/<run-id>.json` | Completed invocation's file statuses/counts, including failures | Past summary unavailable; jobs/outputs remain. |
| `locks/<output-id>.lock`, `locks/source-<input-id>.lock` | OS-backed output and input locks shared across CLI invocations | Recreated; file presence alone does not mean locked. |

No cache or persistent staging PDFs are used.
Staging files are beside the final file for atomic publication and are removed during
normal error handling. A forcibly killed process can leave a `.<name>.*.tmp` file.
After confirming no process is running, remove only that abandoned temporary file.

| Saved stage | Next run |
| --- | --- |
| `submitting` | Acceptance may be unknown. Stop until the user explicitly chooses `--retry-uncertain`. |
| `submitted` | Poll/download the same operation; do not POST again. |
| `ready` | If output hash already matches, return unchanged; otherwise download/validate again. |
| `completed` | `convert` jobs: matching output hash returns unchanged; missing output can be downloaded from the saved operation while available. Named queues consult their separate handoff record first. |
| `review` | A selected OCR page has no extractable text. Keep input, publish no output, and do not access Azure on an ordinary retry. |

Azure result retention is limited. If the operation is expired (HTTP 404), choose
`--retry-uncertain` after checking the saved state; this may incur another analysis charge.
The same option deliberately resubmits an existing incomplete job, including one still
running. It does not force reprocessing an unchanged completed output.
Do not delete the state as a routine retry mechanism.

If output exists but is different or lacks matching state, use a new destination or
explicitly choose `--overwrite`. A backup is created as `<output>.bak-<unique-id>`.
Backups are retained; there is no automatic cleanup. An input PDF cannot be overwritten.

Normal errors leave recoverable state; abrupt termination may prevent a final run report.
A crash after Azure accepts a POST but before the operation URL is persisted is inherently
uncertain. The durable `submitting` marker prevents silent duplicate submissions.
A crash after output publication but before completion is recognized through the `ready`
output hash.

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
- `pipeline.py`: page selection, locks, provenance, resumption/publication.
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

A handoff is identified by source ID, canonical input path and input SHA-256, independently
of the OCR fingerprint or output location. Keep IDs and `state_dir` stable. The history is
retained after output moves and input deletion. Restoring identical input at the same path
returns `unchanged` without recreating output or deleting the restored input. This does not
attempt global content deduplication across different input paths or sources.

| Handoff stage | Retry behavior |
| --- | --- |
| `publishing` | Intent was saved before final publication. Matching saved output permits local completion; missing/changed output requires review. |
| `published` | Output was saved and verified. Recheck input/output hashes and complete retention policy. |
| `cleanup_pending` | Retry only input deletion, with both hashes rechecked; no OCR. Missing/changed output requires review. |
| `completed` | Delivery remains complete even after a downstream move. No regeneration based on output absence. |

If the user changes `keep` to `delete`, a previously kept input is removed only when its
recorded output can still be verified. A completed deletion is not repeated on a restored input.
An interruption after unlink but before saving completion is reconciled on the next selected
queue run: a saved cleanup intent plus absence of input completes the record. Recovery records
that absence was observed; it cannot identify whether this CLI or another application removed it.

Any selected OCR page without extractable result text (including a blank scanned page) is
`needs_review`, even if another page contains text. Unsupported input pages and text drawing
without extractable text also require review. Inputs are retained and new output is not
published. Recognition correctness is not judged. For a reviewed OCR-result issue, explicitly
choosing `--retry-uncertain` resubmits the saved OCR job and may incur another charge; the
ordinary retry returns the existing review result without Azure access.

Delivery intent is persisted before output becomes visible, so a downstream consumer moving
it immediately cannot cause silent automatic republication. A crash before publication and a
move after publication can be indistinguishable when output is absent. Both conservatively
require review. Preserve the record and input, inspect downstream files, and restore the exact
recorded output when available before retrying. `--overwrite`, `--redo-ocr` and
`--retry-uncertain` never override a handoff stop. If there is no recoverable output, a deliberate
manual recovery must reconcile the downstream result and the saved record; ordinary runs do
not infer that a new delivery is wanted. Process a retained file separately with `convert` if a
new output is required while investigating, without altering its queue history.

Deletion removes the local file directly, not through the recycle bin. Checks establish local
file persistence, not successful OneDrive upload. Source/output locks coordinate this CLI's
processes sharing a state directory; external editors/consumers do not honor those locks.
Final hash checks detect intervening changes, but the multiple files and OneDrive are not a
single transaction. Downstream scheduling should allow the queue run to finish first.

`run --dry-run` performs no writes or deletion, including no recovery-record updates. Results
include planned `source_action` (`keep`/`delete`) and `azure_action` (`submit`/`resume`/`none`).
`files` carries `source_id`; `sources` aggregates statuses for sources represented in results.
`needs_review`, `cleanup_pending`, or `failed` causes exit code 1. Invalid routing/arguments
cause exit code 2. Files in all selected sources continue after individual file errors.
