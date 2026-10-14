# P0 failure contract

Written before implementation. The only supported result is one positive domestic SEK purchase, one balanced voucher, its original receipt attached, and one idempotent Ramp acknowledgement. Synthetic verification does not establish sandbox interoperability.

| Failure | Expected observable result | Injection |
| --- | --- | --- |
| Wrong Ramp entity or Fortnox company | Refused before any write | Change identity or reconnect identity |
| Non-SEK settlement, merchant or receipt | Refused | Change each currency independently |
| Foreign merchant | Refused | Change merchant country |
| Non-positive settlement | Refused | Zero or negative amount |
| Missing tax, mixed taxes, wrong rate or inconsistent amount | Refused | Change explicit VAT evidence |
| Approval requirements unmet | Refused | Clear `all_requirements_met_and_approved` |
| Missing receipt, transaction link mismatch or hash mismatch | Refused | Remove or modify original bytes/link |
| Uncalibrated tax units or receipt totals disagree | Refused | Remove calibration or alter receipt evidence |
| Missing liability mapping, inactive account, wrong year or series | Refused | Change configured mapping/metadata |
| Transaction, evidence, mapping, entities or receipt changed after planning | Stale refusal before writes | Mutate each frozen input |
| Duplicate fetch or replay | Same operation and exactly one voucher | Repeated plan/apply subprocesses |
| Two competing CLI processes | One dispatcher; exactly one voucher | Concurrent apply subprocesses |
| CLI crash after durable intent | Restart marks step uncertain, never unsent | Kill process at persisted intent |
| Create response lost after commit | Uncertain then confirmed only after whole-series exact match | HTTP disconnect after SQLite commit |
| Create response lost without commit | Remains uncertain; no automatic create retry | Disconnect without commit |
| Matching operation key but different payload | Investigate; no later writes | Seed differing full voucher |
| Duplicate key | Investigate | Seed two matches |
| Foreign key or unknown voucher in owned series | Investigate | Seed unrelated voucher |
| Matching voucher on a later page | Confirm after all pages and full detail reads | Page size one |
| Definitive upload or connection rejection | Repair only failed step; never recreate voucher | One HTTP 422 |
| Upload or connection response lost | Uncertain then reconcile remote file/connection before repair | Disconnect after commit |
| Ramp report rejection or response lost | Retry same stable idempotency key after attachment confirmation | Reject or disconnect |
| Incomplete attachment | Never acknowledge Ramp | Observe remote sync report count |
| Non-loopback URL or live mode | Refused; no live provider writes exist | Configure external endpoint |

The repeatable proof is `python scripts/verify.py`. It calls the public CLI in subprocesses, exercises actual loopback HTTP and two durable SQLite stores, and writes JSON plus a readable transcript under ignored `artifacts/`.
