# Ramp to Fortnox

An engineering experiment for one Swedish domestic card purchase. It contains a Python fixture reference, an independent read-only verifier, and a TypeScript sandbox service with frozen posting plans and durable recovery.

Built independently as a portfolio project for Ramp engineers. No proprietary Drastic code or company records are included. The Python commands use local HTTP services. The TypeScript service has gated provider adapters and PostgreSQL persistence. Verification so far uses synthetic HTTP services; real Ramp and Fortnox sandbox interoperability remains unproved.

## Run the proof

Use Python 3.9 or later. The implementation uses the standard library.

```sh
python3 scripts/verify.py
python3 scripts/demo.py
python3 scripts/verification_demo.py
python3 -m ramp_fortnox --help
```

The verification script drives the public CLI in separate processes against durable local HTTP services. It produces a JSON proof and readable transcript in the ignored `artifacts/` directory. CI reruns the same proof and uploads its artifacts. Fixture results establish behavior under the injected faults. They do not establish provider interoperability.

## Run the TypeScript service proof

```sh
cd service
bun install --frozen-lockfile
bun run typecheck
bun run proof
bun run connector --help
```

Use Bun 1.4.0 and PostgreSQL binaries through `PG_BIN`, or an explicitly supplied isolated test database through `RAMP_TEST_DATABASE_URL`. The runner creates and stops its own local cluster and retains redacted transcripts, SQL states and provider effect counts. Effect 4 owns typed failure/resource boundaries. PostgreSQL and Drizzle own synchronization state. The [sandbox service guide](docs/sandbox-service.md) covers configured service grants, probe requirements, evidence templates and fingerprint-approved apply.

Voucher creation, upload, linking and acknowledgment remain separate durable outcomes. Lost non-idempotent requests do not become new writes because evidence is absent. A pre-existing test voucher/file must establish supported receipt readback before the first voucher POST. A probe is bounded contract evidence for its configured company. Successful synthetic tests do not prove a provider contract or live integration.

## Inspect one posting

The synthetic purchase is 1,250.00 SEK with an explicit 250.00 SEK VAT figure. Its literal expected voucher is authored independently of the posting code.

| Account | Debit, SEK | Credit, SEK |
| --- | ---: | ---: |
| 5410, selected expense | 1,000.00 | 0.00 |
| 2641, input VAT | 250.00 | 0.00 |
| 2899, explicitly configured fixture liability | 0.00 | 1,250.00 |

Account 2899 is a synthetic configuration choice. A real card funding model needs its own explicit liability mapping.

`plan` prints the exact posting and evidence. `apply` refuses stale inputs and dispatches the frozen plan. A receipt OCR value can supply the VAT amount only when its units are explicitly calibrated and it agrees with evidence tied to the original receipt. A 25% code never supplies missing VAT.

P0 requires one bound legal entity, a cleared sync-ready purchase, satisfied Ramp approval requirements, a domestic merchant, and SEK merchant, settlement, and receipt amounts. The financial year and all posting accounts must be valid. Split coding, required vendor creation, refunds, foreign currencies, other rates, reverse charge, bills, reimbursements, multiple entities, scheduling, and deployment are outside this experiment. Removed rows or unexpected project/cost-center coding cannot confirm the frozen posting.

## Recover without another voucher

SQLite enforces operation identity and records dispatch intent before each external call. A process lock prevents competing local CLI processes from taking over active dispatch. Confirmed vouchers retain their identity while later steps are repaired.

```text
plan -> create voucher -> upload receipt -> confirm attachment -> acknowledge Ramp
                   |
               lost response
                   |
     scan every page and compare full voucher payloads
          |                    |                  |
     one exact match       no match          conflicting evidence
          |                    |                  |
      confirmed            uncertain          investigate
```

An empty search never authorizes a voucher retry. A matching marker with different rows, duplicate markers, or a foreign voucher in the owned series stops for investigation. A failed attachment never causes voucher recreation. Ramp receives success only after the voucher and original receipt attachment are confirmed.

These guarantees require one canonical local ledger. They do not cover copied databases, multiple hosts, or another connector writing to the same series. The owned-series convention detects violations; it cannot prevent external writes.

## Independently assess the result

`verify-sync` captures every selected local frozen plan, including never-dispatched plans. It collects scoped GET-only observations and reports Confirmed, Missing, Duplicate, Mismatched, or Unknown with per-dimension checks and coverage. It never changes accounting records or the operation journal.

```sh
python3 -m ramp_fortnox --workspace local/run --fixture fixtures/purchase.json \
  --base-url http://127.0.0.1:PORT verify-sync \
  --company 556000-0000 --year 1 --series R --output artifacts/report
python3 -m ramp_fortnox replay-verification \
  --source artifacts/report/source.json --observed artifacts/report/observed.json \
  --output artifacts/replay
```

This independent verifier currently reads the Python SQLite fixture journal. The source population excludes unplanned Ramp transactions. A completed scan is a bounded observation interval, not an atomic provider snapshot. Missing means an expected marker was not observed during that scan. It never proves failed execution or permits a retry. Failed pages, voucher details, or receipt reads preserve uncertainty.

Run `scripts/verification_demo.py` to reproduce all five findings and offline replay. Reports retain the source snapshot, raw observation outcomes, hashes, and GET-only/unchanged-record proof. Read the [verification boundary](docs/verification.md) before interpreting a result.

## Review the decisions

Start with the [failure contract](docs/failure-modes.md) and [literal posting oracle](fixtures/expected-posting.json). Read the [architecture](docs/architecture.md) for state ownership and recovery. The [API matrix](docs/api-contracts.md) separates documented fields from unverified semantics. The [sandbox gates](docs/sandbox-gates.md) define the next proof before a live adapter can write. The [application summary](docs/application-summary.md) describes the verified scope in four sentences.

Ramp's current schema documents optional receipt OCR `taxes`. It leaves the units of `tax_amount` unspecified. Fixture calibration is synthetic and does not establish units for a real payload. Actual service-grant authorization, response shapes, pagination, upload readback, and live fault behavior remain unverified. The [Nordic follow-up](docs/nordic-proof.md) is a later bounded proof proposal, after actual Fortnox interoperability.
