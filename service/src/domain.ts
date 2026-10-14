import { Schema } from "effect";
import { parse, LosslessNumber, stringify } from "lossless-json";
import {
  createHash,
  createCipheriv,
  createDecipheriv,
  randomBytes,
} from "node:crypto";

export class Gate extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export function requireThat(
  condition: unknown,
  code: string,
): asserts condition {
  if (!condition) throw new Gate(code);
}
export const ConfigSchema = Schema.Struct({
  mode: Schema.Literals(["synthetic", "sandbox"]),
  databaseUrl: Schema.String,
  schema: Schema.String,
  keyEnv: Schema.String,
  ramp: Schema.Struct({
    origin: Schema.String,
    tokenUrl: Schema.String,
    clientIdEnv: Schema.String,
    clientSecretEnv: Schema.String,
    businessId: Schema.String,
    entityId: Schema.String,
    providerName: Schema.String,
  }),
  fortnox: Schema.Struct({
    origin: Schema.String,
    tokenUrl: Schema.String,
    clientIdEnv: Schema.String,
    clientSecretEnv: Schema.String,
    tenantId: Schema.String,
    organizationNumber: Schema.String,
    consentReference: Schema.String,
  }),
  binding: Schema.Struct({
    sourceReference: Schema.String,
    entityId: Schema.String,
    organizationNumber: Schema.String,
  }),
  mapping: Schema.Struct({
    revision: Schema.String,
    expenseAccount: Schema.Number,
    vatAccount: Schema.Number,
    liabilityAccount: Schema.Number,
    taxCode: Schema.String,
    taxRate: Schema.String,
    taxMappingReference: Schema.String,
    series: Schema.String,
  }),
  receiptHosts: Schema.Array(Schema.String),
  timeoutMs: Schema.Number,
  maxReceiptBytes: Schema.Number,
  probe: Schema.Struct({
    series: Schema.String,
    number: Schema.Number,
    year: Schema.Number,
    fileId: Schema.String,
    originalSha256: Schema.String,
    revision: Schema.String,
  }),
});
export type Config = typeof ConfigSchema.Type;
export function config(value: unknown): Config {
  const c = Schema.decodeUnknownSync(ConfigSchema)(value);
  requireThat(
    /^[a-z][a-z0-9_]{0,50}$/.test(c.schema),
    "invalid-database-schema",
  );
  requireThat(
    /^\d+$/.test(c.fortnox.tenantId) && c.fortnox.consentReference,
    "service-consent-required",
  );
  requireThat(
    c.binding.entityId === c.ramp.entityId &&
      c.binding.organizationNumber === c.fortnox.organizationNumber &&
      c.binding.sourceReference,
    "legal-binding-required",
  );
  requireThat(
    [
      c.mapping.expenseAccount,
      c.mapping.vatAccount,
      c.mapping.liabilityAccount,
    ].every((n) => Number.isSafeInteger(n) && n > 0) &&
      new Set([
        c.mapping.expenseAccount,
        c.mapping.vatAccount,
        c.mapping.liabilityAccount,
      ]).size === 3 &&
      c.mapping.vatAccount === 2641,
    "explicit-distinct-account-mapping-required",
  );
  requireThat(
    c.mapping.taxRate === "25" && c.mapping.taxMappingReference,
    "reviewed-tax-code-rate-mapping-required",
  );
  requireThat(
    Number.isSafeInteger(c.probe.year) &&
      c.probe.year > 0 &&
      Number.isSafeInteger(c.probe.number) &&
      c.probe.number > 0 &&
      /^[A-Z]$/.test(c.probe.series),
    "invalid-readback-probe-identity",
  );
  requireThat(
    /^[A-Z]$/.test(c.mapping.series) &&
      c.mapping.revision &&
      c.probe.revision &&
      c.probe.series !== c.mapping.series,
    "dedicated-series-and-separate-probe-required",
  );
  requireThat(
    c.timeoutMs >= 100 &&
      c.timeoutMs <= 60000 &&
      c.maxReceiptBytes > 0 &&
      c.maxReceiptBytes <= 20 * 1024 * 1024,
    "invalid-http-bounds",
  );
  if (c.mode === "synthetic") {
    requireThat(
      process.env.CONNECTOR_TEST_MODE === "loopback-only",
      "explicit-loopback-test-mode-required",
    );
    for (const origin of [
      c.ramp.origin,
      c.ramp.tokenUrl,
      c.fortnox.origin,
      c.fortnox.tokenUrl,
      ...c.receiptHosts,
    ]) {
      const u = new URL(origin);
      requireThat(
        u.protocol === "http:" &&
          u.hostname === "127.0.0.1" &&
          !u.username &&
          !u.password,
        "test-origin-must-be-loopback",
      );
    }
  } else {
    requireThat(
      c.ramp.origin === "https://demo-api.ramp.com" &&
        c.ramp.tokenUrl === "https://demo-api.ramp.com/developer/v1/token" &&
        c.fortnox.origin === "https://api.fortnox.se/3" &&
        c.fortnox.tokenUrl === "https://apps.fortnox.se/oauth-v1/token",
      "sandbox-hosts-required",
    );
    for (const host of c.receiptHosts) {
      const u = new URL(host);
      requireThat(
        u.protocol === "https:" &&
          host === u.origin &&
          !u.username &&
          !u.password,
        "pinned-https-receipt-host-required",
      );
    }
  }
  return c;
}
export const EvidenceSchema = Schema.Struct({
  mode: Schema.Literals(["synthetic", "sandbox"]),
  businessId: Schema.String,
  entityId: Schema.String,
  merchant_country: Schema.String,
  coded_expense_account: Schema.Number,
  source_reference: Schema.String,
  units_calibration: Schema.Struct({
    verified: Schema.Boolean,
    unit: Schema.Literals(["SEK_MAJOR", "SEK_MINOR"]),
    rate_scale: Schema.String,
    field_path: Schema.String,
    contract_revision: Schema.String,
    source_reference: Schema.String,
    rounding: Schema.String,
  }),
  receipt: Schema.Struct({
    transaction_id: Schema.String,
    receipt_id: Schema.String,
    currency: Schema.String,
    gross_ore: Schema.Number,
    vat_ore: Schema.Number,
    net_ore: Schema.Number,
    merchant: Schema.String,
    date: Schema.String,
    original_match_verified: Schema.Boolean,
    sha256: Schema.String,
    original_base64: Schema.String,
    filename: Schema.String,
    contentType: Schema.String,
  }),
});
export type Evidence = typeof EvidenceSchema.Type;
export function evidence(value: unknown): Evidence {
  try {
    const e = Schema.decodeUnknownSync(EvidenceSchema)(value);
    requireThat(
      [e.receipt.gross_ore, e.receipt.vat_ore, e.receipt.net_ore].every(
        (v) => Number.isSafeInteger(v) && v > 0,
      ),
      "safe-integer-receipt-evidence-required",
    );
    return e;
  } catch {
    throw new Gate("receipt-units-and-original-evidence-required");
  }
}
export type Json =
  | null
  | boolean
  | string
  | number
  | Json[]
  | { [key: string]: Json };
export type ObjectJson = { [key: string]: Json };
export function object(value: unknown): ObjectJson {
  requireThat(
    value && typeof value === "object" && !Array.isArray(value),
    "object-response-required",
  );
  return value as ObjectJson;
}
export function array(value: unknown): Json[] {
  requireThat(Array.isArray(value), "array-response-required");
  return value as Json[];
}
export function text(value: unknown): string {
  requireThat(
    typeof value === "string" && value.length,
    "string-response-required",
  );
  return value;
}
export function integer(value: unknown): number {
  requireThat(
    (typeof value === "number" && Number.isSafeInteger(value)) ||
      (typeof value === "string" && /^\d+$/.test(value)),
    "integer-response-required",
  );
  const n = Number(value);
  requireThat(Number.isSafeInteger(n), "integer-response-out-of-range");
  return n;
}
export function lossless(content: string): unknown {
  return parse(content, (_key, value) =>
    value instanceof LosslessNumber ? value.value : value,
  );
}
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object")
    return (
      "{" +
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => JSON.stringify(k) + ":" + canonical(v))
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
export function hash(value: unknown): string {
  return bytesHash(Buffer.from(canonical(value)));
}
export function bytesHash(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}
export function minor(value: unknown, scale = 0): bigint {
  requireThat(
    typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value),
    "exact-decimal-required",
  );
  const [whole, fraction = ""] = value.split(".");
  requireThat(
    fraction
      .slice(scale)
      .split("")
      .every((d) => d === "0"),
    "fractional-minor-unit",
  );
  return (
    BigInt(whole) * 10n ** BigInt(scale) +
    BigInt(fraction.slice(0, scale).padEnd(scale, "0") || "0")
  );
}
export function money(value: bigint): string {
  return `${value / 100n}.${(value % 100n).toString().padStart(2, "0")}`;
}
export type Row = { Account: number; Debit: string; Credit: string };
export type Posting = {
  Voucher: {
    VoucherSeries: string;
    TransactionDate: string;
    Description: string;
    VoucherRows: Row[];
  };
};
export type Snapshot = {
  transaction: ObjectJson;
  receipt: ObjectJson;
  originalSha256: string;
  identity: unknown;
  preflight: unknown;
  capabilities: unknown;
  binding: unknown;
  mapping: unknown;
  evidence: Evidence;
};
export type Plan = {
  operationId: string;
  fingerprint: string;
  posting: Posting;
  source: Snapshot;
  originalBase64: string;
  year: number;
  configHash: string;
  mode: Config["mode"];
};
export const PlanSchema = Schema.Struct({
  operationId: Schema.String,
  fingerprint: Schema.String,
  posting: Schema.Unknown,
  source: Schema.Unknown,
  originalBase64: Schema.String,
  year: Schema.Number,
  configHash: Schema.String,
  mode: Schema.Literals(["synthetic", "sandbox"]),
});
export function restoredPlan(value: unknown): Plan {
  const p = Schema.decodeUnknownSync(PlanSchema)(value);
  const v = object(object(p.posting).Voucher);
  const rows = array(v.VoucherRows);
  requireThat(
    rows.length === 3 &&
      rows.every((r) => {
        const row = object(r);
        return (
          integer(row.Account) > 0 &&
          typeof row.Debit === "string" &&
          typeof row.Credit === "string"
        );
      }),
    "stored-posting-invalid",
  );
  const source = object(p.source);
  evidence(source.evidence);
  requireThat(
    hash({
      operationId: p.operationId,
      posting: p.posting,
      source: p.source,
      year: p.year,
      configHash: p.configHash,
      mode: p.mode,
    }) === p.fingerprint,
    "stored-plan-fingerprint-invalid",
  );
  return p as Plan;
}
export function materialConfig(c: Config) {
  return {
    mode: c.mode,
    ramp: c.ramp,
    fortnox: c.fortnox,
    binding: c.binding,
    mapping: c.mapping,
    probe: c.probe,
    receiptHosts: c.receiptHosts,
  };
}
export function purchase(
  c: Config,
  s: Snapshot,
  bytes: Buffer,
  year: number,
): Plan {
  const t = s.transaction,
    r = s.receipt,
    e = s.evidence,
    m = c.mapping;
  requireThat(
    e.mode === c.mode &&
      e.businessId === c.ramp.businessId &&
      e.entityId === c.ramp.entityId,
    "calibration-scope-mismatch",
  );
  requireThat(
    t.entity_id === c.ramp.entityId &&
      t.state === "CLEARED" &&
      t.sync_status === "SYNC_READY" &&
      t.all_requirements_met_and_approved === true &&
      t.updated_at,
    "eligible-approved-transaction-required",
  );
  requireThat(
    t.requires_accounting_vendor_creation_to_sync !== true,
    "accounting-vendor-creation-unsupported",
  );
  requireThat(
    t.line_items === undefined || array(t.line_items).length === 0,
    "split-line-items-unsupported",
  );
  const settlement = object(t.entity_amount),
    merchant = object(t.merchant_amount);
  requireThat(
    settlement.currency === "SEK" && merchant.currency === "SEK",
    "SEK-only",
  );
  const gross = minor(settlement.value);
  requireThat(
    gross > 0n && gross === minor(merchant.value),
    "settlement-merchant-amount-mismatch",
  );
  requireThat(
    object(t.merchant_location).country === "SE" && e.merchant_country === "SE",
    "domestic-only",
  );
  const selections = array(t.accounting_field_selections).map(object),
    accounts = selections.filter((x) => x.type === "GL_ACCOUNT"),
    taxes = selections.filter((x) => x.type === "TAX_CODE");
  requireThat(
    accounts.length === 1 &&
      taxes.length === 1 &&
      accounts[0].provider_name === c.ramp.providerName &&
      taxes[0].provider_name === c.ramp.providerName &&
      accounts[0].external_id === String(m.expenseAccount) &&
      taxes[0].external_id === m.taxCode &&
      e.coded_expense_account === m.expenseAccount,
    "coding-mapping-mismatch",
  );
  requireThat(
    array(t.receipts).length === 1 &&
      array(t.receipts)[0] === r.id &&
      r.transaction_id === t.id &&
      r.id === e.receipt.receipt_id &&
      t.id === e.receipt.transaction_id,
    "receipt-link-mismatch",
  );
  const cal = e.units_calibration;
  requireThat(
    cal.verified &&
      cal.field_path === "receipt.ocr.taxes[].tax_amount" &&
      cal.rate_scale === "percent" &&
      cal.rounding === "HALF_UP_ORE" &&
      cal.source_reference &&
      cal.contract_revision,
    "tax-units-calibration-required",
  );
  const ocr = object(r.ocr),
    tax = array(ocr.taxes);
  requireThat(
    ocr.currency_code === "SEK" && tax.length === 1,
    "single-SEK-tax-required",
  );
  requireThat(
    minor(object(tax[0]).tax_rate) === 25n,
    "explicit-25-percent-required",
  );
  const vat = minor(
    object(tax[0]).tax_amount,
    cal.unit === "SEK_MAJOR" ? 2 : 0,
  );
  requireThat(vat > 0n && vat === (gross + 2n) / 5n, "VAT-gross-mismatch");
  const original = e.receipt;
  const date = text(t.accounting_date).slice(0, 10);
  requireThat(
    /^\d{4}-\d{2}-\d{2}$/.test(date) &&
      new Date(date).toISOString().slice(0, 10) === date,
    "invalid-accounting-date",
  );
  requireThat(
    original.original_match_verified &&
      original.sha256 === bytesHash(bytes) &&
      Buffer.from(original.original_base64, "base64").equals(bytes) &&
      original.currency === "SEK" &&
      original.merchant === t.merchant_name &&
      original.date === date &&
      BigInt(original.gross_ore) === gross &&
      BigInt(original.vat_ore) === vat &&
      BigInt(original.net_ore) === gross - vat,
    "original-receipt-evidence-mismatch",
  );
  requireThat(
    (original.contentType === "application/pdf" &&
      original.filename.endsWith(".pdf") &&
      bytes.subarray(0, 5).toString() === "%PDF-") ||
      (original.contentType === "image/jpeg" &&
        /\.jpe?g$/i.test(original.filename) &&
        bytes[0] === 0xff &&
        bytes[1] === 0xd8) ||
      (original.contentType === "image/tiff" &&
        /\.tiff?$/i.test(original.filename) &&
        ["II", "MM"].includes(bytes.subarray(0, 2).toString())),
    "supported-original-file-required",
  );
  const operationId = `ramp:${c.ramp.businessId}:${c.ramp.entityId}:${text(t.id)}:card_purchase`;
  const posting: Posting = {
    Voucher: {
      VoucherSeries: m.series,
      TransactionDate: date,
      Description: operationId,
      VoucherRows: [
        {
          Account: m.expenseAccount,
          Debit: money(gross - vat),
          Credit: "0.00",
        },
        { Account: m.vatAccount, Debit: money(vat), Credit: "0.00" },
        { Account: m.liabilityAccount, Debit: "0.00", Credit: money(gross) },
      ],
    },
  };
  const frozen = {
    operationId,
    posting,
    source: s,
    year,
    configHash: hash(materialConfig(c)),
    mode: c.mode,
  };
  return {
    ...frozen,
    fingerprint: hash(frozen),
    originalBase64: bytes.toString("base64"),
  };
}
export function wirePosting(posting: Posting): string {
  return stringify({
    Voucher: {
      ...posting.Voucher,
      VoucherRows: posting.Voucher.VoucherRows.map((r) => ({
        ...r,
        Debit: new LosslessNumber(r.Debit),
        Credit: new LosslessNumber(r.Credit),
      })),
    },
  })!;
}
export function seal(value: unknown, key: Buffer, aad: string): string {
  const iv = randomBytes(12),
    cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(aad));
  const body = Buffer.concat([
    cipher.update(JSON.stringify(value)),
    cipher.final(),
  ]);
  return Buffer.concat([iv, cipher.getAuthTag(), body]).toString("base64");
}
export function unseal(value: string, key: Buffer, aad: string): unknown {
  const bytes = Buffer.from(value, "base64"),
    cipher = createDecipheriv("aes-256-gcm", key, bytes.subarray(0, 12));
  cipher.setAAD(Buffer.from(aad));
  cipher.setAuthTag(bytes.subarray(12, 28));
  return JSON.parse(
    Buffer.concat([
      cipher.update(bytes.subarray(28)),
      cipher.final(),
    ]).toString(),
  );
}
