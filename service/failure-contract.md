# Sandbox service failure contract

The data model is an immutable encrypted plan, one source action and four durable step records. The unique business, entity, transaction and action identifies the accounting operation. A dedicated database session serializes dispatch for a target company and series. Attempt identifiers provide compare-and-set confirmation. The database never holds a transaction across HTTP.

Before implementation, the public CLI proof defines these failures.

| Failure | Required observable result |
| --- | --- |
| Wrong business, entity or Fortnox identity, denied grant, missing consent, missing active connection | Named gate and no accounting writes. |
| Missing calibrated VAT units or original-byte and association probe | Named gate before voucher creation. |
| Expired grant | Serialized renewal, identity verification and encrypted replacement, or gate with no write. |
| Stale fingerprint, changed source, receipt, mapping or company | No dispatch under stale approval. |
| Fractional minor units, large JSON numbers or decimal precision | Exact rejection or exact integer accounting. No float rounding. |
| Original signed URL redirects or escapes pinned origin | Download rejected without bearer header. |
| Intent committed before process death | Restart leaves the step uncertain and never reposts it. |
| Create committed with lost response, delayed commit or empty scan | Exact complete scan confirms one match. Empty scan remains uncertain. |
| Foreign marker, duplicate match, incomplete pagination or changed voucher | Investigation or gate. No subsequent write or acknowledgement. |
| Upload response lost | Voucher remains known. Upload remains uncertain. No guessed file identity or second upload. |
| Link response lost | Exact known file association readback can reconcile without another link. |
| Ack response lost | Only the frozen documented idempotency key can replay after full readback and source validation. |
| Competing workers | One voucher, upload, association and acknowledgement. |
| Database lock session lost during HTTP | No later dispatch. Durable intent remains for reconciliation. |
| Linked proof destination | Reject the link without modifying its target. Regular proof files are atomically replaced. |
| Help without configuration | Print commands without database or provider access. |
| Replayed complete operation | Same identity and remote counts. |

The independent expected posting is `e2e/expected-posting.json`, copied before implementation from the Python proof's literal oracle. Gross is SEK 1,250.00, VAT is SEK 250.00 and net is SEK 1,000.00. The repeatable proof calls only the public CLI against loopback HTTP and real PostgreSQL. It records redacted command output, SQL state, provider counts, exact rows and original hash. This proves the service's synthetic behavior. It does not prove provider interoperability.


The configured database and schema form the canonical journal. Workers using another database or schema are outside this connector's coordination guarantee. Sequence numbers and account balances are observations. The approved plan freezes stable series and account policy instead.
