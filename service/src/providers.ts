import { Effect, Schema } from "effect";
import {
  Config,
  Evidence,
  Plan,
  Snapshot,
  Gate,
  requireThat,
  object,
  array,
  integer,
  text,
  lossless,
  minor,
  hash,
  bytesHash,
  wirePosting,
} from "./domain";
import { Store } from "./store";

export class ProviderFailure extends Error {
  constructor(
    readonly code: string,
    readonly status: number | null = null,
  ) {
    super(code);
  }
}
const TokenSchema = Schema.Struct({
  access_token: Schema.String,
  expires_in: Schema.String,
  token_type: Schema.String,
});

const AmountWire = Schema.Struct({
  currency: Schema.String,
  value: Schema.String,
});
const TransactionWire = Schema.Struct({
  id: Schema.String,
  entity_id: Schema.String,
  state: Schema.String,
  sync_status: Schema.String,
  all_requirements_met_and_approved: Schema.Boolean,
  updated_at: Schema.String,
  accounting_date: Schema.String,
  merchant_name: Schema.String,
  entity_amount: AmountWire,
  merchant_amount: AmountWire,
  receipts: Schema.Array(Schema.String),
  accounting_field_selections: Schema.Array(Schema.Unknown),
});
const ReceiptWire = Schema.Struct({
  id: Schema.String,
  transaction_id: Schema.String,
  receipt_url: Schema.String,
  ocr: Schema.Struct({
    currency_code: Schema.String,
    taxes: Schema.Array(
      Schema.Struct({ tax_amount: Schema.String, tax_rate: Schema.String }),
    ),
  }),
});
const RowWire = Schema.Struct({
  Account: Schema.String,
  Debit: Schema.String,
  Credit: Schema.String,
  Removed: Schema.optionalKey(Schema.Boolean),
  Project: Schema.optionalKey(Schema.Unknown),
  CostCenter: Schema.optionalKey(Schema.Unknown),
});
function rowSupported(value: unknown) {
  const row = Schema.decodeUnknownSync(RowWire)(value);
  requireThat(
    row.Removed !== true &&
      (row.Project === undefined ||
        row.Project === null ||
        row.Project === "") &&
      (row.CostCenter === undefined ||
        row.CostCenter === null ||
        row.CostCenter === ""),
    "voucher-row-dimensions-or-removal-unsupported",
  );
}

export class Providers {
  constructor(
    readonly c: Config,
    readonly store: Store,
  ) {}
  private request(
    url: string,
    init: RequestInit = {},
    binary = false,
  ): Effect.Effect<unknown, ProviderFailure> {
    return Effect.tryPromise({
      try: async () => {
        let response: Response;
        try {
          response = await fetch(url, {
            ...init,
            redirect: "error",
            signal: AbortSignal.timeout(this.c.timeoutMs),
          });
        } catch {
          throw new ProviderFailure("provider-request-unresolved");
        }
        if (!response.ok)
          throw new ProviderFailure(
            "provider-response-rejected",
            response.status,
          );
        const limit = binary ? this.c.maxReceiptBytes : 2 * 1024 * 1024;
        const reader = response.body?.getReader();
        requireThat(reader, "empty-provider-response");
        const chunks: Uint8Array[] = [];
        let size = 0;
        try {
          while (true) {
            const r = await reader.read();
            if (r.done) break;
            size += r.value.length;
            requireThat(size <= limit, "provider-content-too-large");
            chunks.push(r.value);
          }
        } finally {
          await reader.cancel();
        }
        const bytes = Buffer.concat(chunks);
        if (binary) return bytes;
        try {
          return lossless(bytes.toString());
        } catch {
          throw new ProviderFailure("invalid-provider-json");
        }
      },
      catch: (e) =>
        e instanceof ProviderFailure
          ? e
          : new ProviderFailure(
              e instanceof Gate ? e.code : "provider-boundary-failure",
            ),
    });
  }
  private async run(
    effect: Effect.Effect<unknown, ProviderFailure>,
  ): Promise<unknown> {
    const result = await Effect.runPromise(Effect.result(effect));
    if (result._tag === "Failure") throw result.failure;
    return result.success;
  }
  private async grant(provider: "ramp" | "fortnox"): Promise<string> {
    const cfg = this.c[provider],
      id = process.env[cfg.clientIdEnv],
      secret = process.env[cfg.clientSecretEnv];
    requireThat(id && secret, "authorized-service-credentials-required");
    const identityHash = hash({ provider, config: cfg, clientId: id });
    const lock = await this.store.lock(`auth:${this.c.schema}:${provider}`);
    try {
      await lock.assertAlive();
      const cached = await this.store.token(provider, identityHash);
      if (cached && cached.expiresAt > Date.now() + 30000) return cached.token;

      const scopes =
        provider === "ramp"
          ? "transactions:read receipts:read business:read entities:read accounting:read accounting:write"
          : "companyinformation bookkeeping inbox connectfile archive";
      const headers: Record<string, string> = {
        Authorization:
          "Basic " + Buffer.from(id + ":" + secret).toString("base64"),
        "Content-Type": "application/x-www-form-urlencoded",
      };
      if (provider === "fortnox") headers.TenantId = this.c.fortnox.tenantId;
      const raw = await this.run(
        this.request(cfg.tokenUrl, {
          method: "POST",
          headers,
          body: new URLSearchParams({
            grant_type: "client_credentials",
            scope: scopes,
          }),
        }),
      );
      let token;
      try {
        token = Schema.decodeUnknownSync(TokenSchema)(raw);
      } catch {
        throw new Gate("invalid-token-response");
      }
      const expiry = integer(token.expires_in);
      requireThat(
        expiry > 0 && token.token_type.toLowerCase() === "bearer",
        "invalid-token-response",
      );
      await this.identity(provider, token.access_token);
      await lock.assertAlive();
      await this.store.saveToken(
        provider,
        token.access_token,
        Date.now() + expiry * 1000,
        identityHash,
      );
      return token.access_token;
    } finally {
      await lock.close();
    }
  }
  private async identity(provider: "ramp" | "fortnox", token: string) {
    const headers = { Authorization: `Bearer ${token}` };
    if (provider === "fortnox") {
      const response = object(
          await this.run(
            this.request(this.c.fortnox.origin + "/companyinformation", {
              headers,
            }),
          ),
        ),
        info = object(response.CompanyInformation);
      requireThat(
        info.OrganizationNumber === this.c.fortnox.organizationNumber &&
          integer(info.DatabaseNumber) === Number(this.c.fortnox.tenantId),
        "wrong-Fortnox-identity",
      );
      return info;
    }
    const business = object(
      await this.run(
        this.request(this.c.ramp.origin + "/developer/v1/business", {
          headers,
        }),
      ),
    );
    const entity = object(
      await this.run(
        this.request(
          this.c.ramp.origin +
            "/developer/v1/entities/" +
            encodeURIComponent(this.c.ramp.entityId),
          { headers },
        ),
      ),
    );
    requireThat(
      business.id === this.c.ramp.businessId &&
        entity.id === this.c.ramp.entityId &&
        entity.currency === "SEK",
      "wrong-Ramp-identity",
    );
    return { business, entity };
  }
  async get(
    provider: "ramp" | "fortnox",
    path: string,
    binary = false,
  ): Promise<unknown> {
    requireThat(
      path.startsWith("/") && !path.startsWith("//"),
      "invalid-provider-path",
    );
    const token = await this.grant(provider);
    return this.run(
      this.request(
        this.c[provider].origin + path,
        { headers: { Authorization: `Bearer ${token}` } },
        binary,
      ),
    );
  }
  async identities() {
    const ramp = await this.identity("ramp", await this.grant("ramp")),
      fortnox = await this.identity("fortnox", await this.grant("fortnox"));
    const connections = object(
      await this.get("ramp", "/developer/v1/accounting/all-connections"),
    );
    const active = array(connections.connections)
      .map(object)
      .filter(
        (v) =>
          v.is_active === true &&
          v.remote_provider_name === this.c.ramp.providerName &&
          v.connection_type === "API",
      );
    requireThat(
      active.length === 1,
      "existing-active-accounting-connection-required",
    );
    return { ramp, fortnox, connection: active[0] };
  }
  async original(url: string): Promise<Buffer> {
    const u = new URL(url);
    requireThat(
      this.c.receiptHosts.includes(u.origin) && !u.username && !u.password,
      "receipt-host-not-pinned",
    );
    return (await this.run(this.request(url, {}, true))) as Buffer;
  }
  async voucher(series: string, number: number, year: number) {
    const v = object(
      object(
        await this.get(
          "fortnox",
          `/vouchers/${encodeURIComponent(series)}/${number}?financialyear=${year}`,
        ),
      ).Voucher,
    );
    requireThat(
      v.VoucherSeries === series &&
        integer(v.VoucherNumber) === number &&
        integer(v.Year) === year,
      "voucher-readback-identity-mismatch",
    );
    text(v.TransactionDate);
    text(v.Description);
    requireThat(array(v.VoucherRows).length > 0, "full-voucher-rows-required");
    for (const value of array(v.VoucherRows)) {
      const row = object(value);
      rowSupported(row);
      integer(row.Account);
      minor(row.Debit, 2);
      minor(row.Credit, 2);
    }
    return v;
  }
  async link(fileId: string, series: string, number: number, year: number) {
    const link = object(
      object(
        await this.get(
          "fortnox",
          "/voucherfileconnections/" +
            encodeURIComponent(fileId) +
            "?financialyear=" +
            year,
        ),
      ).VoucherFileConnection,
    );
    requireThat(
      link.FileId === fileId &&
        link.VoucherSeries === series &&
        integer(link.VoucherNumber) === number &&
        integer(link.VoucherYear) === year,
      "attachment-readback-mismatch",
    );
    return link;
  }
  async file(fileId: string, sha256: string) {
    const bytes = (await this.get(
      "fortnox",
      "/archive?fileid=" + encodeURIComponent(fileId),
      true,
    )) as Buffer;
    requireThat(bytesHash(bytes) === sha256, "original-byte-readback-mismatch");
    return sha256;
  }
  async capabilities() {
    const p = this.c.probe;
    const v = await this.voucher(p.series, p.number, p.year);
    const link = await this.link(p.fileId, p.series, p.number, p.year);
    await this.file(p.fileId, p.originalSha256);
    await this.scan(p.year, false);
    return {
      mode: this.c.mode,
      identity: {
        businessId: this.c.ramp.businessId,
        entityId: this.c.ramp.entityId,
        organizationNumber: this.c.fortnox.organizationNumber,
        tenantId: this.c.fortnox.tenantId,
      },
      revision: p.revision,
      paths: [
        "/vouchers/{series}/{number}?financialyear",
        "/vouchers?financialyear&page",
        "/voucherfileconnections/{fileid}?financialyear",
        "/archive?fileid",
      ],
      sourceHash: hash({ voucher: v, link, originalSha256: p.originalSha256 }),
      status: "observed",
      voucherRowsContract:
        "candidate-array-shape-observed-against-existing-probe",
      probe: {
        series: p.series,
        number: p.number,
        year: p.year,
        fileId: p.fileId,
      },
    };
  }
  async preflight(date: string) {
    const years = array(
      object(
        await this.get(
          "fortnox",
          "/financialyears?date=" + encodeURIComponent(date),
        ),
      ).FinancialYears,
    )
      .map(object)
      .filter((y) => text(y.FromDate) <= date && text(y.ToDate) >= date);
    requireThat(years.length === 1, "exact-financial-year-required");
    const year = integer(years[0].Id);
    const series = object(
      object(
        await this.get(
          "fortnox",
          `/voucherseries/${this.c.mapping.series}?financialyear=${year}&financialyeardate=${date}`,
        ),
      ).VoucherSeries,
    );
    requireThat(series.Code === this.c.mapping.series, "owned-series-required");
    const accounts = [];
    for (const number of [
      this.c.mapping.expenseAccount,
      this.c.mapping.vatAccount,
      this.c.mapping.liabilityAccount,
    ]) {
      const account = object(
        object(
          await this.get(
            "fortnox",
            `/accounts/${number}?financialyear=${year}`,
          ),
        ).Account,
      );
      requireThat(
        integer(account.Number) === number && account.Active === true,
        "active-posting-account-required",
      );
      accounts.push({
        Number: account.Number,
        Active: account.Active,
        Description: account.Description ?? null,
        VATCode: account.VATCode ?? null,
      });
    }
    return {
      year,
      financialYear: {
        Id: years[0].Id,
        FromDate: years[0].FromDate,
        ToDate: years[0].ToDate,
      },
      series: {
        Code: series.Code,
        Manual: series.Manual ?? null,
        Description: series.Description ?? null,
      },
      accounts,
    };
  }
  async source(
    transactionId: string,
    e: Evidence,
    allowSynced = false,
  ): Promise<{ snapshot: Snapshot; bytes: Buffer; year: number }> {
    const identity = await this.identities();
    const t = object(
      await this.get(
        "ramp",
        "/developer/v1/transactions/" + encodeURIComponent(transactionId),
      ),
    );
    try {
      Schema.decodeUnknownSync(TransactionWire)(t);
    } catch {
      throw new Gate("unsupported-Ramp-transaction-shape");
    }
    requireThat(t.id === transactionId, "transaction-identity-mismatch");
    requireThat(
      t.sync_status === "SYNC_READY" ||
        (allowSynced && t.sync_status === "SYNCED"),
      "transaction-not-ready",
    );
    const ids = array(t.receipts);
    requireThat(ids.length === 1, "one-linked-receipt-required");
    const raw = object(
      await this.get(
        "ramp",
        "/developer/v1/receipts/" +
          encodeURIComponent(text(ids[0])) +
          "?include_ocr_data=true",
      ),
    );
    try {
      Schema.decodeUnknownSync(ReceiptWire)(raw);
    } catch {
      throw new Gate("unsupported-Ramp-receipt-shape");
    }
    const bytes = await this.original(text(raw.receipt_url));
    const receipt = { ...raw };
    delete receipt.receipt_url;
    const preflight = await this.preflight(
      text(t.accounting_date).slice(0, 10),
    );
    const capabilities = await this.capabilities();
    return {
      snapshot: {
        transaction: t,
        receipt,
        originalSha256: bytesHash(bytes),
        identity,
        preflight,
        capabilities,
        binding: this.c.binding,
        mapping: this.c.mapping,
        evidence: e,
      },
      bytes,
      year: preflight.year,
    };
  }
  matches(v: ReturnType<typeof object>, p: Plan): boolean {
    const wanted = p.posting.Voucher;
    try {
      if (
        integer(v.Year) !== p.year ||
        v.VoucherSeries !== wanted.VoucherSeries ||
        v.TransactionDate !== wanted.TransactionDate ||
        v.Description !== wanted.Description
      )
        return false;
      const rows = array(v.VoucherRows);
      if (rows.length !== wanted.VoucherRows.length) return false;
      return rows.every((r, i) => {
        rowSupported(r);
        const a = object(r),
          w = wanted.VoucherRows[i];
        return (
          integer(a.Account) === w.Account &&
          minor(a.Debit, 2) === minor(w.Debit, 2) &&
          minor(a.Credit, 2) === minor(w.Credit, 2)
        );
      });
    } catch {
      return false;
    }
  }
  async scan(year: number, checkKnown = true) {
    const vouchers: ReturnType<typeof object>[] = [];
    let total: number | undefined;
    const seen = new Set<number>();
    for (let page = 1; page <= 10000; page++) {
      const listing = object(
          await this.get(
            "fortnox",
            `/vouchers?financialyear=${year}&voucherseries=${this.c.mapping.series}&page=${page}`,
          ),
        ),
        meta = object(listing.MetaInformation);
      const pages = integer(meta["@TotalPages"]);
      requireThat(
        pages >= 1 &&
          pages <= 10000 &&
          (total === undefined || pages === total) &&
          integer(meta["@CurrentPage"]) === page,
        "incomplete-series-pagination",
      );
      total = pages;
      for (const summary of array(listing.Vouchers).map(object)) {
        requireThat(
          summary.VoucherSeries === this.c.mapping.series,
          "wrong-series-summary",
        );
        const number = integer(summary.VoucherNumber);
        requireThat(
          number > 0 && !seen.has(number),
          "duplicate-series-listing",
        );
        seen.add(number);
        vouchers.push(await this.voucher(this.c.mapping.series, number, year));
      }
      if (page === total) break;
    }
    if (checkKnown) {
      const known = await this.store.known(),
        markers = new Set<string>();
      for (const v of vouchers) {
        const marker = text(v.Description),
          plan = known.get(marker);
        requireThat(
          plan && !markers.has(marker) && this.matches(v, plan),
          "owned-series-foreign-duplicate-or-mismatch",
        );
        markers.add(marker);
      }
    }
    return vouchers;
  }
  async write(
    provider: "ramp" | "fortnox",
    path: string,
    body: string | FormData,
    assertAlive: () => Promise<void>,
  ) {
    const token = await this.grant(provider);
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
    };
    if (typeof body === "string") headers["Content-Type"] = "application/json";
    await assertAlive();
    return this.run(
      this.request(this.c[provider].origin + path, {
        method: "POST",
        headers,
        body,
      }),
    );
  }
  create(p: Plan, assertAlive: () => Promise<void>) {
    return this.write(
      "fortnox",
      "/vouchers?financialyear=" + p.year,
      wirePosting(p.posting),
      assertAlive,
    );
  }
  upload(p: Plan, assertAlive: () => Promise<void>) {
    const form = new FormData();
    const e = p.source.evidence.receipt;
    form.set(
      "file",
      new Blob([new Uint8Array(Buffer.from(p.originalBase64, "base64"))], {
        type: e.contentType,
      }),
      e.filename,
    );
    return this.write("fortnox", "/inbox?path=Inbox_v", form, assertAlive);
  }
}
