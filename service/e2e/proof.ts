import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createServer } from 'node:net';
import { createHash, randomBytes } from 'node:crypto';
import pg from 'pg';

const root = resolve(import.meta.dir, '..');
const fixture = JSON.parse(await readFile(join(root, '../fixtures/purchase.json'), 'utf8'));
const expected = JSON.parse(await readFile(join(import.meta.dir, 'expected-posting.json'), 'utf8'));
const original = Buffer.from(fixture.evidence.receipt.original_base64, 'base64');
const artifact = resolve(process.env.PROOF_DIR ?? join(root, 'artifacts/proof'));
const local = await mkdtemp(join(tmpdir(), 'ramp-service-proof-'));
await mkdir(artifact, { recursive: true });
const pgBin = process.env.PG_BIN ?? '/opt/homebrew/opt/postgresql@17/bin';
let ownedCluster = false;
let databaseUrl = process.env.RAMP_TEST_DATABASE_URL;
const transcripts: unknown[] = [], results: unknown[] = [];
async function shell(cmd: string[]) {
  const child = Bun.spawn(cmd, { stdout: 'pipe', stderr: 'pipe' });
  const out = await new Response(child.stdout).text(), err = await new Response(child.stderr).text();
  assert.equal(await child.exited, 0, `${cmd[0]} failed ${err}`);
  return out;
}
if (!databaseUrl) {
  const port = await new Promise<number>(r => { const s = createServer(); s.listen(0, '127.0.0.1', () => { const a = s.address(); assert(a && typeof a !== 'string'); s.close(() => r(a.port)); }); });
  await shell([join(pgBin, 'initdb'), '-D', join(local, 'pg'), '-A', 'trust', '-U', 'proof']);
  await shell([join(pgBin, 'pg_ctl'), '-D', join(local, 'pg'), '-l', join(local, 'postgres.log'), '-o', `-h 127.0.0.1 -p ${port} -k ${local}`, '-w', 'start']);
  ownedCluster = true;
  databaseUrl = `postgres://proof@127.0.0.1:${port}/postgres`;
}
const control = new pg.Client({ connectionString: databaseUrl });
await control.connect();
const baseConfig = {
  mode: 'synthetic', databaseUrl, keyEnv: 'CONNECTOR_KEY',
  ramp: { origin: '', tokenUrl: '', clientIdEnv: 'RAMP_CLIENT_ID', clientSecretEnv: 'RAMP_CLIENT_SECRET', businessId: fixture.config.ramp_business_id, entityId: fixture.config.ramp_entity_id, providerName: fixture.config.provider_name },
  fortnox: { origin: '', tokenUrl: '', clientIdEnv: 'FORTNOX_CLIENT_ID', clientSecretEnv: 'FORTNOX_CLIENT_SECRET', tenantId: '42', organizationNumber: fixture.config.fortnox_company_id, consentReference: 'synthetic-service-consent' },
  binding: { sourceReference: 'synthetic-reviewed-binding', entityId: fixture.config.ramp_entity_id, organizationNumber: fixture.config.fortnox_company_id },
  mapping: { revision: 'synthetic-map-1', expenseAccount: 5410, vatAccount: 2641, liabilityAccount: 2899, taxCode: 'SE25', series: 'R' },
  receiptHosts: [] as string[], timeoutMs: 1500, maxReceiptBytes: 1048576,
  probe: { series: 'P', number: 9, year: 1, fileId: 'probe-original', originalSha256: fixture.evidence.receipt.sha256, revision: 'synthetic-contract-1' },
};
const env = { ...process.env, CONNECTOR_TEST_MODE: 'loopback-only', CONNECTOR_KEY: randomBytes(32).toString('base64'), RAMP_CLIENT_ID: 'synthetic-ramp', RAMP_CLIENT_SECRET: 'secret-ramp', FORTNOX_CLIENT_ID: 'synthetic-fortnox', FORTNOX_CLIENT_SECRET: 'secret-fortnox' };
type State = { fault: string; transaction: any; receipt: any; vouchers: any[]; files: Map<string, Buffer>; links: any[]; counts: Record<string, number>; tokenCalls: number; downloadsAuthenticated: number };
let state: State;
let schema = '';
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, async fetch(req) {
  const u = new URL(req.url), path = u.pathname;
  const json = (v: unknown, status = 200) => Response.json(v, { status });
  const drop = () => new Response('response deliberately lost', { status: 503 });
  if (path.endsWith('/token')) { state.tokenCalls++; return state.fault === 'denied' ? json({ error: 'access_denied' }, 401) : json({ access_token: 'synthetic-token', expires_in: state.fault === 'expiry' ? 1 : 3600, token_type: 'bearer' }); }
  if (path === '/developer/v1/business') return json({ id: fixture.config.ramp_business_id });
  if (path.startsWith('/developer/v1/entities/')) return json({ id: fixture.config.ramp_entity_id, currency: 'SEK', entity_name: 'Synthetic entity' });
  if (path === '/developer/v1/accounting/all-connections') return json({ data: [{ id: 'connection-1', is_active: true, remote_provider_name: fixture.config.provider_name, status: 'ACTIVE' }] });
  if (path.startsWith('/developer/v1/transactions/')) { state.counts.sourceReads++; return json(state.transaction); }
  if (path.startsWith('/developer/v1/receipts/')) return json({ ...state.receipt, receipt_url: `${server.url}original` });
  if (path === '/original') { if (req.headers.has('authorization')) state.downloadsAuthenticated++; return state.fault === 'redirect' ? new Response(null, { status: 302, headers: { location: 'https://invalid.example/original' } }) : new Response(original); }
  if (path === '/3/companyinformation') return json({ CompanyInformation: { OrganizationNumber: state.fault === 'identity' ? 'wrong' : fixture.config.fortnox_company_id, DatabaseNumber: 42 } });
  if (path === '/3/financialyears') return json({ FinancialYears: [{ Id: 1, FromDate: '2026-01-01', ToDate: '2026-12-31' }] });
  if (path.startsWith('/3/accounts/')) return json({ Account: { Number: Number(path.split('/').at(-1)), Active: true } });
  if (path.startsWith('/3/voucherseries/')) return json({ VoucherSeries: { Code: 'R' } });
  if (path === '/3/archive') { if (state.fault === 'no-readback') return json({ File: { Id: 'metadata-only' } }); return new Response(state.files.get(u.searchParams.get('fileid')!) ?? original); }
  if (path.startsWith('/3/voucherfileconnections/') && req.method === 'GET') {
    const id = path.split('/').at(-1)!;
    const link = id === 'probe-original' ? { FileId: id, VoucherSeries: 'P', VoucherNumber: 9, VoucherYear: 1 } : state.links.find(l => l.FileId === id);
    return link ? json({ VoucherFileConnection: link }) : json({ error: 'not found' }, 404);
  }
  if (path.startsWith('/3/vouchers/') && req.method === 'GET') {
    const series = path.split('/')[3], number = Number(path.split('/')[4]);
    if (series === 'P') return json({ Voucher: { ...expected.Voucher, VoucherSeries: 'P', Description: 'existing probe', VoucherNumber: 9, Year: 1 } });
    const voucher = state.vouchers.find(v => v.VoucherNumber === number);
    return voucher ? json({ Voucher: voucher }) : json({ error: 'missing' }, 404);
  }
  if (path === '/3/vouchers' && req.method === 'GET') return json({ Vouchers: state.vouchers.map(v => ({ VoucherNumber: v.VoucherNumber, VoucherSeries: v.VoucherSeries })), MetaInformation: state.fault === 'incomplete' ? {} : { '@TotalPages': 1, '@CurrentPage': 1 } });
  if (path === '/3/vouchers' && req.method === 'POST') {
    state.counts.create++;
    const body = await req.json() as any;
    assert.deepEqual(body, expected, 'independent literal voucher oracle');
    const v = { ...body.Voucher, VoucherNumber: state.vouchers.length + 1, Year: 1 };
    if (state.fault === 'absent') return drop();
    if (state.fault === 'late') { setTimeout(() => state.vouchers.push(v), 400); return drop(); }
    state.vouchers.push(v);
    if (state.fault === 'duplicate') state.vouchers.push({ ...v, VoucherNumber: 2 });
    if (state.fault === 'session-loss') { await control.query(`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = $1`, [`connector-lock-${schema}`]); return json({ Voucher: v }); }
    return ['create-lost', 'duplicate'].includes(state.fault) ? drop() : json({ Voucher: v }, 201);
  }
  if (path === '/3/inbox' && req.method === 'POST') {
    state.counts.upload++;
    const form = await req.formData(), file = form.get('file');
    assert(file instanceof File); const bytes = Buffer.from(await file.arrayBuffer()); assert.deepEqual(bytes, original);
    state.files.set('file-1', bytes);
    return state.fault === 'upload-lost' ? drop() : json({ File: { Id: 'file-1', Name: file.name, Size: bytes.length } }, 201);
  }
  if (path === '/3/voucherfileconnections' && req.method === 'POST') {
    state.counts.link++; const b = await req.json() as any; const l = { ...b.VoucherFileConnection, VoucherYear: 1 }; state.links.push(l);
    return state.fault === 'link-lost' ? drop() : json({ VoucherFileConnection: l }, 201);
  }
  if (path === '/developer/v1/accounting/syncs' && req.method === 'POST') {
    const b = await req.json() as any; assert.equal(b.idempotency_key, expected.Voucher.Description + ':ack');
    if (state.transaction.sync_status !== 'SYNCED') state.counts.ack++;
    state.transaction.sync_status = 'SYNCED'; state.transaction.updated_at = '2026-10-01T14:00:00Z';
    return state.fault === 'ack-lost' ? drop() : json({ sync_id: '55555555-5555-4555-8555-555555555555' }, 201);
  }
  return json({ error: `unimplemented fixture path ${path}` }, 404);
} });
async function command(config: string, args: string[], extra: Record<string, string> = {}) {
  const cmd = [process.execPath, 'run', 'src/cli.ts', ...args, '--config', config];
  const child = Bun.spawn(cmd, { cwd: root, env: { ...env, ...extra }, stdout: 'pipe', stderr: 'pipe' });
  const out = await new Response(child.stdout).text(), stderr = await new Response(child.stderr).text(); const exit = await child.exited;
  transcripts.push({ args, exit, output: out.trim(), stderr: stderr.trim() });
  let value: any; try { value = JSON.parse(out); } catch { throw new Error(`CLI emitted invalid JSON ${out} ${stderr}`); }
  return { ...value, exit };
}
async function setup(fault: string) {
  schema = `proof_${randomBytes(6).toString('hex')}`; await control.query(`CREATE SCHEMA ${schema}`);
  state = { fault, transaction: structuredClone(fixture.transaction), receipt: structuredClone(fixture.receipt), vouchers: [], files: new Map([['probe-original', original]]), links: [], counts: { create: 0, upload: 0, link: 0, ack: 0, sourceReads: 0 }, tokenCalls: 0, downloadsAuthenticated: 0 };
  const config = { ...baseConfig, schema, ramp: { ...baseConfig.ramp, origin: server.url.origin, tokenUrl: `${server.url}ramp/token` }, fortnox: { ...baseConfig.fortnox, origin: `${server.url.origin}/3`, tokenUrl: `${server.url}fortnox/token` }, receiptHosts: [server.url.origin] };
  const file = join(local, `${schema}.json`), evidence = join(local, `${schema}-evidence.json`);
  await writeFile(file, JSON.stringify(config)); await writeFile(evidence, JSON.stringify({ ...fixture.evidence, mode: 'synthetic', businessId: fixture.config.ramp_business_id, entityId: fixture.config.ramp_entity_id }));
  return { config: file, evidence, cfg: config };
}
async function planned(s: Awaited<ReturnType<typeof setup>>) {
  const c = await command(s.config, ['connect']); assert.equal(c.state, 'connected', JSON.stringify(c));
  const p = await command(s.config, ['plan', '--transaction', fixture.transaction.id, '--evidence', s.evidence]); assert.equal(p.state, 'planned', JSON.stringify(p)); assert.deepEqual(p.posting, expected); return p;
}
async function projection() { return (await control.query(`SELECT kind,state,attempt_id,request_hash,remote FROM ${schema}.connector_steps ORDER BY kind`)).rows; }
async function record(name: string, outcome: any) {
  assert.equal(state.downloadsAuthenticated, 0);
  const rows = await projection(); results.push({ name, outcome, rows, counts: state.counts, vouchers: state.vouchers, receiptSha256: createHash('sha256').update(original).digest('hex') });
  await control.query(`DROP SCHEMA ${schema} CASCADE`);
}
try {
  for (const fault of ['identity', 'denied', 'no-readback', 'redirect']) {
    const s = await setup(fault); const c = await command(s.config, ['connect']);
    if (['identity', 'denied'].includes(fault)) assert.equal(c.state, 'blocked');
    else { const p = await command(s.config, ['plan', '--transaction', fixture.transaction.id, '--evidence', s.evidence]); assert.equal(p.state, 'blocked'); }
    assert.equal(state.counts.create, 0); await record(fault, c);
  }
  for (const fault of ['units', 'fraction', 'large-fraction']) {
    const s = await setup(''); const evidence = JSON.parse(await readFile(s.evidence, 'utf8'));
    if (fault === 'units') delete evidence.units_calibration;
    else state.receipt.ocr.taxes[0].tax_amount = fault === 'fraction' ? 250.001 : 900719925474099.1;
    await writeFile(s.evidence, JSON.stringify(evidence)); await command(s.config, ['connect']);
    const p = await command(s.config, ['plan', '--transaction', fixture.transaction.id, '--evidence', s.evidence]); assert.equal(p.state, 'blocked'); assert.equal(state.counts.create, 0); await record(fault, p);
  }
  for (const fault of ['happy', 'stale', 'mapping', 'crash', 'create-lost', 'absent', 'late', 'duplicate', 'incomplete', 'upload-lost', 'link-lost', 'ack-lost', 'ack-changed', 'competitors', 'session-loss', 'expiry']) {
    const s = await setup(''); const p = await planned(s); state.fault = fault;
    const args = ['apply', '--operation', p.operationId, '--fingerprint', p.fingerprint];
    if (fault === 'stale') state.transaction.updated_at = 'changed';
    if (fault === 'mapping') { s.cfg.mapping.revision = 'changed'; await writeFile(s.config, JSON.stringify(s.cfg)); }
    if (fault === 'expiry') { await control.query(`UPDATE ${schema}.connector_tokens SET expires_at=now()-interval '1 second'`); state.fault = 'denied'; }
    if (fault === 'ack-changed') state.fault = 'ack-lost';
    let a;
    if (fault === 'competitors') { const both = await Promise.all([command(s.config, args), command(s.config, args)]); a = both.find(v => v.state === 'complete'); assert(a); }
    else a = await command(s.config, args, fault === 'crash' ? { CONNECTOR_TEST_CRASH_AFTER_INTENT: 'create' } : {});
    if (fault === 'late') await new Promise(r => setTimeout(r, 550));
    if (fault === 'ack-changed') state.vouchers[0].VoucherRows[0].Debit = '999.00';
    if (['create-lost', 'late', 'link-lost', 'ack-lost', 'ack-changed', 'crash', 'absent', 'upload-lost', 'session-loss'].includes(fault)) { state.fault = ''; a = await command(s.config, ['worker', '--once', '--operation', p.operationId, '--fingerprint', p.fingerprint]); }
    if (['happy', 'create-lost', 'late', 'link-lost', 'ack-lost', 'competitors'].includes(fault)) { assert.equal(a.state, 'complete', JSON.stringify(a)); assert.equal(state.vouchers.length, 1); assert.equal(state.counts.create, 1); assert.equal(state.counts.upload, 1); assert.equal(state.counts.link, 1); assert.equal(state.counts.ack, 1); const replay = await command(s.config, args); assert.equal(replay.state, 'complete'); assert.equal(state.counts.create, 1); assert(state.counts.sourceReads >= 3); }
    if (['stale', 'mapping', 'incomplete', 'expiry'].includes(fault)) { assert(['blocked','investigate'].includes(a.state), JSON.stringify(a)); assert.equal(state.counts.create, 0); }
    if (fault === 'crash') { assert.equal(a.state, 'uncertain'); assert.equal(state.counts.create, 0); }
    if (['absent','upload-lost'].includes(fault)) { assert.equal(a.state, 'uncertain'); assert.equal(state.counts.create, 1); }
    if (fault === 'duplicate' || fault === 'ack-changed') { assert.equal(a.state, 'investigate'); assert.equal(state.counts.create, 1); }
    if (fault === 'session-loss') { assert.equal(state.counts.create, 1); }
    const status = await command(s.config, ['status', '--operation', p.operationId, '--proof', join(artifact, fault)]); assert.equal(status.operationId, p.operationId);
    const immutable = await control.query(`UPDATE ${schema}.connector_plans SET fingerprint='changed' WHERE operation_id=$1`, [p.operationId]).then(() => false, () => true); assert(immutable, 'immutable plan database constraint');
    await record(fault, a);
  }
  await writeFile(join(artifact, 'proof.json'), JSON.stringify({ mode: 'synthetic', command: 'cd service && bun run proof', expected, fixtureSha256: createHash('sha256').update(JSON.stringify(fixture)).digest('hex'), versions: { bun: Bun.version, postgres: (await control.query('SHOW server_version')).rows[0].server_version }, results, transcripts }, null, 2));
  console.log(JSON.stringify({ state: 'passed', scenarios: results.length, artifact: join(artifact, 'proof.json') }));
} finally {
  server.stop(true); await control.end();
  if (ownedCluster) await shell([join(pgBin, 'pg_ctl'), '-D', join(local, 'pg'), '-m', 'fast', '-w', 'stop']);
}
