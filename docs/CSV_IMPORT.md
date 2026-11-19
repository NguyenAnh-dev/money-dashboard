# CSV import: preview and validation (Task 2b)

- **One read.** `read_rows` in `adapters/csv_source.py` parses the file once into `ParsedRow` tuples. The preview and the save use that list. `CsvSource(rows=...)` builds transactions from it, so the file is never reread after confirmation. IDs embed the user scope, which only exists after the database opens, so rows are turned into transactions at commit time.
- **Database stays closed** during validation, preview and cancellation. Only `commit` calls `store.open_db()`. A failure inside `commit` reports that the save is one transaction (none of the file's rows stored) and that the file, schema and local user may already exist.
- **Amount syntax** is checked against `_AMOUNT_SYNTAX` before any stripping: optional sign, optional single `USD` (leading, after the sign, or trailing), optional `$`, digits with optional `,`/space groups of three, optional decimals. `1USD2.34` and `-1,23` fail. A non-USD code or symbol on an otherwise valid amount is a currency error.
- **Strict CSV.** Superseded by the tokenizer described under "Re-review fixes"; `"BROKEN"x` and `BA"D` are both rejected, and line numbers are physical.
- **Terminal safety.** `safe()` in `import_csv.py` escapes control, format and line-separator characters (`\x1b`, `\n`, `‮`) in everything the CLI prints from untrusted input. Account identifiers containing them are rejected. Stored descriptions are unchanged.

## Re-review fixes

- **Own tokenizer.** `_records` in `adapters/csv_source.py` replaces `csv.reader`, because Python's strict mode accepts a quote inside an unquoted field (`BA"D`). It follows RFC 4180: `""` escapes a quote, a quote anywhere else in an unquoted field is an error, and nothing may follow a closing quote except a delimiter or line break. It counts physical lines itself (`\r\n`, `\r` and `\n` each count once, including inside quoted fields). Errors give the record's starting line, or a range for an unterminated quoted field.
- **Decoding.** The file is read as bytes and decoded as UTF-8 with an optional BOM. A decode failure reports the physical line of the bad byte and the reason.
- **Headers.** Duplicate `Date`, `Description`, `Amount` or `Currency` headers are rejected. If a `Currency` column exists, a non-empty value other than USD (any case) is an error naming the row. A blank value or no column means USD.
- **Range.** `clean_amount` checks the signed cents (after `SIGN`) fit a signed 64-bit integer, which is SQLite's limit, so overflow fails at parse time with its row.
- **Dollar sign.** `$` may sit before or after the sign, but not both.
- **Output encoding.** `main` sets stdout and stderr to `backslashreplace`, so characters the stream cannot encode print as `\uXXXX` instead of raising. Control characters are still escaped by `safe()`. Stored descriptions are untouched.

## Third review fixes

- **Fractional cents.** `to_cents` in `core/money.py` multiplied a `Decimal` under the default 28-digit context, which rounded `1.000000000000000000000000000001` to exactly 1 before the precision check. It now scales under a context wide enough for the input, so extra non-zero digits are rejected and trailing zeros stay valid.
- **Currency header.** The `Currency` column is found by comparing header names with surrounding spaces removed and case folded. Two such headers are a duplicate-column error, and a non-USD value under either variant is a row-specific error.
- **BOM offsets.** `_decode` removes the BOM bytes first and then decodes and counts lines against the same buffer, so the reported physical line is correct.
- **Amount grammar.** The cell is trimmed once with `str.strip()`. Every optional whitespace run in `_AMOUNT_SYNTAX` now trails a mandatory token (`USD`, `$`, or the sign), so no two runs can match the same spaces and rejection stays linear.

## Fourth review fix

- **Underflow.** `to_cents` no longer multiplies a `Decimal` at all. It reads the digit tuple and exponent, drops trailing zero digits, and rejects the value if the remaining exponent is finer than the currency's minor unit. Otherwise the cents are the integer coefficient times a power of ten. Nothing is rounded or can underflow, so `"0." + "0" * 1_000_100 + "1"` is rejected, while exact zero, trailing zeros, and the SQLite boundary amounts convert as before. Non-finite `Decimal` inputs are a `ValueError`.

## Fifth review fix

- **Integer string limit.** `to_cents` used to rebuild the coefficient as a string for `int()`, which fails past Python's 4,300-digit conversion limit. It now builds a `Decimal` from the validated coefficient tuple and a non-negative exponent and calls `int()` on that, which is exact and involves no string or context arithmetic. The CSV importer still rejects such amounts at the range check, with the row number, before opening the database.

## Task 2c: new and previously stored rows

- **Counting.** `repository.save` asks `count_stored` how many of the incoming transaction ids already exist, before the upsert and inside the same transaction. `SaveResult.new` and `SaveResult.existing` hold the split (new = distinct ids minus existing). Both default to 0, so existing positional constructions still work. IDs and occurrence handling in `adapters/fingerprint.py` are unchanged.
- **Output.** `import_csv.main` prints `new` and `stored` lines after the shared sync report. The existing `wrote N transactions` line still counts every row upserted. `sync.py` output is unchanged.
- **Per account.** IDs embed the connection, which is scoped per user and account identifier, so an id match is a match for that account. The same rows imported under another account are new.
- **Ambiguity.** Without bank transaction IDs, "previously stored" means "same date, description, amount and occurrence number". Two identical same-day charges are told apart only by their order within one file. If a later partial export contains only one of them, it matches the first stored copy and the second stays stored but unmentioned. If an export lists a same-day duplicate that an earlier export held only once, the extra copy counts as new, which may be a real second charge or an overlap artifact. Neither case can be resolved from the CSV alone.
