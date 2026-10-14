import { Effect } from "effect";
import { mkdir, writeFile, lstat, mkdtemp, rename, rm } from "node:fs/promises";
import { join } from "node:path";
import {
  Config,
  Evidence,
  Plan,
  Gate,
  requireThat,
  purchase,
  hash,
  materialConfig,
  object,
  text,
  integer,
} from "./domain";
import { Providers, ProviderFailure } from "./providers";
import { Store, Step, Kind, kinds } from "./store";

export class StorageFailure extends Error {
  constructor() {
    super("storage-operation-failed");
  }
}
export type Failure = Gate | ProviderFailure | StorageFailure;
export function task<A>(run: () => Promise<A>): Effect.Effect<A, Failure> {
  return Effect.tryPromise({
    try: run,
    catch: (error) =>
      error instanceof Gate || error instanceof ProviderFailure
        ? error
        : new StorageFailure(),
  });
}
export class Connector {
  readonly providers: Providers;
  constructor(
    readonly config: Config,
    readonly store: Store,
  ) {
    this.providers = new Providers(config, store);
  }
  connect() {
    return task(async () => {
      await this.providers.identities();
      const identity = {
        businessId: this.config.ramp.businessId,
        entityId: this.config.ramp.entityId,
        organizationNumber: this.config.fortnox.organizationNumber,
        tenantId: this.config.fortnox.tenantId,
      };
      const capabilities = await this.providers.capabilities();
      return {
        state: "connected",
        mode: this.config.mode,
        identity,
        capabilities,
      };
    });
  }
  inspect(transactionId: string, evidence: Evidence) {
    return task(async () => {
      const s = await this.providers.source(transactionId, evidence);
      const p = purchase(this.config, s.snapshot, s.bytes, s.year);
      return {
        state: "eligible",
        operationId: p.operationId,
        fingerprint: p.fingerprint,
        posting: p.posting,
        receiptSha256: p.source.originalSha256,
        capabilities: p.source.capabilities,
      };
    });
  }
  plan(transactionId: string, evidence: Evidence) {
    return task(async () => {
      const s = await this.providers.source(transactionId, evidence);
      const p = purchase(this.config, s.snapshot, s.bytes, s.year);
      await this.store.register(p);
      return this.store.result(p.operationId);
    });
  }
  status(operationId: string, proof?: string) {
    return task(async () => {
      const result = await this.store.result(operationId);
      if (proof) {
        await mkdir(proof, { recursive: true });
        const directory = await lstat(proof);
        requireThat(
          directory.isDirectory() && !directory.isSymbolicLink(),
          "proof-directory-must-be-regular",
        );
        const destination = join(proof, "status.json");
        try {
          const existing = await lstat(destination);
          requireThat(
            existing.isFile() &&
              !existing.isSymbolicLink() &&
              existing.nlink === 1,
            "proof-destination-must-be-unlinked-regular-file",
          );
        } catch (error) {
          if (
            !(
              error instanceof Error &&
              "code" in error &&
              error.code === "ENOENT"
            )
          )
            throw error;
        }
        const stage = await mkdtemp(join(proof, ".status-stage-"));
        try {
          const staged = join(stage, "status.json");
          await writeFile(staged, JSON.stringify(result, null, 2), {
            mode: 0o600,
            flag: "wx",
          });
          await rename(staged, destination);
        } finally {
          await rm(stage, { recursive: true, force: true });
        }
      }
      return result;
    });
  }
  private async frozen(plan: Plan, steps: Step[]) {
    requireThat(
      hash(materialConfig(this.config)) === plan.configHash,
      "stale-config-or-binding",
    );
    const ack = steps.find((s) => s.kind === "ack")!;
    const mayBeSynced =
      ack.state !== "ready" &&
      steps
        .filter((s) => s.kind !== "ack")
        .every((s) => s.state === "confirmed");
    const current = await this.providers.source(
      text(plan.source.transaction.id),
      plan.source.evidence,
      mayBeSynced,
    );
    if (mayBeSynced && current.snapshot.transaction.sync_status === "SYNCED") {
      current.snapshot.transaction = {
        ...current.snapshot.transaction,
        sync_status: plan.source.transaction.sync_status,
        updated_at: plan.source.transaction.updated_at,
      };
      if ("synced_at" in plan.source.transaction)
        current.snapshot.transaction.synced_at =
          plan.source.transaction.synced_at;
      else delete current.snapshot.transaction.synced_at;
    }
    const proposed = purchase(
      this.config,
      current.snapshot,
      current.bytes,
      current.year,
    );
    requireThat(
      proposed.fingerprint === plan.fingerprint,
      "stale-source-or-approval",
    );
  }
  private step(steps: Step[], kind: Kind): Step {
    const step = steps.find((s) => s.kind === kind);
    requireThat(step, "missing-durable-step");
    return step;
  }
  private async attachment(plan: Plan, steps: Step[]) {
    const create = this.step(steps, "create"),
      upload = this.step(steps, "upload");
    requireThat(
      create.state === "confirmed" &&
        upload.state === "confirmed" &&
        create.remote &&
        upload.remote,
      "receipt-prerequisites-required",
    );
    const number = integer(create.remote.number),
      fileId = text(upload.remote.fileId);
    const voucher = await this.providers.voucher(
      this.config.mapping.series,
      number,
      plan.year,
    );
    requireThat(
      this.providers.matches(voucher, plan),
      "confirmed-voucher-changed",
    );
    await this.providers.file(fileId, plan.source.originalSha256);
    return this.providers.link(
      fileId,
      this.config.mapping.series,
      number,
      plan.year,
    );
  }
  private request(kind: Kind, plan: Plan, steps: Step[]) {
    if (kind === "create")
      return {
        path: "/vouchers?financialyear=" + plan.year,
        body: plan.posting,
      };
    if (kind === "upload")
      return {
        path: "/inbox?path=Inbox_v",
        originalSha256: plan.source.originalSha256,
        filename: plan.source.evidence.receipt.filename,
        contentType: plan.source.evidence.receipt.contentType,
      };
    const create = this.step(steps, "create"),
      upload = this.step(steps, "upload");
    requireThat(create.remote, "known-voucher-required");
    const number = integer(create.remote.number);
    if (kind === "link") {
      requireThat(upload.remote, "known-original-file-required");
      return {
        path: "/voucherfileconnections?financialyear=" + plan.year,
        body: {
          VoucherFileConnection: {
            FileId: text(upload.remote.fileId),
            VoucherSeries: this.config.mapping.series,
            VoucherNumber: number,
          },
        },
      };
    }
    return {
      path: "/developer/v1/accounting/syncs",
      body: {
        idempotency_key: plan.operationId + ":ack",
        sync_type: "TRANSACTION_SYNC",
        mark_ready_to_sync: false,
        successful_syncs: [
          {
            id: text(plan.source.transaction.id),
            reference_id: `${plan.year}:${this.config.mapping.series}:${number}`,
          },
        ],
      },
    };
  }
  private async reconcileStep(
    plan: Plan,
    step: Step,
    steps: Step[],
    assertAlive: () => Promise<void>,
  ): Promise<boolean> {
    if (step.kind === "create") {
      const all = await this.providers.scan(plan.year);
      const candidates = all.filter((v) => v.Description === plan.operationId);
      if (candidates.length === 1) {
        await assertAlive();
        await this.store.transition(plan.operationId, step, "confirmed", {
          number: integer(candidates[0].VoucherNumber),
          series: this.config.mapping.series,
          year: plan.year,
        });
        return true;
      }
      return false;
    }
    if (step.kind === "upload") return false;
    if (step.kind === "link") {
      try {
        await this.attachment(plan, steps);
      } catch (error) {
        if (error instanceof ProviderFailure && error.status === 404)
          return false;
        throw error;
      }
      await assertAlive();
      await this.store.transition(plan.operationId, step, "confirmed", {
        fileId: this.step(steps, "upload").remote!.fileId,
      });
      return true;
    }
    return true;
  }
  apply(
    operationId: string,
    fingerprint: string,
    observeOnly = false,
  ): Effect.Effect<
    | Awaited<ReturnType<Store["result"]>>
    | { state: string; operationId: string; reason: string },
    Failure
  > {
    const acquire = task(async () => {
      const plan = await this.store.plan(operationId);
      requireThat(
        plan.fingerprint === fingerprint,
        "exact-reviewed-fingerprint-required",
      );
      requireThat(
        plan.configHash === hash(materialConfig(this.config)),
        "stale-config-or-binding",
      );
      const lock = await this.store.lock(
        `dispatch:${this.config.fortnox.organizationNumber}:${this.config.fortnox.tenantId}:${plan.year}:${this.config.mapping.series}`,
      );
      return { plan, lock };
    });
    return Effect.acquireUseRelease(
      acquire,
      ({ plan, lock }) =>
        task(async () => {
          try {
            let steps = await this.store.steps(operationId);
            for (const step of steps.filter((s) => s.state === "intent"))
              await this.store.transition(
                operationId,
                step,
                "uncertain",
                step.remote,
                "prior-intent-may-have-executed",
              );
            steps = await this.store.steps(operationId);
            const prior = await this.store.result(operationId);
            if (prior.state === "investigate") return prior;
            await this.frozen(plan, steps);
            await this.providers.scan(plan.year);
            if (prior.state === "complete") {
              await this.attachment(plan, steps);
              return prior;
            }
            for (const kind of kinds) {
              await lock.assertAlive();
              steps = await this.store.steps(operationId);
              let step = this.step(steps, kind);
              if (step.state === "confirmed") continue;
              if (step.state === "uncertain") {
                if (
                  !(await this.reconcileStep(plan, step, steps, () =>
                    lock.assertAlive(),
                  ))
                )
                  return this.store.result(operationId);
                steps = await this.store.steps(operationId);
                step = this.step(steps, kind);
                if (step.state === "confirmed") continue;
              }
              if (observeOnly) return this.store.result(operationId);
              await this.frozen(plan, steps);
              if (kind === "ack") await this.attachment(plan, steps);
              const request = this.request(kind, plan, steps);
              if (step.state === "ready") {
                const attempt = await this.store.intent(
                  operationId,
                  kind,
                  request,
                );
                step = {
                  ...step,
                  state: "intent",
                  attempt_id: attempt,
                  request_hash: hash(request),
                };
              }
              requireThat(
                step.request_hash === hash(request),
                "frozen-step-request-changed",
              );
              if (
                this.config.mode === "synthetic" &&
                process.env.CONNECTOR_TEST_CRASH_AFTER_INTENT === kind
              ) {
                console.log(
                  JSON.stringify({
                    state: "uncertain",
                    operationId,
                    reason: "synthetic-process-death-after-intent",
                  }),
                );
                process.exit(91);
              }
              try {
                await lock.assertAlive();
                await this.providers.identities();
                await lock.assertAlive();
                let response: unknown;
                if (kind === "create")
                  response = await this.providers.create(plan, () =>
                    lock.assertAlive(),
                  );
                else if (kind === "upload")
                  response = await this.providers.upload(plan, () =>
                    lock.assertAlive(),
                  );
                else
                  response = await this.providers.write(
                    kind === "ack" ? "ramp" : "fortnox",
                    request.path,
                    JSON.stringify("body" in request ? request.body : null),
                    () => lock.assertAlive(),
                  );
                await lock.assertAlive();
                let remote: Record<string, unknown>;
                if (kind === "create") {
                  const voucher = object(object(response).Voucher);
                  requireThat(
                    this.providers.matches(voucher, plan),
                    "created-voucher-mismatch",
                  );
                  const all = await this.providers.scan(plan.year);
                  const candidates = all.filter(
                    (v) => v.Description === plan.operationId,
                  );
                  requireThat(
                    candidates.length === 1 &&
                      integer(candidates[0].VoucherNumber) ===
                        integer(voucher.VoucherNumber),
                    "create-readback-attribution-mismatch",
                  );
                  remote = {
                    number: integer(voucher.VoucherNumber),
                    series: this.config.mapping.series,
                    year: plan.year,
                  };
                } else if (kind === "upload") {
                  const file = object(object(response).File),
                    fileId = text(file.Id);
                  await this.providers.file(fileId, plan.source.originalSha256);
                  remote = { fileId };
                } else if (kind === "link") {
                  await this.attachment(plan, steps);
                  remote = {
                    fileId: this.step(steps, "upload").remote!.fileId,
                  };
                } else {
                  const ack = object(response);
                  remote = { syncId: text(ack.sync_id) };
                  const refreshed = await this.providers.get(
                    "ramp",
                    "/developer/v1/transactions/" +
                      encodeURIComponent(text(plan.source.transaction.id)),
                  );
                  requireThat(
                    object(refreshed).sync_status === "SYNCED",
                    "ack-not-observed",
                  );
                  await this.frozen(
                    plan,
                    steps.map((s) => (s.kind === "ack" ? step : s)),
                  );
                  await this.attachment(plan, steps);
                }
                await lock.assertAlive();
                await this.store.transition(
                  operationId,
                  step,
                  "confirmed",
                  remote,
                );
              } catch (error) {
                if (error instanceof StorageFailure) throw error;
                await this.store.transition(
                  operationId,
                  step,
                  "uncertain",
                  null,
                  error instanceof Gate
                    ? error.code
                    : "external-write-may-have-executed",
                );
                if (
                  error instanceof Gate &&
                  error.code !== "dispatch-session-lost"
                )
                  await this.store.investigate(operationId, error.code);
                return this.store.result(operationId);
              }
            }
            return this.store.result(operationId);
          } catch (error) {
            if (error instanceof Gate) {
              if (
                /owned-series|voucher-changed|attachment-readback|original-byte-readback|create-readback|fractional-minor/.test(
                  error.code,
                )
              ) {
                await this.store.investigate(operationId, error.code);
                return this.store.result(operationId);
              }
              return { state: "blocked", operationId, reason: error.code };
            }
            throw error;
          }
        }),
      ({ lock }) => Effect.promise(() => lock.close()),
    );
  }
}
