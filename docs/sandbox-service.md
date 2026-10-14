# TypeScript sandbox service

The Bun CLI implements one domestic SEK card purchase using Effect 4, PostgreSQL, Drizzle and provider HTTP adapters. It keeps the Python fixture proof alongside it. No actual Ramp sandbox or Fortnox test-company transaction has been executed.

## Run the synthetic proof

```sh
cd service
bun install --frozen-lockfile
bun run typecheck
bun run proof
bun run connector --help
```

The proof starts a fresh PostgreSQL cluster using `PG_BIN`, or uses an explicitly supplied `RAMP_TEST_DATABASE_URL` with isolated test schemas. It invokes the public CLI in separate processes against local HTTP fixtures and retains JSON results, SQL states, receipt hashes and transcripts in `service/artifacts/proof/`. The runner owns and stops its local cluster. CI uses a fresh PostgreSQL 17 service.

The expected voucher is copied from the independently authored Python oracle. The document is a separate valid synthetic PDF with its own pinned original hash. Local results prove injected failure behavior. They do not establish provider response shapes, consent, tax units or interoperability.

## Configure authorized sandbox access

Copy [config.example.json](../service/config.example.json) into an ignored private directory and replace the empty identifiers, account mappings and probe values. `databaseUrl` and credentials stay outside Git. Set the configured client-ID/client-secret environment variables and `CONNECTOR_KEY`, a base64-encoded 32-byte key held outside PostgreSQL. The service encrypts tokens, source snapshots, posting plans, requests and original receipt bytes with authenticated encryption.

The implemented grants are Ramp internal sandbox client credentials and Fortnox previously consented service-account client credentials. Fortnox requires the numeric tenant/database identity. Its expected organization number and Ramp business/entity pair also need an explicit reviewed legal binding. Browser authorization-code onboarding is a later grant implementation.

Ramp calls are pinned to `demo-api.ramp.com`. Fortnox uses its API host and the documented token host. The service checks identity after token issuance and before planning or dispatch. It requires an existing active API accounting connection for the configured provider. It does not register or replace that connection.

Receipt downloads use separately pinned origins, bounded content and no bearer header or redirects. Complete [evidence.example.json](../service/evidence.example.json) in an ignored private file using the actual transaction and original bytes. Its empty units and unverified checks deliberately block planning. Calibrate OCR amount units and rate scale independently; the synthetic calibration cannot enable a sandbox plan. The tax code must explicitly map to 25% under a reviewed mapping revision. There is no liability-account default.

Configure a pre-existing test voucher and attached original file outside the owned posting series. This probe must establish full voucher rows, the exact persisted file association, original-byte retrieval and usable pagination before posting. The adapter currently supports direct `VoucherRows` arrays. The older guide's nested row shape, metadata-only archive response, unsupported row dimensions or a hash mismatch block capability acquisition. Probe success is bounded evidence for that company and configured contract revision, not a universal provider guarantee.

## Execute one reviewed operation

```sh
bun run connector connect --config ../local/sandbox.json
bun run connector inspect --config ../local/sandbox.json \
  --transaction TRANSACTION_ID --evidence ../local/receipt-evidence.json
bun run connector plan --config ../local/sandbox.json \
  --transaction TRANSACTION_ID --evidence ../local/receipt-evidence.json
bun run connector apply --config ../local/sandbox.json \
  --operation OPERATION_ID --fingerprint REVIEWED_PLAN_HASH
bun run connector worker --once --config ../local/sandbox.json \
  --operation OPERATION_ID --fingerprint REVIEWED_PLAN_HASH
bun run connector status --config ../local/sandbox.json \
  --operation OPERATION_ID --proof ../local/proof
```

`inspect` acquires provider observations without posting. `plan` freezes them in PostgreSQL. `apply` requires the reviewed fingerprint and refetches material source/accounting evidence. `worker --once` runs the same durable operation. `reconcile` observes uncertain effects without dispatching new steps. `status` exports the local journal's recorded outcome; it does not make a fresh remote correctness claim.

## Preserve uncertainty across interruptions

```text
PostgreSQL unique source action + immutable approved plan
                            |
           dedicated target/year/series session lock
                            |
             committed intent and request hash
                            |
                       provider call
                     /             \
             exact readback     ambiguous outcome
                  |                  |
              confirmed         GET reconciliation
                                 /            \
                          exact match       no match
                           confirmed        uncertain
```

Voucher creation, receipt upload, linking and Ramp acknowledgment have separate durable steps. A lost create or upload response never becomes another POST because a scan is empty. An uncertain link can be recovered from its known file association. Only Ramp's frozen idempotent acknowledgment request can replay, after rechecking the voucher, original bytes, association and source.

Session death stops later dispatch. It cannot fence an already in-flight remote request. The canonical database and schema coordinate local workers; copied databases/schemas and other integrators are outside that guarantee. Sequence counters and account balances are observations. Frozen equality covers stable accounting policy and evidence, excluding the documented changes caused by the operation itself.

Read the [failure contract](../service/failure-contract.md) and [sandbox gates](sandbox-gates.md). The next external proof still needs authorized access, an eligible SEK transaction, calibrated receipt evidence, actual contract probes, one verified voucher/attachment/acknowledgment and unchanged replay identity. Scheduling, dashboards, production operations and Nordic adapters follow that proof.
