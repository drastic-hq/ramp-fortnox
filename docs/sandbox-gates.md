# Verify the connector against both sandboxes

Live access is deferred for this version. This version runs only against local HTTP fixtures. It cannot write to provider hosts. Do not use its fixture receipt calibration for live data.

## Establish the authorized environment

1. Obtain Ramp sandbox access and a Fortnox test company through the providers' official setup flows. Do not use a real company.
2. Configure the Ramp business and Swedish legal entity, plus the Fortnox organization number and database identity.
3. Create a dedicated voucher series. Select a liability account for the actual card funding model. Do not infer that account from this demo.
4. Keep OAuth tokens, company records, receipts, and payload captures outside Git. Use the ignored `local/` directory or a credential store.
5. Verify Fortnox company identity after initial authorization and every token renewal.

## Pin evidence before enabling live writes

1. Fetch a cleared, fully coded `SYNC_READY` SEK transaction with `all_requirements_met_and_approved=true`.
2. Fetch its linked receipt with `include_ocr_data=true`. Require merchant, settlement, and receipt currency to be SEK.
3. Inspect the original receipt. Record its total, explicit VAT amount, 25% rate, and original-byte hash.
4. Establish `tax_amount` units and `tax_rate` scale with provider documentation or an independently checked sandbox sample. Record the evidence and its scope. Never infer minor units from other Ramp fields.
5. Require a single supported domestic tax treatment and exact agreement under the declared rounding rule. Missing or ambiguous evidence blocks posting.
6. Pin all Fortnox request and response shapes. Verify every page of the owned series and the full details of candidate vouchers.
7. Implement and review the live transport adapter and OAuth lifecycle. Preserve the fixture-only default and require explicit sandbox identity configuration.

## Produce the live proof

1. Print and inspect the frozen plan with the original receipt.
2. Apply it to the test company. Confirm the full voucher and original attachment before reporting success to Ramp.
3. Fetch Ramp again and confirm the sync state.
4. Replay the operation. Prove that the voucher count and identity do not change.
5. Run the authorized fault scenarios. Recover a committed lost response by full payload comparison. Leave an absent match uncertain.
6. Repair an attachment failure without recreating the voucher.
7. Save sanitized output and screenshots of the voucher, attachment, and Ramp sync state. Keep private originals and tokens out of the public repository.

Only this proof can support a claim that the connector works against both providers. Until then, application text must call it a fixture-driven engineering experiment.
