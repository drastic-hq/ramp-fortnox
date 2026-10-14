# Read-only sync verification failure contract

The denominator is the selected local frozen-plan snapshot. It includes never-dispatched plans. It does not enumerate Ramp eligibility or acknowledgements. Selection binds Ramp business/entity and Fortnox company/year/series. Invalid selected plans and missing requested IDs stay visible as Unknown. Excluded rows retain their exclusion reason.

| Observation | Required outcome |
| --- | --- |
| One distinct full voucher, matching frozen posting and exactly bound original bytes, complete list/detail coverage | Confirmed |
| Complete usable enumeration and detail reads, no matching marker | Missing, described as not observed during this scan |
| Two distinct voucher identities with the marker | Duplicate, even with partial coverage, count is a lower bound |
| Positive posting, date, account, amount or attachment mismatch | Mismatched, preserve differences even with partial coverage |
| Late page/detail failure, changed page totals, wrong page identity, repeated voucher identity or conflicting details | Unknown for otherwise Confirmed/Missing rows throughout the scope |
| Company binding failure | Unknown throughout the scope, do not collect vouchers |
| Unreadable receipt bytes or incomplete connection inventory | Unknown receipt check, never receipt absence |
| Complete connection inventory with no connection | Mismatched summary and Missing receipt check |
| Matching hash metadata without readable original bytes | Unknown |
| Invalid fingerprint, operation identity, receipt digest, or malformed stored plan | Unknown source row |
| Foreign marker or observed voucher outside selected population | Separate Unknown observed-only finding |
| Empty selected population | Zero local frozen plans, no all-Ramp success claim |
| Late commit after a Missing scan | A later scan may Confirm; the first scan never authorizes retry |
| Captured source or raw request digest is modified | Offline replay refuses the input |

Every finding carries `retry_authorized=false` and `execution_outcome=unknown`. Positive mismatches and duplicate lower bounds do not authorize writes. Comparisons preserve P0 ordered rows and exact integer ore. No VAT derivation occurs.

The CLI branches before Journal or Connector construction. It opens SQLite with `mode=ro`, enables connection-local `query_only`, and materializes plans and step references within one read transaction. HTTP collection permits only GET through a proxy-free, no-redirect literal-loopback transport. Captured inputs, report JSON and text live only in the output directory. No source refresh, journal calls, repair, claim, acknowledgement, or POST is allowed.

The synthetic attachment protocol explicitly declares inventory completeness. Live attachment coverage is unsupported. A sequential scan records its acquisition interval and offers no atomic provider snapshot. SQLite business records remain unchanged. Stopped-writer proofs compare main database and existing WAL bytes; shared-memory auxiliary file immutability is not claimed.

Repeatable proof uses public CLI subprocesses, durable HTTP/SQLite fixtures, independent literal expectations, GET method audit, unchanged ledger records and bytes, and offline replay without the service or ledger.
