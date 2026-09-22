# Changelog

User-visible additions and changes are recorded here by application version.
Validation methods, measurements and limitations belong in
[OCR validation](docs/validation.md); current behavior and settings are described in
[processing](docs/reference/processing.md) and
[configuration](docs/reference/configuration.md).

This history was reconstructed from the available Git snapshots and existing
documentation. Dates below identify documented work or validation, not verified
release publication dates. The 0.1.0 and 0.3.0 snapshots and the 0.5.0 change are
available in Git; 0.2.0 is described in the existing validation record. No separate
0.4.0 snapshot or version-specific record was found, so changes between 0.3.0 and
0.5.0 are grouped under 0.5.0 rather than assigned speculatively to 0.4.0.

## 0.5.0 — 2026-09-22

### Added

- Multiple named input/output queues under `sources`, with `run --source ID` to
  select a queue and per-source enablement, recursion and filename suffix settings.
- An implicit `default` queue when no sources are configured.
- Optional `after_success: delete`: delete an input only after output verification
  and durable delivery recording; the default remains `keep`.
- Durable delivery history that prevents automatic redelivery after downstream
  output moves, including recovery of interrupted publication or input deletion.
- Byte-for-byte delivery of already-searchable PDFs without Azure OCR.
- `needs_review` and `cleanup_pending` reporting for uncertain results and unfinished
  cleanup, with inputs retained when safe completion cannot be established.

### Changed

- Configuration schema is now `3.0.0`. Folder settings belong exclusively under
  `sources.<id>`; top-level `input_dir`, `output_dir` and `recursive`, and their
  former folder CLI options, are not accepted.
- Folder runs validate non-overlapping input/output/state roots across all enabled
  sources. Dry-run includes planned input deletion without making changes.
- Exit code 1 also covers review items and pending input deletions.
- Single-file `convert` remains independent of queues and always retains its input.

### Upgrade notes

Back up edited configuration and move each input/output pair into a named source.
Set `schema_version: "3.0.0"` and place recursion under that source. Use `keep`
unless deletion is intended. Keep source IDs and the state directory stable to
retain delivery history. See the [configuration reference](docs/reference/configuration.md).

## 0.3.0 — 2026-09-22

### Changed

- Replaced rasterized OCR composition with original-image-preserving processing:
  upload selected pages, extract invisible text from the Azure result and compose
  it onto the original document without rendering or recompressing original images.
- Classify pages individually, preserving native-text and nonselected pages.
  Clone document structures including bookmarks, links, annotations and metadata.
- Use explicit `--redo-ocr` to replace supported invisible OCR text.
- Use configuration schema `2.0.0`; remove `existing_text` and `redo_dpi`.
- Change the processing revision so earlier rasterized outputs are not treated as
  completed results for the new method. Earlier output files and state remain intact.

### Upgrade notes

For this historical version, configuration required schema `2.0.0` and removal of
the old settings. Existing conflicting outputs required a new destination or
`--overwrite` with backup. For the current version, follow the 0.5.0 notes above.

See [0.3.0 live validation](docs/validation.md#live-ocr-validation-030-2026-09-22)
for image preservation, selected-text checks and their limits.

## 0.2.0 — 2026-09-22

### Added

- Browser sign-in with application-specific encrypted token-cache reuse.
- `auth login`, including explicit reauthentication and a read-only dry-run.

### Changed

- Browser authentication is the default; Azure CLI authentication is removed.
- `DefaultAzureCredential` excludes external CLI/developer credentials.

### Upgrade notes

Replace `azure.auth_mode: azure_cli` with `browser`. The removed value is rejected.
The [authentication validation record](docs/validation.md#browser-authentication-validation-020-2026-09-22)
distinguishes automated checks from later successful live sign-in.

## 0.1.0 — 2026-09-21

### Added

- Windows CLI for Azure Document Intelligence `prebuilt-read` searchable-PDF
  conversion, with Azure CLI / Entra authentication used in the recorded live run.
- Separate output files, local PDF/text verification, read-only preflight dry-runs
  and resumable processing with repeat-run detection.
- Explicit reprocessing of existing OCR via the historical `--existing-text redo`
  path, which rasterized pages before submission.

See [historical live OCR validation](docs/validation.md#historical-live-ocr-010-2026-09-21)
for the original sample evaluation. This date comes from that evaluation;
the available 0.1.0 Git snapshot was committed on 2026-09-22.
