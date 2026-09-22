# OCR validation

This document records validation methods, results and limits for the versions
explicitly named below. User-visible additions and changes are maintained in the
[Changelog](../CHANGELOG.md).

The latest live OCR evaluation recorded here is for **0.3.0**, not the current
**0.5.0** application. These results do not establish live validation of the later
named-queue delivery and input-deletion features. Historical commands, settings and
test counts describe their original runs, not the current interface or test suite.

## Live OCR validation (0.3.0, 2026-09-22)

The image-preserving upload and composition path completed new Azure OCR for **20
Japanese receipt PDFs / 21 pages** using `prebuilt-read`, API `2024-11-30`, and browser
Entra authentication. A later process reused the application-specific encrypted cache.
Azure CLI was not used. Old invisible Adobe text was removed before each upload.

- All drawn original images retained identical encoded bytes and decoding settings.
- All 21 output pages were pixel-identical to the originals when rendered at 144 DPI.
- The same 100 selected-item checks used below matched **100/100** in these new outputs,
  compared with **72/100** in the archived Adobe text. Each of issuer, date, total,
  phone and phrase matched 20/20. This is not a full-text accuracy or CER/WER claim.
- Output totaled **4,748,184 bytes**, versus **4,271,239** source
  bytes: **1.112 times** in aggregate, median per-file ratio **1.186 times**.
- All 20 repeat runs returned `unchanged` with provider construction prohibited.
- All **2,768** original files retained their SHA-256, byte count and modification time.
- **105 automated tests**, Ruff and strict mypy passed. Wheel/sdist packaging and the
  installed **0.3.0** CLI's help, schema-2 configuration and PDF verification were checked.

The sample and selected-item limitations in the historical method below also apply.
This run verifies browser token acquisition/cache reuse and OCR resource access, but
not indefinitely unattended authentication or future account-policy behavior.

## Image preservation (0.3.0, 2026-09-22)

The new implementation was tested by recomposing the same 20 originals / 21 pages with
the **previously downloaded** Azure results. This check made no new Azure requests.
All original encoded images and their decoding settings matched exactly. All 21 pages
rendered at 144 DPI were pixel-identical to their originals. Extracted Azure text matched
after removing only leading/trailing whitespace; the old Adobe text was replaced.

Total output size was **4,573,408 bytes**, compared with 4,271,239 source bytes: **1.071
times** in aggregate, median per-file ratio **1.119 times**. All 2,768 source files retained
their SHA-256, byte count and modification time. Representative output previews were
visually inspected. Private outputs/reports are excluded from version control.

This establishes composition/image preservation on these samples, not recognition accuracy
for the new upload path. The earlier 100 selected-item checks below used rasterized inputs.
The 0.3.0 live run above separately evaluates recognition with original-image inputs.

Automated tests cover scan/OCR/native/blank classification, mixed-page selection, explicit
replacement, old-text removal, retained bookmarks/links/metadata, repeated redo, shared
forms, rotations 0/90/180/270, CropBox and UserUnit, provider text rejection, no-write
dry runs, output backups, browser authentication and resumable processing.

## Historical live OCR (0.1.0, 2026-09-21)

On 2026-09-21, version 0.1.0 completed live Azure processing for **20 Japanese receipt
PDFs, 21 pages**, using Azure CLI / Entra authentication, `prebuilt-read`, API
`2024-11-30`, and an S0 resource in Japan East. All outputs retained their page count
and passed local PDF/text verification.

For these samples, selected search and text-extraction checks supported using Azure
Read as an alternative to the archived Adobe OCR results. This is a limited evaluation,
not a guarantee for all documents or a comparison against the current Adobe service.

### Method and scope

The source collection contained 2,768 PDFs. Twenty were selected across available years
from 2012 to 2026, with deterministic shuffling within years and a phone-number-based
rule to reduce repeated issuers. Encrypted documents and documents longer than three
pages were excluded from selection. This was a diversity sample, not a simple random
sample of the collection.

The supplied PDFs already contained Adobe OCR text. `--existing-text redo` rasterized
all pages at 300 DPI before sending them to Azure, so the old text layer could not be
reused in the result. The locale was left unset for automatic detection. The pre-OCR
scans and the historical Adobe model/settings were unavailable.

Five fields or phrases per document were transcribed from rendered source images:
issuer, date, total amount, phone number and an item/other phrase. Presence in extracted
text was checked after Unicode, case and whitespace normalization, with common numeric
format variations allowed. Numeric checks preserved line boundaries to avoid joining
unrelated table values. Both versions used the same extraction and matching rules.

| Selected item | Archived Adobe output | Azure output |
| --- | ---: | ---: |
| Issuer | 11/20 | 20/20 |
| Date | 16/20 | 20/20 |
| Total amount | 19/20 | 20/20 |
| Phone number | 15/20 | 20/20 |
| Item or other phrase | 11/20 | 20/20 |
| Total | 72/100 | 100/100 |

These are **selected-item presence checks**, not a 100% OCR accuracy claim. The check
was not blind or preregistered. No full-text reference transcription, CER/WER,
confidence interval, or semantic label/value extraction was evaluated. Small print,
logos and decorative text still contained recognition errors. Some labels and amounts
appeared in separate text blocks.

### Appearance and size

All 21 source and output pages were rendered and visually inspected in contact sheets.
No missing pages, visible clipping or large displacement was found. Pixel dimensions
matched on every page when rendered at 144 DPI. The median RGB mean absolute difference
was 2.07 on a 0-255 scale (maximum 4.73). The median fraction of pixels with any channel
difference greater than 8 was 7.73%. These measures include resampling differences;
they are not OCR accuracy or perceptual quality scores.

| Size measure | Result |
| --- | ---: |
| Total archived source size | 4,271,239 bytes (4.07 MiB) |
| Total Azure output size | 26,490,765 bytes (25.26 MiB) |
| Aggregate size ratio | 6.20 times |
| Median per-file size ratio | 9.49 times |
| Per-file size ratio range | 2.64-15.20 times |

The historical 0.1.0 redo path rendered RGB images and stored them with lossless compression. This can substantially
increase size compared with previously compressed scans and fixes visual detail at the
selected DPI. The normal image-only path sends original PDF bytes without local
rasterization; its live file-size behavior was not evaluated by this receipt comparison.

### Repeatability and source protection

All 20 preflight dry runs succeeded. Twenty live analyses processed 21 pages. All 20
outputs passed the CLI's page-count and extractable-text verification. Repeating the CLI
execution path with Azure client construction explicitly blocked returned `unchanged`
for all 20 files, with unchanged output hashes and job manifests.

Before/after SHA-256 and modification-time checks of all 2,768 source PDFs found no
content changes, timestamp changes, additions or removals. Source documents were not
edited. Detailed comparisons, extracted text, source paths, previews, operational state,
and real endpoint settings remain in private, Git-ignored local storage. They are not
shipped as test fixtures or package resources.

This evaluation does not cover unattended authentication, Task Scheduler operation,
Linux, current Adobe reprocessing, full-text accuracy, PDF/A, accessibility, signatures,
or metadata preservation. See the [processing contract](reference/processing.md) and
[configuration](reference/configuration.md) for the supported behavior.

## Browser authentication validation (0.2.0, 2026-09-22)

This check evaluated the browser authentication introduced in 0.2.0. The
2026-09-21 OCR results above are historical tests of 0.1.0; they are not evidence
of successful live browser authentication in 0.2.0.

All 88 automated tests passed, including initial browser sign-in, account-record and
encrypted-cache reuse, reauthentication, invalid-record recovery, failure before PDF
submission, dry-run isolation and old-job compatibility. Ruff and strict mypy passed;
the wheel/sdist were built and the installed CLI reported 0.2.0. Installed `auth login
--dry-run` succeeded without authentication or PDF submission.

The 20 earlier live jobs were also checked with the 0.2.0 browser configuration and
Azure client construction prohibited: all returned `unchanged`, preserving source and
output hashes. No additional OCR requests were sent.

At the time of the 0.2.0 check, a browser sign-in attempt did not complete within its
300-second timeout. The later 0.3.0 live run above completed sign-in, cross-process cache
reuse and real OCR; this resolves that earlier authentication verification gap.
