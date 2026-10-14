# API contracts and verification limits

Checked against primary documentation on 2026-10-14. No provider sandbox has been exercised. The local HTTP services implement selected contract shapes and explicit fault scenarios. They are not vendor emulators or evidence of vendor behavior.

## Ramp

The [current OpenAPI schema](https://docs.ramp.com/openapi/developer-api.json) defines the selected fields recorded in [ramp-contract.json](ramp-contract.json). The [ERP guide](https://docs.ramp.com/llms-guides/erp-integrations.txt) describes connection setup and accounting syncs. The [changelog](https://docs.ramp.com/llms-guides/changelog.txt) describes receipt OCR and settlement amounts.

| Operation | Endpoint | Scope | Relevant contract |
| --- | --- | --- | --- |
| Fetch candidates | `GET /developer/v1/transactions?sync_status=SYNC_READY` | `transactions:read` | Eligibility also requires `state=CLEARED` and `all_requirements_met_and_approved=true`. |
| Refresh transaction | `GET /developer/v1/transactions/{transaction_id}` | `transactions:read` | Freeze `updated_at`, `entity_id`, accounting selections, receipt IDs, and signed settlement evidence. |
| Fetch receipt | `GET /developer/v1/receipts/{receipt_id}?include_ocr_data=true` | `receipts:read` | `transaction_id` binds the receipt. `receipt_url` downloads the original. Its signature expires and is not receipt identity. |
| Report completion | `POST /developer/v1/accounting/syncs` | `accounting:write` | Stable `idempotency_key`, `sync_type=TRANSACTION_SYNC`, `successful_syncs` with transaction ID and voucher reference. |
| Register accounting connection | Accounting connection API in ERP guide | `accounting:read`, `accounting:write` | Arbitrary `remote_provider_name` acceptance remains unverified. |

`entity_amount` and `merchant_amount` use `{currency, value}`. Values are signed integer minor units. Positive SEK purchases are the only supported amounts. `accounting_field_selections` provides coding. A selected tax code is not VAT evidence.

Receipt `ocr` and its `taxes` array are optional. Each tax has nullable numeric `tax_amount` and `tax_rate`, plus nullable `tax_name`. The schema does not define the units of `tax_amount` or the scale of `tax_rate`. The fixture declares these units explicitly and binds the VAT figure to original receipt evidence. That fixture declaration does not calibrate real Ramp data.

`mark_ready_to_sync=true` clears accounting requirements before recording completion. This project leaves it false. The transaction must already meet eligibility requirements.

## Fortnox

The [voucher guide](https://www.fortnox.se/developer/guides-and-good-to-know/best-practices/vouchers), [API reference](https://apps.fortnox.se/apidocs), [scope table](https://www.fortnox.se/developer/guides-and-good-to-know/scopes), and [parameter guide](https://www.fortnox.se/developer/guides-and-good-to-know/parameters) describe the resources below.

| Operation | Endpoint | Scope | Verification gate |
| --- | --- | --- | --- |
| Verify company | `GET /3/companyinformation` | `companyinformation` | Match configured organization number and database identity after connect or token renewal. |
| Find financial year | `GET /3/financialyears?date={date}` | `bookkeeping` | Exactly one year contains the posting date. |
| Check accounts | `GET /3/accounts/{number}?financialyear={year}` | `bookkeeping` | Expense, VAT, and configured liability accounts are active. |
| Check series | Voucher series resource, scoped to financial year | `bookkeeping` | Dedicated configured series exists. `Manual=false` does not prove exclusivity. |
| Create voucher | `POST /3/vouchers?financialyear={year}` | `bookkeeping` | Exact frozen balanced payload. No integrator-settable external ID or create idempotency guarantee was found. |
| List vouchers | `GET /3/vouchers?financialyear={year}&page={page}` | `bookkeeping` | Consume every page and inspect the whole owned series. Confirm exact pagination metadata in a test company. |
| Read voucher | `GET /3/vouchers/{series}/{number}?financialyear={year}` | `bookkeeping` | Compare full date, series, accounts, amounts, and rows. |
| Upload receipt | `POST /3/inbox?path=Inbox_v` | `inbox` | Multipart original file. Response contains `File.Id`. |
| Connect receipt | `POST /3/voucherfileconnections?financialyear={year}` | `connectfile` | Bind exact file ID, voucher series, and voucher number. |
| Confirm connection | Voucher file connection resource | `connectfile` | Verify persisted association and original file evidence. Exact read/filter semantics need sandbox proof. |
| Read original archive file | Archive resource, if required for content verification | `archive` | Pin download and hash-verification semantics before enabling live recovery. |

The guide's voucher-series URL syntax differs from the modern reference. Pin the exact year query and response shape with a real response before enabling an adapter. Shared pagination documentation does not prove list completeness after a concurrent write.

No observed or documented absence guarantee authorizes another voucher POST after a timeout. Deletion is not part of recovery. A missing match stays uncertain.

## What remains unverified

- Ramp sandbox access, Swedish entities, SEK cards, accounting connection registration, and tax-bearing receipt availability.
- Real OCR amount units, rate scale, rounding, and agreement with the receipt.
- OAuth and PKCE, token renewal, company identity stability, and developer licence terms.
- Fortnox response shapes, pagination termination, full row equality, receipt upload and content readback, and attachment confirmation.
- Provider behavior under lost responses, delayed commits, and repeated requests.

These are implementation gates for a future live adapter. The fixture demo does not bypass them.
