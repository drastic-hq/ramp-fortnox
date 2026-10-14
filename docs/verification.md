# Accounting sync verification

The verifier compares an explicit frozen source population with scoped accounting observations. It reports comparison results and their coverage. It does not repair operations, create vouchers, or report success to Ramp.

## Source population

The denominator is the selected frozen plans present in one atomic local SQLite read snapshot. Selection binds Ramp business and entity to the configured Fortnox company, financial year, and voucher series. Never-dispatched plans remain expectations. Selected invalid records remain visible. Excluded records retain their selection reason.

This population excludes unplanned transactions. It does not enumerate all Ramp sync-ready transactions, refresh current eligibility, or verify Ramp acknowledgements. A plan fingerprint identifies the frozen input. It does not establish independent accounting correctness.

## Observation boundary

The collector checks the configured company identity, traverses every declared voucher page, and reads each full voucher. It collects exact attachment associations and original file bytes for candidate vouchers. It records requested and received pages, failed reads, source and observation hashes, and the acquisition interval.

A sequential read is not an atomic remote snapshot. An observation made during this interval says nothing about an effect that commits later. The fixture attachment protocol declares completeness explicitly. Its proof routes are not verified Fortnox APIs.

The report pins Fortnox, Sweden, SEK, the single-purchase P0 policy, and ordered posting rows. Money comparisons use exact integer ore. It compares supplied accounts, dates, and amounts without deriving VAT, account mappings, or country-specific tax treatment. It does not reconcile aggregate account balances.

## Report classifications

| Classification | Meaning within the declared coverage |
| --- | --- |
| Confirmed | Complete usable enumeration establishes one observed voucher, with matching full posting and exactly bound original receipt bytes. |
| Missing | Complete usable enumeration did not observe the expected operation marker during this acquisition. This is not proof of failed execution. |
| Duplicate | At least two distinct observed voucher identities carry the expected marker. Partial coverage permits only an observed lower bound. |
| Mismatched | Positive evidence differs from the frozen posting or required original-evidence association. The report preserves the differences. |
| Unknown | Missing source, scope, uniqueness, detail, or receipt evidence prevents a stronger conclusion. Observed records outside the source population are separately identified. |

Repeated pagination references to one identity do not prove duplicate vouchers. Failed pages or unresolved details can hide a second match, so they prevent Confirmed and Missing in the affected scope. Qualified positive duplicate or mismatch evidence can remain visible.

A missing voucher has no observed receipt to inspect. The receipt check remains Unknown without overriding a properly established voucher-level Missing. A failed receipt download remains Unknown. A complete attachment inventory that lacks the required association establishes a receipt mismatch.

Every result keeps execution outcome unproven and retry authorization false. Report classifications never trigger a write or change the journal's recovery state.

## Read-only guarantee and replay evidence

The CLI branches before constructing the writable journal, connector, or dispatch owner. Source capture uses SQLite read-only mode and connection-local query-only enforcement. Target acquisition permits GET requests to literal loopback HTTP addresses and rejects redirects. Business records remain unchanged. Existing artifact symlinks and nonregular destinations are refused before writing. Regular artifacts are atomically replaced so hard links cannot overwrite ledger bytes. Ordinary SQLite auxiliary-file behavior is not an accounting write guarantee.

The output includes JSON and readable reports plus frozen source and observed inputs. Captured request outcomes and original-byte evidence permit offline replay without the database or HTTP service. Internal hashes detect inconsistent captured inputs. They do not authenticate the truth of a remote system.

The proof uses real CLI subprocesses and durable local HTTP/SQLite services. Expected classifications and postings are literal fixture values written independently of comparison code. Method audits, unchanged source records, late commitment, partial coverage, and offline replay test the boundary. See the [failure contract](sync-verification-failure-modes.md) and [literal oracle](../fixtures/sync-verification-oracle.json).

Live-provider verification remains deferred. The [sandbox gates](sandbox-gates.md) define the external proof. The [Nordic follow-up](nordic-proof.md) separates candidate reusable concepts from country and provider contracts that still need their own proof.
