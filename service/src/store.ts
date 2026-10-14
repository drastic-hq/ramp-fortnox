import pg from "pg";
import { drizzle } from "drizzle-orm/node-postgres";
import { sql } from "drizzle-orm";
import { randomUUID } from "node:crypto";
import {
  Config,
  Plan,
  Gate,
  requireThat,
  seal,
  unseal,
  restoredPlan,
  hash,
} from "./domain";

export const kinds = ["create", "upload", "link", "ack"] as const;
export type Kind = (typeof kinds)[number];
export type StepState =
  | "ready"
  | "intent"
  | "uncertain"
  | "confirmed"
  | "investigate";
export type Step = {
  kind: Kind;
  state: StepState;
  attempt_id: string | null;
  request_hash: string | null;
  remote: Record<string, unknown> | null;
  reason: string | null;
};
export class Store {
  readonly pool: pg.Pool;
  readonly db;
  readonly key: Buffer;
  constructor(readonly config: Config) {
    this.key = Buffer.from(process.env[config.keyEnv] ?? "", "base64");
    requireThat(
      this.key.length === 32,
      "external-32-byte-encryption-key-required",
    );
    this.pool = new pg.Pool({
      connectionString: config.databaseUrl,
      max: 2,
      connectionTimeoutMillis: config.timeoutMs,
      query_timeout: config.timeoutMs,
      options: `-c search_path=${config.schema}`,
    });
    this.db = drizzle(this.pool);
  }
  async close() {
    await this.pool.end();
  }
  async migrate() {
    await this.pool.query(`CREATE SCHEMA IF NOT EXISTS ${this.config.schema}`);
    const migration = await Bun.file(
      new URL("./migration.sql", import.meta.url),
    ).text();
    await this.db.transaction(async (tx) => {
      await tx.execute(
        sql`SELECT pg_advisory_xact_lock(hashtextextended(${this.config.schema + ":migration"},0))`,
      );
      await tx.execute(sql.raw(migration));
    });
  }
  async lock(
    name: string,
  ): Promise<{ assertAlive(): Promise<void>; close(): Promise<void> }> {
    const client = new pg.Client({
      connectionString: this.config.databaseUrl,
      connectionTimeoutMillis: this.config.timeoutMs,
      query_timeout: this.config.timeoutMs,
      application_name: `connector-lock-${this.config.schema}`,
    });
    let lost = false;
    client.on("error", () => {
      lost = true;
    });
    try {
      await client.connect();
      const result = await client.query(
        "SELECT pg_try_advisory_lock(hashtextextended($1,0)) AS acquired",
        [name],
      );
      requireThat(result.rows[0].acquired, "dispatch-busy");
    } catch (error) {
      await client.end().catch(() => {});
      throw error;
    }
    return {
      async assertAlive() {
        requireThat(!lost, "dispatch-session-lost");
        try {
          await client.query("SELECT 1");
        } catch {
          lost = true;
          throw new Gate("dispatch-session-lost");
        }
      },
      async close() {
        await client.end().catch(() => {});
      },
    };
  }
  async token(
    provider: string,
    identityHash: string,
  ): Promise<{ token: string; expiresAt: number } | null> {
    const r = await this.db.execute(
      sql`SELECT encrypted,expires_at,identity_hash FROM connector_tokens WHERE provider=${provider}`,
    );
    const row = r.rows[0];
    if (!row || row.identity_hash !== identityHash) return null;
    const value = unseal(
      String(row.encrypted),
      this.key,
      `token:${provider}:${identityHash}`,
    ) as { token: string };
    return {
      token: value.token,
      expiresAt: new Date(String(row.expires_at)).getTime(),
    };
  }
  async saveToken(
    provider: string,
    token: string,
    expiresAt: number,
    identityHash: string,
  ) {
    const encrypted = seal(
      { token },
      this.key,
      `token:${provider}:${identityHash}`,
    );
    await this.db.execute(
      sql`INSERT INTO connector_tokens(provider,encrypted,expires_at,identity_hash,generation) VALUES(${provider},${encrypted},${new Date(expiresAt)},${identityHash},1) ON CONFLICT(provider) DO UPDATE SET encrypted=excluded.encrypted,expires_at=excluded.expires_at,identity_hash=excluded.identity_hash,generation=connector_tokens.generation+1`,
    );
  }
  async register(plan: Plan) {
    const c = this.config;
    await this.db.transaction(async (tx) => {
      const encrypted = seal(plan, this.key, `plan:${plan.operationId}`);
      const insertion = await tx.execute(
        sql`INSERT INTO connector_operations(operation_id,business_id,entity_id,transaction_id,action,target,series) VALUES(${plan.operationId},${c.ramp.businessId},${c.ramp.entityId},${String(plan.source.transaction.id)},'card_purchase',${c.fortnox.organizationNumber + ":" + c.fortnox.tenantId},${c.mapping.series}) ON CONFLICT DO NOTHING RETURNING operation_id`,
      );
      if (insertion.rows.length === 0) {
        const existing = await tx.execute(
          sql`SELECT fingerprint FROM connector_plans WHERE operation_id=${plan.operationId}`,
        );
        requireThat(
          existing.rows[0]?.fingerprint === plan.fingerprint,
          "existing-immutable-plan-differs",
        );
        return;
      }
      await tx.execute(
        sql`INSERT INTO connector_plans(operation_id,fingerprint,encrypted) VALUES(${plan.operationId},${plan.fingerprint},${encrypted})`,
      );
      for (const kind of kinds)
        await tx.execute(
          sql`INSERT INTO connector_steps(operation_id,kind,state) VALUES(${plan.operationId},${kind},'ready')`,
        );
    });
  }
  async plan(operationId: string): Promise<Plan> {
    const r = await this.db.execute(
      sql`SELECT encrypted FROM connector_plans WHERE operation_id=${operationId}`,
    );
    requireThat(r.rows.length === 1, "operation-not-found");
    return restoredPlan(
      unseal(String(r.rows[0].encrypted), this.key, `plan:${operationId}`),
    );
  }
  async known(): Promise<Map<string, Plan>> {
    const r = await this.db.execute(
      sql`SELECT operation_id,encrypted FROM connector_plans`,
    );
    return new Map(
      r.rows.map((v) => [
        String(v.operation_id),
        restoredPlan(
          unseal(String(v.encrypted), this.key, `plan:${v.operation_id}`),
        ),
      ]),
    );
  }
  async steps(id: string): Promise<Step[]> {
    const r = await this.db.execute(
      sql`SELECT kind,state,attempt_id,request_hash,remote,reason FROM connector_steps WHERE operation_id=${id}`,
    );
    return r.rows as Step[];
  }
  async intent(id: string, kind: Kind, request: unknown): Promise<string> {
    const attempt = randomUUID(),
      requestHash = hash(request),
      encrypted = seal(request, this.key, `attempt:${attempt}`);
    await this.db.transaction(async (tx) => {
      const r = await tx.execute(
        sql`UPDATE connector_steps SET state='intent',attempt_id=${attempt},request_hash=${requestHash},reason=NULL WHERE operation_id=${id} AND kind=${kind} AND state='ready' RETURNING kind`,
      );
      requireThat(r.rows.length === 1, "step-intent-conflict");
      await tx.execute(
        sql`INSERT INTO connector_attempts(attempt_id,operation_id,kind,request_hash,encrypted) VALUES(${attempt},${id},${kind},${requestHash},${encrypted})`,
      );
    });
    return attempt;
  }
  async transition(
    id: string,
    step: Step,
    state: StepState,
    remote: unknown,
    reason: string | null = null,
  ) {
    const r = await this.db.execute(
      sql`UPDATE connector_steps SET state=${state},remote=${JSON.stringify(remote)}::jsonb,reason=${reason} WHERE operation_id=${id} AND kind=${step.kind} AND attempt_id=${step.attempt_id}::uuid AND state IN ('intent','uncertain') RETURNING kind`,
    );
    requireThat(r.rows.length === 1, "attempt-compare-and-set-conflict");
  }
  async investigate(id: string, reason: string) {
    await this.db.execute(
      sql`UPDATE connector_operations SET reason=${reason} WHERE operation_id=${id}`,
    );
  }
  async result(id: string) {
    const plan = await this.plan(id),
      steps = await this.steps(id),
      r = await this.db.execute(
        sql`SELECT reason FROM connector_operations WHERE operation_id=${id}`,
      );
    const state =
      r.rows[0]?.reason || steps.some((s) => s.state === "investigate")
        ? "investigate"
        : steps.every((s) => s.state === "confirmed")
          ? "complete"
          : steps.some((s) => s.state === "intent" || s.state === "uncertain")
            ? "uncertain"
            : "planned";
    return {
      state,
      operationId: id,
      fingerprint: plan.fingerprint,
      posting: plan.posting,
      receiptSha256: plan.source.originalSha256,
      steps,
      reason: r.rows[0]?.reason ?? null,
      mode: plan.mode,
    };
  }
}
