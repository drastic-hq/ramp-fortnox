# Prove which Nordic boundaries generalize

This is a follow-up proposal from the project owner. It starts after the real Fortnox P0 path is complete. No Denmark or Norway adapter, account, posting, or provider test has been executed. The current connector and verifier remain Fortnox, Sweden, SEK, and fixture-only.

## Keep the engineering question small

A future proof should translate one reviewed, country-specific expected posting into one provider voucher, attach its original evidence, read the actual result, and explain recovery after an ambiguous response. The result is a comparison of contracts and assumptions, not a Nordic platform claim.

| Concept | Candidate for reuse | Must be established for each proof |
| --- | --- | --- |
| Operation identity | Stable source operation key and immutable local intent | Provider tenant binding and legal entity identity |
| Expected versus observed | Explicit population, scoped observations, full posting comparison | Provider voucher IDs, fiscal-period identifiers, pagination, row semantics, and decimal units |
| Original evidence | Original-byte hashes and exact voucher association | Upload format, readback endpoints, attachment completeness, and authorization |
| Ambiguous effects | Retain uncertainty until evidence proves an outcome | Endpoint-specific idempotency, consistency, timeout, and retry guarantees |
| Accounting treatment | Freeze a reviewed expectation without inventing missing facts | Local account mappings, VAT codes, applicability dates, deductibility, and currency |

The current verification report carries a Fortnox/SE/SEK/P0 policy tag. Its comparison does not derive VAT or map charts of accounts. Its acquisition layer still uses Fortnox-shaped fixture resources. The concepts above are hypotheses for reuse, not demonstrated portable adapters.

## Primary contracts to examine

The documentation observations below were checked on 2026-10-14. They establish published contracts only. No authenticated provider calls were made.

| Candidate | Current documentation observation | Question the proof must settle |
| --- | --- | --- |
| Visma e-conomic, Denmark | Its Documents API uses app-secret and agreement-grant headers, multipart attachments, and accounting-year/voucher-number references. It documents an optional idempotency header and attachment conflict modes. | Which exact voucher and attachment operations support those semantics, and what readback proves their effects? |
| Tripletex, Norway | It documents Basic authentication with a session token and company selection. Test and production credentials are separate. | Which test-company voucher, evidence, fiscal-period, and retry contracts can be pinned and verified? |

Sources are [e-conomic Documents API](https://apis.e-conomic.com/documentsapi/redoc.html), [e-conomic REST API](https://restdocs.e-conomic.com/), [Tripletex authentication](https://developer.tripletex.no/docs/documentation/authentication-and-tokens/), and [Tripletex test OpenAPI guidance](https://developer.tripletex.no/docs/documentation/getting-started/2-explore/).

An idempotency option on one resource does not establish safe retries for another. A test environment does not establish account access, correct VAT treatment, or completed provider verification.

## Deliver one bounded proof per provider

1. Pin one company, currency, fiscal period, account mapping, and independently reviewed tax treatment.
2. Capture exact request and response contracts for create, attach, and readback.
3. Run one voucher and original-evidence attachment against the authorized test environment.
4. Repeat the source operation and inspect every relevant target observation.
5. Exercise an ambiguous response under the documented endpoint contract. Report anything unprovable as uncertain.
6. Record the reusable comparison inputs and the provider-specific policy that stayed outside them.

Prefer one provider proof before deciding what to extract for the next. The [Fortnox sandbox gates](sandbox-gates.md) remain the first external integration gate.
