# EML fixture corpus

Each file is a small deterministic input for parser tests. The files are
deliberately plain ASCII at the transport level so they can be reviewed and
checked into source control; message bodies and attachment names use the MIME
charset or transfer encoding being tested.

| File | Purpose | Expected assertion |
| --- | --- | --- |
| `01_iso2022_jp.eml` | ISO-2022-JP body | Japanese body decodes without replacement characters |
| `02_cp932_machine_chars.eml` | CP932 body with platform characters | `①`, `㈱`, and `髙` survive decoding |
| `03_euc_jp.eml` | EUC-JP body | EUC-JP text is decoded |
| `04_utf8.eml` | UTF-8 body | UTF-8 text is decoded |
| `05_charset_declared_shift_jis_actual_cp932.eml` | Declared `shift_jis`, CP932 bytes | CP932 fallback handles the machine characters |
| `06_charset_x_sjis.eml` | Non-standard `x-sjis` label | Label normalizes to CP932 |
| `07_charset_iso_2022_jp_ms.eml` | Non-standard ISO-2022-JP label | Label normalizes to `iso2022_jp_ext` |
| `08_charset_undeclared.eml` | No charset declaration | Charset detection or fallback returns readable text |
| `09_attachment_rfc2231_split.eml` | RFC 2231 `filename*0*` / `filename*1*` | Segments join into `日本語.txt` |
| `10_attachment_outlook_rfc2047.eml` | RFC 2047 value inside `filename=` | Filename decodes to `日本.txt` |
| `11_attachment_japanese.eml` | Japanese attachment name | Japanese name is retained before sanitization |
| `12_attachment_path_traversal.eml` | `../` and backslash filename | Sanitizer removes path components |
| `13_attachment_windows_reserved.eml` | `CON.txt` filename | Sanitizer marks the reserved Windows name |
| `14_attachment_executable.eml` | `.exe` attachment | Parser retains it and sanitizer raises the executable warning |
| `15_multipart_alternative.eml` | Plain and HTML alternatives | Plain text is preferred |
| `16_multipart_related_inline_image.eml` | HTML with `cid:` image | Body is extracted; inline image is not a regular attachment |
| `17_nested_multipart.eml` | Nested multipart body | Text is found through nested containers |
| `18_attachment_only.eml` | No body, regular attachment | Empty body and `has_attachment=True` |
| `19_message_id_missing.eml` | Missing Message-ID | Message parses with `message_id=None` |
| `20_message_id_duplicate.eml` | Duplicate Message-ID fixture | Two files can share the same header value |
| `21_date_invalid.eml` | Invalid Date header | Parser falls back to INTERNALDATE |
| `22_date_missing.eml` | Missing Date header | Parser falls back to INTERNALDATE |
| `23_date_future.eml` | Date ten years in the future | Parser falls back to INTERNALDATE |
| `24_malformed_boundary.eml` | Boundary does not match body | Parser returns `parse_error` without raising |
| `25_malformed_base64.eml` | Truncated/invalid base64 payload | Parser returns `parse_error` without raising |

The oversized-message case is intentionally generated in tests instead of
being committed. Use `tests.support.eml_builder.build_eml()` with an
`AttachmentSpec` containing a repeated byte string larger than the configured
limit. `write_corpus()` can materialize the generated corpus in a temporary
directory for parser tests.

The EML corpus is introduced in Phase 1. It will cover malformed MIME, legacy Japanese encodings, RFC2231 filenames, Outlook-specific attachment names, missing Message-ID headers, large attachments, and inline images.
