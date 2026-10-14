from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .domain import canonical, digest, FrozenPlan, Refusal

STEPS = ('create', 'upload', 'connect', 'ack')


class Journal:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.database_path = (self.workspace / 'journal.sqlite').resolve()
        self.dispatcher_lock = Path(str(self.database_path) + '.lock')
        self.connection = sqlite3.connect(str(self.database_path), timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS operations (
              id TEXT PRIMARY KEY, business TEXT NOT NULL, entity TEXT NOT NULL,
              transaction_id TEXT NOT NULL, kind TEXT NOT NULL,
              plan_json TEXT NOT NULL, fingerprint TEXT NOT NULL, owner TEXT,
              UNIQUE(business, entity, transaction_id, kind));
            CREATE TABLE IF NOT EXISTS steps (
              operation_id TEXT NOT NULL REFERENCES operations(id), kind TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN
                ('ready','dispatching','confirmed','uncertain','repairable','investigate')),
              remote_json TEXT, reason TEXT, attempt_id INTEGER,
              PRIMARY KEY(operation_id,kind),
              CHECK(state != 'confirmed' OR remote_json IS NOT NULL));
            CREATE TABLE IF NOT EXISTS attempts (
              id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, step TEXT NOT NULL,
              owner TEXT NOT NULL, request_json TEXT NOT NULL, request_digest TEXT NOT NULL,
              preflight_json TEXT NOT NULL, started_at REAL NOT NULL,
              outcome_json TEXT, finished_at REAL);
            CREATE TABLE IF NOT EXISTS calls (
              id INTEGER PRIMARY KEY, operation_id TEXT, owner TEXT NOT NULL,
              method TEXT NOT NULL, path TEXT NOT NULL, request_json TEXT,
              started_at REAL NOT NULL, outcome_json TEXT, finished_at REAL);
        ''')
        self.owner = str(uuid.uuid4())

    @contextmanager
    def dispatcher(self, recover=True):
        with self.dispatcher_lock.open('a+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal('busy dispatcher owns this journal')
            try:
                if recover:
                    with self.connection:
                        self.connection.execute("UPDATE steps SET state=CASE WHEN kind='ack' "
                                                "THEN 'repairable' ELSE 'uncertain' END, "
                                                "reason='dispatcher stopped after durable intent' "
                                                "WHERE state='dispatching'")
                        self.connection.execute('UPDATE operations SET owner=NULL')
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def register(self, plan):
        config = plan.source['config']
        with self.connection:
            existing = self.connection.execute('SELECT fingerprint FROM operations WHERE id=?',
                                               (plan.operation_id,)).fetchone()
            if existing:
                if existing['fingerprint'] != plan.fingerprint:
                    raise Refusal('stale operation cannot replace its frozen plan')
                return
            self.connection.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,NULL)',
                                    (plan.operation_id, config['ramp_business_id'],
                                     config['ramp_entity_id'], plan.source['transaction']['id'],
                                     'card_purchase', canonical(plan.as_dict()), plan.fingerprint))
            self.connection.executemany('INSERT INTO steps(operation_id,kind,state) VALUES(?,?,?)',
                                        [(plan.operation_id, kind, 'ready') for kind in STEPS])

    def plan(self, operation_id=None):
        if operation_id:
            row = self.connection.execute('SELECT plan_json FROM operations WHERE id=?',
                                          (operation_id,)).fetchone()
        else:
            row = self.connection.execute('SELECT plan_json FROM operations ORDER BY rowid DESC LIMIT 1').fetchone()
        if not row:
            raise Refusal('no frozen plan exists; run plan first')
        return FrozenPlan.restore(json.loads(row['plan_json']))

    def steps(self, operation_id):
        rows = self.connection.execute('SELECT * FROM steps WHERE operation_id=?',
                                       (operation_id,)).fetchall()
        return {row['kind']: {'state': row['state'],
                              'remote': json.loads(row['remote_json']) if row['remote_json'] else None,
                              'reason': row['reason']} for row in rows}

    def result(self, operation_id):
        steps = self.steps(operation_id)
        states = [steps[kind]['state'] for kind in STEPS]
        if all(state == 'confirmed' for state in states):
            state = 'complete'
        elif 'investigate' in states:
            state = 'investigate'
        elif 'dispatching' in states:
            state = 'dispatching'
        elif 'uncertain' in states:
            state = 'uncertain'
        elif 'repairable' in states:
            state = 'repairable'
        elif 'confirmed' in states:
            state = 'confirmed'
        else:
            state = 'planned'
        return {'operation_id': operation_id, 'state': state, 'steps': steps}

    def all_results(self):
        return [self.result(row['id']) for row in
                self.connection.execute('SELECT id FROM operations ORDER BY rowid')]

    def known_plans(self):
        return {row['id']: FrozenPlan.restore(json.loads(row['plan_json'])) for row in
                self.connection.execute('SELECT id,plan_json FROM operations')}

    def begin_attempt(self, operation_id, kind, request, preflight):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            row = self.connection.execute('SELECT state FROM steps WHERE operation_id=? AND kind=?',
                                          (operation_id, kind)).fetchone()
            if row['state'] not in ('ready', 'repairable'):
                raise Refusal('step is not dispatchable')
            prerequisites = STEPS[:STEPS.index(kind)]
            if any(self.steps(operation_id)[previous]['state'] != 'confirmed'
                   for previous in prerequisites):
                raise Refusal('step prerequisites are not confirmed')
            changed = self.connection.execute('UPDATE operations SET owner=? WHERE id=? AND owner IS NULL',
                                              (self.owner, operation_id)).rowcount
            if changed != 1:
                raise Refusal('busy operation claim')
            cursor = self.connection.execute('INSERT INTO attempts(operation_id,step,owner,request_json,'
                'request_digest,preflight_json,started_at) VALUES(?,?,?,?,?,?,?)',
                (operation_id, kind, self.owner, canonical(request), digest(request),
                 canonical(preflight), time.time()))
            attempt = cursor.lastrowid
            self.connection.execute("UPDATE steps SET state='dispatching',attempt_id=?,reason=NULL "
                                    'WHERE operation_id=? AND kind=?', (attempt, operation_id, kind))
            self.connection.commit()
            return attempt
        except Exception:
            self.connection.rollback()
            raise

    def finish_attempt(self, operation_id, kind, attempt, state, remote=None, reason=None):
        with self.connection:
            owner = self.connection.execute('SELECT owner FROM operations WHERE id=?',
                                             (operation_id,)).fetchone()['owner']
            if owner != self.owner:
                raise Refusal('lost durable operation ownership')
            changed = self.connection.execute('UPDATE steps SET state=?,remote_json=?,reason=? '
                "WHERE operation_id=? AND kind=? AND attempt_id=? AND state='dispatching'",
                (state, canonical(remote) if remote is not None else None, reason,
                 operation_id, kind, attempt)).rowcount
            if changed != 1:
                raise Refusal('attempt is no longer dispatching')
            self.connection.execute('UPDATE attempts SET outcome_json=?,finished_at=? WHERE id=? AND owner=?',
                (canonical({'state': state, 'remote': remote, 'reason': reason}), time.time(),
                 attempt, self.owner))
            self.connection.execute('UPDATE operations SET owner=NULL WHERE id=? AND owner=?',
                                     (operation_id, self.owner))

    def reconcile_step(self, operation_id, kind, state, remote=None, reason=None):
        existing = self.steps(operation_id)[kind]
        if existing['state'] == 'confirmed':
            if state == 'confirmed':
                if existing['remote'] == remote:
                    return
                state = 'investigate'
                reason = 'confirmed remote identity differs from reconciliation'
            elif state != 'investigate':
                raise Refusal('confirmed step cannot regress')
            remote = existing['remote']
        elif existing['remote'] is not None:
            if remote is not None and remote != existing['remote']:
                raise Refusal('stored remote identity is immutable')
            remote = existing['remote']
        with self.connection:
            self.connection.execute('UPDATE steps SET state=?,remote_json=?,reason=? '
                                     'WHERE operation_id=? AND kind=?',
                                     (state, canonical(remote) if remote is not None else None,
                                      reason, operation_id, kind))

    def begin_call(self, operation_id, method, path, request):
        with self.connection:
            return self.connection.execute('INSERT INTO calls(operation_id,owner,method,path,request_json,'
                'started_at) VALUES(?,?,?,?,?,?)',
                (operation_id, self.owner, method, path, canonical(request), time.time())).lastrowid

    def finish_call(self, call_id, outcome):
        with self.connection:
            self.connection.execute('UPDATE calls SET outcome_json=?,finished_at=? WHERE id=?',
                                     (canonical(outcome), time.time(), call_id))
