# Ramp to Fortnox

A fixture-driven engineering experiment for one Swedish domestic card purchase. It demonstrates frozen posting plans, evidence gates, and recovery when an accounting API call may have committed without returning a response.

Built independently as a portfolio project for Ramp engineers. No proprietary Drastic code or company records are included. This version uses local HTTP services only. Real Ramp and Fortnox sandbox verification is deferred.

## Run the proof

Use Python 3.9 or later. The implementation uses the standard library.

```sh
python3 scripts/verify.py
python3 scripts/demo.py
python3 -m ramp_fortnox --help
```

The verification script drives the public CLI in separate processes against durable local HTTP services. It produces a JSON proof and readable transcript in the ignored `artifacts/` directory. CI reruns the same proof and uploads its artifacts. Fixture results establish behavior under the injected faults. They do not establish provider interoperability.

## Inspect one posting

The synthetic purchase is 1,250.00 SEK with an explicit 250.00 SEK VAT figure. Its literal expected voucher is authored independently of the posting code.

| Account | Debit, SEK | Credit, SEK |
| --- | ---: | ---: |
| 5410, selected expense | 1,000.00 | 0.00 |
| 2641, input VAT | 250.00 | 0.00 |
| 2899, explicitly configured fixture liability | 0.00 | 1,250.00 |

Account 2899 is a synthetic configuration choice. A real card funding model needs its own explicit liability mapping.

`plan` prints the exact posting and evidence. `apply` refuses stale inputs and dispatches the frozen plan. A receipt OCR value can supply the VAT amount only when its units are explicitly calibrated and it agrees with evidence tied to the original receipt. A 25% code never supplies missing VAT.

P0 requires one bound legal entity, a cleared sync-ready purchase, satisfied Ramp approval requirements, a domestic merchant, and SEK merchant, settlement, and receipt amounts. The financial year and all posting accounts must be valid. Refunds, foreign currencies, other rates, reverse charge, bills, reimbursements, multiple entities, scheduling, and deployment are outside this experiment.

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

## Review the decisions

Start with the [failure contract](docs/failure-modes.md) and [literal posting oracle](fixtures/expected-posting.json). Read the [architecture](docs/architecture.md) for state ownership and recovery. The [API matrix](docs/api-contracts.md) separates documented fields from unverified semantics. The [sandbox gates](docs/sandbox-gates.md) define the next proof before a live adapter can write. The [application summary](docs/application-summary.md) describes the verified scope in four sentences.

Ramp's current schema documents optional receipt OCR `taxes`. It leaves the units of `tax_amount` unspecified. Fixture calibration is synthetic and does not establish units for a real payload. Provider OAuth, response shapes, pagination, upload readback, and live fault behavior remain unverified.
