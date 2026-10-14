import base64
import hashlib
import copy
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ORACLE = json.loads((ROOT / 'fixtures/expected-posting.json').read_text())


class CliE2E(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.clean_owned)
        self.path = Path(self.temp.name)
        self.fixture = self.path / 'fixture.json'
        self.fixture.write_text((ROOT / 'fixtures/purchase.json').read_text())
        self.ready = self.path / 'ready.json'
        self.server = subprocess.Popen([
            sys.executable, '-m', 'ramp_fortnox.fixture_server',
            '--state', str(self.path / 'remote.sqlite'), '--fixture', str(self.fixture),
            '--ready-file', str(self.ready),
        ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not self.ready.exists() and time.monotonic() < deadline:
            if self.server.poll() is not None:
                self.fail(self.server.communicate()[1])
            time.sleep(0.02)
        self.assertTrue(self.ready.exists(), 'HTTP fixture service becomes ready')
        self.url = json.loads(self.ready.read_text())['base_url']
        self.command = [sys.executable, '-m', 'ramp_fortnox', '--workspace',
                        str(self.path / 'local'), '--fixture', str(self.fixture),
                        '--base-url', self.url]

    def clean_owned(self):
        server = getattr(self, 'server', None)
        if server is not None:
            try:
                if server.poll() is None:
                    server.terminate()
                server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
            finally:
                self.server = None
        self.temp.cleanup()

    def tearDown(self):
        try:
            artifact_dir = os.environ.get('RAMP_FORTNOX_ARTIFACT_DIR')
            if artifact_dir:
                destination = Path(artifact_dir)
                destination.mkdir(parents=True, exist_ok=True)
                remote = self.remote()
                database_path = self.path / 'local/journal.sqlite'
                journal = {}
                if database_path.exists():
                    with sqlite3.connect(str(database_path)) as database:
                        database.row_factory = sqlite3.Row
                        journal = {table: [dict(row) for row in database.execute('SELECT * FROM ' + table)]
                                   for table in ['operations', 'steps', 'attempts', 'calls']}
                artifact = {'scenario': self.id(), 'fixture_sha256': hashlib.sha256(self.fixture.read_bytes()).hexdigest(),
                            'events': self.events, 'remote': remote, 'journal': journal}
                (destination / (self._testMethodName + '.json')).write_text(json.dumps(artifact, indent=2))
        finally:
            self.clean_owned()

    def cli(self, command, expected=0, extra=()):
        result = subprocess.run(self.command + [command] + list(extra), cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.events.append({'command': command, 'extra': list(extra), 'exit_code': result.returncode,
                            'stdout': result.stdout, 'stderr': result.stderr})
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def control(self, **changes):
        self.events.append({'fixture_control': changes})
        request = Request(self.url + '/__fixture', data=json.dumps(changes).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def remote(self):
        with urlopen(self.url + '/__fixture', timeout=5) as response:
            return json.load(response)

    def mutate(self, field, value):
        fixture = json.loads(self.fixture.read_text())
        cursor = fixture
        for name in field[:-1]:
            cursor = cursor[name]
        cursor[field[-1]] = value
        self.fixture.write_text(json.dumps(fixture))
        self.control(reload_fixture=True)

    def assert_complete(self):
        result = self.cli('status')
        self.assertEqual(result['operations'][0]['state'], 'complete')
        remote = self.remote()
        self.assertEqual([v['payload'] for v in remote['vouchers']], [ORACLE])
        self.assertEqual(len(remote['files']), 1)
        self.assertEqual(len(remote['connections']), 1)
        original = (ROOT / 'fixtures/original-receipt.txt').read_bytes()
        self.assertEqual(base64.b64decode(remote['files'][0]['original_base64']), original)
        self.assertEqual(remote['files'][0]['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(remote['connections'][0]['voucher_number'], remote['vouchers'][0]['number'])
        self.assertEqual(remote['connections'][0]['file_id'], remote['files'][0]['id'])
        self.assertEqual(remote['connections'][0]['voucher_series'], 'R')
        self.assertEqual(remote['connections'][0]['voucher_year'], 1)
        self.assertEqual(remote['connections'][0]['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(len(remote['syncs']), 1)
        self.assertEqual(remote['syncs'][0]['payload']['mark_ready_to_sync'], False)
        self.assertEqual(remote['syncs'][0]['payload']['idempotency_key'],
                         'ramp:44444444-4444-4444-8444-444444444444:22222222-2222-4222-8222-222222222222:11111111-1111-4111-8111-111111111111:card_purchase:ack')

    def test_literal_posting_attachment_ack_and_replay(self):
        planned = self.cli('plan')
        self.assertEqual(planned['posting'], ORACLE)
        self.assertEqual(planned['state'], 'planned')
        self.assertEqual(self.cli('apply')['state'], 'complete')
        self.assert_complete()
        self.assertEqual(self.remote()['fixture']['transaction']['sync_status'], 'SYNCED')
        self.assertEqual(self.cli('plan')['operation_id'], planned['operation_id'])
        self.assertEqual(self.cli('apply')['state'], 'complete')
        self.assert_complete()

    def test_eligibility_rejections(self):
        cases = [
            (['config', 'ramp_entity_id'], 'other', 'entity'),
            (['transaction', 'entity_amount', 'currency'], 'EUR', 'currency'),
            (['transaction', 'merchant_amount', 'currency'], 'EUR', 'currency'),
            (['evidence', 'receipt', 'currency'], 'EUR', 'currency'),
            (['evidence', 'merchant_country'], 'DE', 'domestic'),
            (['transaction', 'merchant_location', 'country'], 'DE', 'domestic'),
            (['transaction', 'accounting_field_selections'], [], 'coded'),
            (['transaction', 'entity_amount', 'value'], 0, 'positive'),
            (['transaction', 'entity_amount', 'value'], -125000, 'positive'),
            (['transaction', 'all_requirements_met_and_approved'], False, 'approved'),
            (['evidence', 'units_calibration', 'verified'], False, 'calibration'),
            (['evidence', 'receipt', 'transaction_id'], 'other', 'receipt'),
            (['evidence', 'receipt', 'sha256'], 'bad', 'receipt'),
            (['evidence', 'receipt', 'vat_ore'], 24999, 'receipt'),
            (['config', 'card_liability_account'], None, 'liability'),
            (['receipt', 'ocr', 'taxes', 0, 'tax_rate'], 12, '25'),
            (['receipt', 'ocr', 'taxes', 0, 'tax_amount'], 249.99, 'tax'),
            (['receipt', 'ocr', 'taxes'], [{}, {}], 'single'),
        ]
        original = self.fixture.read_text()
        for path, value, message in cases:
            with self.subTest(path=path):
                self.fixture.write_text(original)
                self.mutate(path, value)
                refused = self.cli('plan', expected=2)
                self.assertIn(message, refused['error'])
                self.assertEqual(self.remote()['vouchers'], [])
        self.fixture.write_text(original)
        self.control(reload_fixture=True)
        self.cli('plan')
        self.cli('apply')
        self.assert_complete()

    def test_split_line_items_are_refused_before_writes(self):
        self.mutate(['transaction', 'line_items'], [{
            'accounting_field_selections': [{'type': 'GL_ACCOUNT', 'external_id': '5420'}],
            'amount': {'currency': 'SEK', 'value': 125000}}])
        self.assertIn('line items', self.cli('plan', expected=2)['error'])
        self.assertEqual(self.remote()['vouchers'], [])
        self.assertEqual(self.remote()['syncs'], [])

    def test_vendor_creation_requirement_is_refused_before_writes(self):
        self.mutate(['transaction', 'requires_accounting_vendor_creation_to_sync'], True)
        self.assertIn('vendor creation', self.cli('plan', expected=2)['error'])
        self.assertEqual(self.remote()['vouchers'], [])
        self.assertEqual(self.remote()['syncs'], [])

    def test_empty_line_items_and_no_vendor_creation_keep_literal_posting(self):
        self.mutate(['transaction', 'line_items'], [])
        self.mutate(['transaction', 'requires_accounting_vendor_creation_to_sync'], False)
        self.assertEqual(self.cli('plan')['posting'], ORACLE)
        self.assertEqual(self.cli('apply')['state'], 'complete')
        self.assert_complete()

    def test_lost_ack_with_synced_at_recovers_without_another_voucher(self):
        self.assertEqual(json.loads(self.fixture.read_text())['transaction'].get('synced_at'), None)
        self.cli('plan')
        self.control(fault='ack_drop_commit')
        self.assertEqual(self.cli('apply')['state'], 'repairable')
        transaction = self.remote()['fixture']['transaction']
        self.assertEqual(transaction['sync_status'], 'SYNCED')
        self.assertEqual(transaction['synced_at'], '2026-10-01T13:01:00Z')
        self.assertEqual(self.cli('apply')['state'], 'complete')
        self.assert_complete()
        self.assertEqual(self.remote()['create_attempts'], 1)

    def ack_source_drift(self, path, value):
        self.cli('plan')
        self.control(fault='ack_drop_commit')
        self.assertEqual(self.cli('apply')['state'], 'repairable')
        self.fixture.write_text(json.dumps(self.remote()['fixture']))
        self.mutate(['transaction'] + path, value)
        before = self.remote()
        self.assertIn('stale', self.cli('apply', expected=2)['error'])
        after = self.remote()
        self.assertEqual(after['vouchers'], before['vouchers'])
        self.assertEqual(after['syncs'], before['syncs'])
        self.assertEqual(after['create_attempts'], 1)
        self.assertEqual(self.cli('status')['operations'][0]['state'], 'repairable')

    def test_lost_ack_does_not_normalize_coding_drift(self):
        self.ack_source_drift(['accounting_field_selections', 0, 'external_id'], '5420')

    def test_lost_ack_does_not_normalize_amount_drift(self):
        self.ack_source_drift(['entity_amount', 'value'], 125001)

    def test_lost_ack_does_not_normalize_approval_drift(self):
        self.ack_source_drift(['all_requirements_met_and_approved'], False)

    def test_synced_at_drift_before_own_ack_is_stale(self):
        self.cli('plan')
        self.mutate(['transaction', 'synced_at'], '2026-10-01T13:01:00Z')
        self.assertIn('stale', self.cli('apply', expected=2)['error'])
        self.assertEqual(self.remote()['vouchers'], [])

    def unsupported_recovery_row(self, fields):
        self.cli('plan')
        self.control(fault='create_drop_commit')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.control(voucher_row_fields=fields)
        result = self.cli('reconcile')
        self.assertEqual(result['state'], 'investigate')
        remote = self.remote()
        self.assertEqual(len(remote['vouchers']), 1)
        self.assertEqual(remote['files'], [])
        self.assertEqual(remote['syncs'], [])
        self.assertEqual(remote['create_attempts'], 1)

    def test_removed_boolean_row_cannot_recover_voucher(self):
        self.unsupported_recovery_row({'Removed': True})

    def test_removed_string_row_cannot_recover_voucher(self):
        self.unsupported_recovery_row({'Removed': 'true'})

    def test_cost_center_row_cannot_recover_voucher(self):
        self.unsupported_recovery_row({'CostCenter': 'TEAM1'})

    def test_project_row_cannot_recover_voucher(self):
        self.unsupported_recovery_row({'Project': 'PRJ1'})

    def test_empty_row_metadata_can_recover_literal_voucher(self):
        self.cli('plan')
        self.control(fault='create_drop_commit')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.control(voucher_row_fields={'Removed': False, 'CostCenter': '', 'Project': ''})
        self.assertEqual(self.cli('reconcile')['state'], 'confirmed')
        self.assertEqual(self.cli('apply')['state'], 'complete')
        remote = self.remote()
        self.assertEqual(len(remote['vouchers']), 1)
        self.assertEqual(len(remote['syncs']), 1)
        self.assertEqual(remote['create_attempts'], 1)

    def test_stale_frozen_inputs_never_write(self):
        self.cli('plan')
        original = self.fixture.read_text()
        changes = [
            (['transaction', 'updated_at'], '2026-10-02T12:00:00Z'),
            (['config', 'mapping_revision'], 'changed'),
            (['evidence', 'source_reference'], 'changed'),
            (['config', 'fortnox_company_id'], 'changed'),
            (['evidence', 'receipt', 'original_base64'], 'Y2hhbmdlZA=='),
        ]
        for path, value in changes:
            with self.subTest(path=path):
                self.fixture.write_text(original)
                self.mutate(path, value)
                self.assertIn('stale', self.cli('apply', expected=2)['error'])
                self.assertEqual(self.remote()['vouchers'], [])
        self.fixture.write_text(original)
        self.control(reload_fixture=True)
        self.cli('apply')
        self.assert_complete()

    def test_company_accounts_year_and_series(self):
        self.cli('plan')
        for fault, message in [('wrong_company', 'company'), ('inactive_account', 'account'),
                               ('missing_year', 'year'), ('missing_series', 'series')]:
            with self.subTest(fault=fault):
                self.control(metadata_fault=fault)
                self.assertIn(message, self.cli('apply', expected=2)['error'])
                self.assertEqual(self.remote()['vouchers'], [])
        self.control(metadata_fault='')
        self.cli('apply')
        self.assert_complete()

    def test_competing_processes_do_not_steal_dispatch(self):
        self.cli('plan')
        self.control(hold_create=True)
        first = subprocess.Popen(self.command + ['apply'], cwd=ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while True:
                live = self.cli('status')
                if live.get('dispatcher') == 'busy' and live['operations'][0]['state'] == 'dispatching':
                    break
                if first.poll() is not None:
                    output, error = first.communicate()
                    self.fail('first dispatcher exited before barrier: ' + output + error)
                self.assertLess(time.monotonic(), deadline, 'durable dispatch barrier appears')
                time.sleep(0.02)
            second = subprocess.run(self.command + ['apply'], cwd=ROOT, capture_output=True,
                                    text=True, timeout=15)
            self.events.append({'concurrent_apply_exit_code': second.returncode,
                                'stdout': second.stdout, 'stderr': second.stderr})
            self.control(hold_create=False)
            output, error = first.communicate(timeout=15)
            self.events.append({'first_apply_exit_code': first.returncode, 'stdout': output, 'stderr': error})
            self.assertEqual(first.returncode, 0, output + error)
            self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
            self.assertIn('busy', json.loads(second.stdout)['error'])
            self.assert_complete()
        finally:
            if first.poll() is None:
                first.kill()
            first.communicate()

    def test_create_lost_response_commit_reconciles_and_never_reposts(self):
        self.cli('plan')
        self.control(fault='create_drop_commit', page_size=1)
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.cli('reconcile')['state'], 'confirmed')
        self.cli('apply')
        self.assert_complete()
        self.assertEqual(self.remote()['create_attempts'], 1)
        self.assertGreaterEqual(self.remote()['detail_reads'], 1)

    def test_create_lost_response_without_commit_stays_uncertain(self):
        self.cli('plan')
        self.control(fault='create_drop_no_commit')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.cli('reconcile')['state'], 'uncertain')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.remote()['create_attempts'], 1)
        self.assertEqual(self.remote()['vouchers'], [])
        self.assertEqual(self.remote()['syncs'], [])

    def test_crash_at_durable_intent_never_reposts(self):
        self.cli('plan')
        self.assertEqual(self.cli('apply', expected=91,
                                  extra=['--crash-after-intent', 'create'])['state'], 'dispatching')
        self.assertEqual(self.cli('status')['operations'][0]['state'], 'uncertain')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.cli('reconcile')['state'], 'uncertain')
        self.assertEqual(self.remote()['create_attempts'], 0)

    def test_owned_series_conflicts_and_pagination(self):
        self.cli('plan')
        self.control(fault='create_drop_commit', page_size=1)
        self.cli('apply')
        original = self.fixture.read_text()
        known_markers = []
        for transaction_id in ['55555555-5555-4555-8555-555555555555',
                               '66666666-6666-4666-8666-666666666666']:
            fixture = json.loads(original)
            fixture['transaction']['id'] = transaction_id
            fixture['receipt']['transaction_id'] = transaction_id
            fixture['evidence']['receipt']['transaction_id'] = transaction_id
            self.fixture.write_text(json.dumps(fixture))
            self.control(reload_fixture=True)
            known_markers.append(self.cli('plan')['operation_id'])
        self.fixture.write_text(original)
        self.control(reload_fixture=True, seed='known_before_and_after', known_markers=known_markers)
        self.assertEqual(self.cli('reconcile')['state'], 'confirmed')
        self.assertEqual(self.remote()['list_pages'], 3)
        self.assertEqual(self.remote()['detail_reads'], 3)
        for seed in ['foreign', 'duplicate', 'mismatch']:
            with self.subTest(seed=seed):
                self.control(seed=seed)
                self.assertEqual(self.cli('reconcile')['state'], 'investigate')
                self.assertEqual(self.remote()['syncs'], [])

    def test_attachment_repair_and_ack_idempotency(self):
        self.cli('plan')
        self.control(fault='upload_reject')
        self.assertEqual(self.cli('apply')['state'], 'repairable')
        self.assertEqual(len(self.remote()['vouchers']), 1)
        self.assertEqual(self.remote()['syncs'], [])
        self.control(fault='connect_reject')
        self.assertEqual(self.cli('apply')['state'], 'repairable')
        self.assertEqual(len(self.remote()['vouchers']), 1)
        self.assertEqual(self.remote()['syncs'], [])
        self.control(fault='ack_drop_commit')
        self.assertEqual(self.cli('apply')['state'], 'repairable')
        self.cli('apply')
        self.assert_complete()
        self.assertEqual(self.remote()['create_attempts'], 1)

    def test_ambiguous_attachment_requires_reconciliation(self):
        self.cli('plan')
        self.control(fault='upload_drop_commit')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.remote()['syncs'], [])
        self.assertEqual(self.cli('reconcile')['state'], 'confirmed')
        self.control(fault='connect_drop_commit')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.cli('reconcile')['state'], 'confirmed')
        self.cli('apply')
        self.assert_complete()
        self.assertEqual(self.remote()['create_attempts'], 1)

    def test_malformed_create_success_and_failed_recovery_reads(self):
        self.cli('plan')
        self.control(fault='create_malformed')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        for fault in ['list_fail', 'detail_fail']:
            with self.subTest(fault=fault):
                self.control(read_fault=fault)
                self.assertEqual(self.cli('reconcile')['state'], 'uncertain')
                self.assertEqual(self.remote()['create_attempts'], 1)
        self.control(read_fault='')
        self.assertEqual(self.cli('reconcile')['state'], 'confirmed')
        self.cli('apply')
        self.assert_complete()

    def test_approval_missing_null_string_and_missing_receipt_tax(self):
        original = self.fixture.read_text()
        for approval in [None, 'true', 1]:
            with self.subTest(approval=approval):
                self.fixture.write_text(original)
                self.mutate(['transaction', 'all_requirements_met_and_approved'], approval)
                self.assertIn('approved', self.cli('plan', expected=2)['error'])
                self.assertEqual(self.remote()['vouchers'], [])
        self.fixture.write_text(original)
        self.mutate(['receipt', 'ocr', 'taxes'], [])
        self.assertIn('single', self.cli('plan', expected=2)['error'])
        self.fixture.write_text(original)
        self.mutate(['transaction', 'receipts'], [])
        self.assertIn('receipt', self.cli('plan', expected=2)['error'])
        self.fixture.write_text(original)
        self.control(reload_fixture=True)
        self.cli('plan')
        self.cli('apply')
        self.assert_complete()

    def test_minor_unit_calibration_and_fractional_ore_refusal(self):
        self.mutate(['evidence', 'units_calibration', 'unit'], 'SEK_MINOR')
        self.mutate(['receipt', 'ocr', 'taxes', 0, 'tax_amount'], 25000)
        self.assertEqual(self.cli('plan')['posting'], ORACLE)
        self.cli('apply')
        self.assert_complete()

    def test_fractional_ore_refused_with_valid_pair(self):
        self.mutate(['receipt', 'ocr', 'taxes', 0, 'tax_amount'], 250.001)
        self.assertIn('fractions', self.cli('plan', expected=2)['error'])
        self.assertEqual(self.remote()['vouchers'], [])
        self.mutate(['receipt', 'ocr', 'taxes', 0, 'tax_amount'], 250)
        self.cli('plan')
        self.cli('apply')
        self.assert_complete()

    def test_late_commit_after_first_missing_recovery(self):
        self.cli('plan')
        self.control(fault='create_drop_no_commit')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.cli('reconcile')['state'], 'uncertain')
        self.control(commit_pending=True)
        self.assertEqual(self.cli('reconcile')['state'], 'confirmed')
        self.cli('apply')
        self.assert_complete()
        self.assertEqual(self.remote()['create_attempts'], 1)

    def test_remote_server_restart_keeps_actual_effects(self):
        self.cli('plan')
        self.cli('apply')
        self.server.terminate()
        self.server.communicate(timeout=5)
        self.ready.unlink()
        self.server = subprocess.Popen([
            sys.executable, '-m', 'ramp_fortnox.fixture_server',
            '--state', str(self.path / 'remote.sqlite'), '--fixture', str(self.fixture),
            '--ready-file', str(self.ready),
        ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not self.ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.ready.exists())
        self.url = json.loads(self.ready.read_text())['base_url']
        self.command[-1] = self.url
        self.assertEqual(self.cli('apply')['state'], 'complete')
        self.assert_complete()
        self.assertEqual(self.remote()['create_attempts'], 1)

    def test_confirmed_identity_survives_missing_remote_investigation(self):
        self.cli('plan')
        self.cli('apply')
        confirmed = self.cli('status')['operations'][0]['steps']['create']['remote']
        self.control(erase_vouchers=True)
        result = self.cli('reconcile')
        self.assertEqual(result['state'], 'investigate')
        self.assertEqual(result['steps']['create']['remote'], confirmed)
        self.assertEqual(self.cli('apply')['state'], 'investigate')
        self.assertEqual(self.remote()['create_attempts'], 1)

    def test_wrong_series_year_attachment_never_acknowledged(self):
        self.cli('plan')
        self.control(fault='connect_drop_commit')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.control(wrong_connection_identity=True)
        self.assertEqual(self.cli('reconcile')['state'], 'uncertain')
        self.assertEqual(self.cli('apply')['state'], 'uncertain')
        self.assertEqual(self.remote()['syncs'], [])
        self.assertEqual(self.remote()['create_attempts'], 1)

    def ack_tamper(self, control, step):
        self.cli('plan')
        self.control(fault='ack_reject')
        self.assertEqual(self.cli('apply')['state'], 'repairable')
        confirmed = self.cli('status')['operations'][0]['steps'][step]['remote']
        self.control(**{control: True})
        result = self.cli('apply')
        self.assertEqual(result['state'], 'investigate')
        self.assertEqual(result['steps'][step]['remote'], confirmed)
        self.assertEqual(self.remote()['syncs'], [])
        self.assertEqual(self.remote()['create_attempts'], 1)

    def test_repaired_ack_revalidates_exact_voucher(self):
        self.ack_tamper('tamper_voucher', 'create')

    def test_repaired_ack_revalidates_original_file_bytes(self):
        self.ack_tamper('tamper_file', 'upload')

    def test_repaired_ack_revalidates_exact_connection_identity(self):
        self.ack_tamper('wrong_connection_identity', 'connect')

    def test_each_posting_account_must_be_active(self):
        self.cli('plan')
        for account in [5410, 2641, 2899]:
            with self.subTest(account=account):
                self.control(inactive_account_number=account)
                self.assertIn('account', self.cli('apply', expected=2)['error'])
                self.assertEqual(self.remote()['vouchers'], [])
        self.control(inactive_account_number=None)
        self.cli('apply')
        self.assert_complete()

    def test_original_receipt_download_blocks_external_url_and_redirect(self):
        self.control(receipt_url_override='https://api.fortnox.se')
        self.assertIn('loopback', self.cli('plan', expected=2)['error'])
        self.assertEqual(self.remote()['vouchers'], [])
        self.control(receipt_url_override=self.url + '/__redirect')
        self.assertIn('302', self.cli('plan', expected=2)['error'])
        self.assertEqual(self.remote()['vouchers'], [])
        self.control(receipt_url_override='')
        self.cli('plan')
        self.cli('apply')
        self.assert_complete()

    def test_live_endpoint_blocked(self):
        command = copy.copy(self.command)
        command[-1] = 'https://api.fortnox.se'
        result = subprocess.run(command + ['plan'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('loopback', json.loads(result.stdout)['error'])
        self.cli('plan')
        self.cli('apply')
        self.assert_complete()


if __name__ == '__main__':
    unittest.main()
