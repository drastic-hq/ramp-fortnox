import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest

import test_cli_e2e as export_e2e

ROOT = Path(__file__).resolve().parents[1]
ORACLE = json.loads((ROOT / 'fixtures/sync-verification-oracle.json').read_text())


class SyncVerificationE2E(unittest.TestCase):
    setUp = export_e2e.CliE2E.setUp
    clean_owned = export_e2e.CliE2E.clean_owned
    cli = export_e2e.CliE2E.cli
    control = export_e2e.CliE2E.control
    remote = export_e2e.CliE2E.remote

    def tearDown(self):
        try:
            directory = os.environ.get('RAMP_FORTNOX_ARTIFACT_DIR')
            if directory:
                destination = Path(directory)
                destination.mkdir(parents=True, exist_ok=True)
                proof = destination / self._testMethodName
                proof.mkdir()
                for path in self.path.glob('verification-*'):
                    shutil.copytree(path, proof / path.name)
                artifact = {'scenario': self.id(), 'events': self.events,
                            'remote': self.remote() if self.server else self.stopped_remote, 'reports': str(proof.name)}
                (destination / (self._testMethodName + '.json')).write_text(json.dumps(artifact, indent=2))
        finally:
            self.clean_owned()

    def ledger(self):
        database_path = self.path / 'local/journal.sqlite'
        with sqlite3.connect(database_path.as_uri() + '?mode=ro', uri=True) as database:
            records = list(database.iterdump())
        return {'records': records, 'bytes': {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [database_path, Path(str(database_path) + '-wal')] if path.exists()}}

    def verify(self, extra=(), expected=0):
        before = self.ledger()
        remote_before = self.remote()
        audit_before = len(remote_before.get('method_log', []))
        output = self.path / ('verification-' + str(len(list(self.path.glob('verification-*')))))
        result = self.cli('verify-sync', expected, ['--company', '556000-0000', '--year', '1',
                         '--series', 'R', '--output', str(output)] + list(extra))
        after = self.ledger()
        self.assertEqual(after, before, 'verification leaves ledger records and stable DB/WAL bytes unchanged')
        remote_after = self.remote()
        financial_fields = ['vouchers', 'files', 'connections', 'syncs', 'create_attempts']
        self.assertEqual({key: remote_after[key] for key in financial_fields},
                         {key: remote_before[key] for key in financial_fields})
        audit = remote_after['method_log'][audit_before:]
        self.assertTrue(audit)
        self.assertEqual({event['method'] for event in audit}, {'GET'})
        self.assertFalse(any('/developer/' in event['path'] for event in audit))
        self.events.append({'verification': str(output), 'ledger_before': before, 'ledger_after': after,
                            'financial_remote_unchanged': True, 'http_audit': audit})
        if expected:
            return result
        report = json.loads((output / 'report.json').read_text())
        self.assertEqual(result['decision_digest'], report['decision_digest'])
        self.assertTrue((output / 'report.txt').read_text())
        self.assertEqual(report['policy'], ORACLE['policy'])
        for finding in report['findings'] + report['observed_only']:
            self.assertIs(finding['retry_authorized'], False)
            self.assertEqual(finding['execution_outcome'], 'unknown')
        return report, output

    def status(self, report):
        return report['findings'][0]['status']

    def test_confirmed_literal_report_and_offline_replay(self):
        self.cli('plan')
        self.cli('apply')
        report, output = self.verify()
        self.assertEqual(report['counts'], ORACLE['counts_confirmed'])
        finding = report['findings'][0]
        self.assertEqual(finding['operation_id'], ORACLE['operation_id'])
        self.assertEqual({key: value['status'] for key, value in finding['checks'].items()}, ORACLE['confirmed_checks'])
        self.assertEqual(report['source_population']['plans'][0]['posting_rows'], ORACLE['posting_rows'])
        self.assertEqual(report['source_population']['kind'], 'local_frozen_plans')
        self.assertEqual(report['source_population']['ramp_population_coverage'], 'not_enumerated')
        self.clean_server()
        shutil.rmtree(self.path / 'local')
        replay = self.path / 'verification-replay'
        command = [sys.executable, '-m', 'ramp_fortnox', 'replay-verification', '--source',
                   str(output / 'source.json'), '--observed', str(output / 'observed.json'), '--output', str(replay)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.events.append({'replay': command, 'stdout': result.stdout, 'exit_code': result.returncode})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((replay / 'report.json').read_text())['decision_digest'], report['decision_digest'])
        captured = json.loads((output / 'observed.json').read_text())
        captured['payload']['requests'][0]['body_base64'] = 'e30='
        tampered = output / 'tampered-observed.json'
        tampered.write_text(json.dumps(captured))
        command[command.index('--observed') + 1] = str(tampered)
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn('digest', result.stdout)
        captured['sha256'] = hashlib.sha256(json.dumps(captured['payload'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        tampered.write_text(json.dumps(captured))
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn('raw request body digest', result.stdout)
        source = json.loads((output / 'source.json').read_text())
        source['payload']['rows'][0]['fingerprint'] = 'changed'
        source['sha256'] = hashlib.sha256(json.dumps(source['payload'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        tampered_source = output / 'tampered-source.json'
        tampered_source.write_text(json.dumps(source))
        command[command.index('--source') + 1] = str(tampered_source)
        command[command.index('--observed') + 1] = str(output / 'observed.json')
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn('source snapshot digest', result.stdout)

    def test_unsupported_row_semantics_are_unknown_details(self):
        self.cli('plan')
        self.cli('apply')
        for field, value in [('Removed', True), ('Removed', 'true'),
                             ('CostCenter', 'TEAM1'), ('Project', 'PRJ1')]:
            with self.subTest(field=field, value=value):
                fields = {'Removed': False, 'CostCenter': '', 'Project': ''}
                fields[field] = value
                self.control(voucher_row_fields=fields)
                report, _ = self.verify()
                self.assertEqual(self.status(report), 'Unknown')
                self.assertEqual(report['counts']['Confirmed'], 0)
                self.assertIn('invalid detail', json.dumps(report))
        self.control(voucher_row_fields={'Removed': False, 'CostCenter': '', 'Project': ''})
        report, _ = self.verify()
        self.assertEqual(report['counts'], ORACLE['counts_confirmed'])

    def clean_server(self):
        self.stopped_remote = self.remote()
        self.server.terminate()
        self.server.communicate(timeout=5)
        self.server = None

    def test_never_dispatched_missing_then_late_commit(self):
        self.cli('plan')
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Missing')
        self.assertIn('not observed', report['findings'][0]['reasons'][0])
        self.cli('apply')
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Confirmed')

    def test_all_positive_classes_and_foreign_observations(self):
        self.cli('plan')
        self.cli('apply')
        self.control(verification_seed='foreign')
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Confirmed')
        self.assertEqual([row['status'] for row in report['observed_only']], ['Unknown'])
        self.control(verification_seed='duplicate', page_size=1)
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Duplicate')
        self.assertEqual(len(report['findings'][0]['voucher_identities']), 2)
        self.control(verification_seed='mismatch')
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Mismatched')
        self.assertEqual(report['findings'][0]['checks']['amounts']['status'], 'Mismatched')
        self.assertEqual(report['findings'][0]['checks']['amounts']['differences'][0]['observed'], 99900)

    def test_late_page_detail_failures_and_repeated_identity(self):
        self.cli('plan')
        self.cli('apply')
        self.control(verification_seed='foreign', page_size=1)
        for fault in ['late_page_fail', 'late_detail_fail', 'repeat_identity', 'conflicting_detail',
                      'changed_total', 'wrong_current_page']:
            with self.subTest(fault=fault):
                self.control(verification_fault=fault)
                report, _ = self.verify()
                self.assertEqual(self.status(report), 'Unknown')
                self.assertEqual(report['coverage']['state'], 'partial')
        self.control(verification_fault='late_page_fail', verification_seed='duplicate_and_foreign')
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Unknown')
        self.control(verification_fault='late_detail_fail')
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Duplicate')
        self.assertFalse(report['findings'][0]['count_is_exact'])

    def test_receipt_bytes_and_complete_connection_contract(self):
        self.cli('plan')
        self.cli('apply')
        for fault, status, receipt in [('receipt_fail', 'Unknown', 'Unknown'),
                                     ('partial_connections', 'Unknown', 'Unknown'),
                                     ('missing_connection', 'Mismatched', 'Missing'),
                                     ('wrong_connection', 'Mismatched', 'Mismatched'),
                                     ('corrupt_receipt', 'Mismatched', 'Mismatched')]:
            with self.subTest(fault=fault):
                self.control(verification_fault=fault)
                report, _ = self.verify()
                self.assertEqual(self.status(report), status)
                self.assertEqual(report['findings'][0]['checks']['receipt']['status'], receipt)

    def test_source_integrity_selection_empty_and_foreign_scope(self):
        self.cli('plan')
        self.cli('apply')
        report, _ = self.verify(['--operation-id', 'ramp:missing'])
        self.assertEqual(self.status(report), 'Unknown')
        self.assertEqual(report['source_population']['missing_requested_ids'], ['ramp:missing'])
        report, _ = self.verify(['--company', 'another-company'])
        self.assertEqual(report['source_population']['selected_operation_ids'], [])
        self.assertEqual(report['counts'], {'Confirmed': 0, 'Missing': 0, 'Duplicate': 0, 'Mismatched': 0, 'Unknown': 0})
        self.assertEqual(report['coverage']['state'], 'partial')
        self.control(metadata_fault='wrong_company')
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Unknown')
        self.assertEqual(report['coverage']['pages_requested'], [])
        self.control(metadata_fault='')
        database_path = self.path / 'local/journal.sqlite'
        with sqlite3.connect(database_path) as database:
            database.execute("UPDATE operations SET fingerprint='invalid'")
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Unknown')
        self.assertEqual(report['source_population']['invalid_operations'][0]['operation_id'], ORACLE['operation_id'])

    def test_positive_mismatch_survives_partial_and_full_field_differences(self):
        self.cli('plan')
        self.cli('apply')
        self.control(verification_seed='mismatch_and_foreign', page_size=1, verification_fault='late_detail_fail')
        report, _ = self.verify()
        self.assertEqual(self.status(report), 'Mismatched')
        self.assertEqual(report['coverage']['state'], 'partial')
        self.control(verification_seed='field_mismatch', verification_fault='')
        report, _ = self.verify()
        finding = report['findings'][0]
        self.assertEqual(self.status(report), 'Mismatched')
        self.assertEqual(finding['checks']['date']['status'], 'Mismatched')
        self.assertEqual(finding['checks']['accounts']['status'], 'Mismatched')
        self.assertEqual(finding['checks']['voucher']['status'], 'Mismatched')

    def test_numeric_json_preserves_exact_ore_in_scan_and_replay(self):
        self.cli('plan')
        self.cli('apply')
        for token, expected in [('1000.00', 'Confirmed'), ('1000.00000000000001', 'Unknown'),
                                ('1000.0000000000000000000000000000000001', 'Unknown')]:
            with self.subTest(token=token):
                self.control(verification_numeric_token=token)
                report, output = self.verify()
                captured = json.loads((output / 'observed.json').read_text())
                detail = next(record for record in captured['payload']['requests'] if record['kind'] == 'detail')
                self.assertIn(('"Debit": ' + token).encode(), base64.b64decode(detail['body_base64']))
                replay = output / 'offline-replay'
                command = [sys.executable, '-m', 'ramp_fortnox', 'replay-verification', '--source',
                           str(output / 'source.json'), '--observed', str(output / 'observed.json'), '--output', str(replay)]
                result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.events.append({'replay': command, 'stdout': result.stdout, 'exit_code': result.returncode})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                replay_report = json.loads((replay / 'report.json').read_text())
                self.assertEqual(self.status(report), expected)
                self.assertEqual(self.status(replay_report), expected)
                self.assertEqual(replay_report['decision_digest'], report['decision_digest'])
                if expected == 'Unknown':
                    self.assertEqual(report['coverage']['state'], 'partial')
                    self.assertIn('exact nonnegative ore', ' '.join(report['coverage']['failures']))

    def artifact_command(self, command, output, capture):
        if command == 'verify-sync':
            return self.command + [command, '--company', '556000-0000', '--year', '1',
                                   '--series', 'R', '--output', str(output)]
        return [sys.executable, '-m', 'ramp_fortnox', command, '--source', str(capture / 'source.json'),
                '--observed', str(capture / 'observed.json'), '--output', str(output)]

    def test_artifact_symlinks_and_nonregular_destinations_refused_before_any_write(self):
        self.cli('plan')
        self.cli('apply')
        _, capture = self.verify()
        ledger = self.path / 'local/journal.sqlite'
        before = self.ledger()
        names = ['source.json', 'observed.json', 'report.json', 'report.txt']
        for command in ['verify-sync', 'replay-verification']:
            for name in names:
                output = self.path / ('escape-' + command + '-' + name)
                output.mkdir()
                originals = {}
                for other in names:
                    if other != name:
                        originals[other] = ('untouched ' + other).encode()
                        (output / other).write_bytes(originals[other])
                (output / name).symlink_to(ledger)
                invocation = self.artifact_command(command, output, capture)
                result = subprocess.run(invocation, cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.events.append({'artifact_escape': invocation, 'stdout': result.stdout, 'exit_code': result.returncode})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.ledger(), before)
                self.assertTrue((output / name).is_symlink())
                self.assertEqual({other: (output / other).read_bytes() for other in originals}, originals)
            output = self.path / ('nonregular-' + command)
            output.mkdir()
            (output / 'report.txt').mkdir()
            result = subprocess.run(self.artifact_command(command, output, capture), cwd=ROOT,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(sorted(path.name for path in output.iterdir()), ['report.txt'])
            self.assertEqual(self.ledger(), before)

    def test_artifact_hardlinks_never_mutate_linked_ledger(self):
        self.cli('plan')
        self.cli('apply')
        _, capture = self.verify()
        ledger = self.path / 'local/journal.sqlite'
        before = self.ledger()
        for command in ['verify-sync', 'replay-verification']:
            for name in ['source.json', 'observed.json', 'report.json', 'report.txt']:
                output = self.path / ('hardlink-' + command + '-' + name)
                output.mkdir()
                os.link(ledger, output / name)
                invocation = self.artifact_command(command, output, capture)
                result = subprocess.run(invocation, cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.events.append({'artifact_hardlink': invocation, 'stdout': result.stdout, 'exit_code': result.returncode})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.ledger(), before)
                self.assertNotEqual((output / name).stat().st_ino, ledger.stat().st_ino)
                self.assertEqual(json.loads((output / 'report.json').read_text())['counts'], ORACLE['counts_confirmed'])

    def test_malformed_source_containers_isolate_unknown_from_valid_plan(self):
        self.cli('plan')
        self.cli('apply')
        fixture = json.loads(self.fixture.read_text())
        transaction_id = '55555555-5555-4555-8555-555555555555'
        fixture['transaction']['id'] = transaction_id
        fixture['receipt']['transaction_id'] = transaction_id
        fixture['evidence']['receipt']['transaction_id'] = transaction_id
        self.fixture.write_text(json.dumps(fixture))
        self.control(reload_fixture=True)
        other_key = self.cli('plan')['operation_id']
        self.cli('apply')
        database_path = self.path / 'local/journal.sqlite'
        with sqlite3.connect(database_path) as database:
            original = database.execute('SELECT plan_json FROM operations WHERE id=?', (ORACLE['operation_id'],)).fetchone()[0]
        invalid_fields = [(path, 'malformed') for path in [
            ('transaction', 'entity_amount'), ('transaction',), ('receipt',), ('evidence',),
            ('transaction', 'merchant_location'), ('receipt', 'ocr'), ('evidence', 'units_calibration')]]
        invalid_fields += [(('transaction', 'line_items'), [{'amount': 125000}]),
                           (('transaction', 'requires_accounting_vendor_creation_to_sync'), True)]
        for path, value in invalid_fields:
            with self.subTest(path=path):
                raw = json.loads(original)
                cursor = raw['source']
                for name in path[:-1]:
                    cursor = cursor[name]
                cursor[path[-1]] = value
                fingerprint = hashlib.sha256(json.dumps({'source': raw['source'], 'posting': raw['payload']},
                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                raw['fingerprint'] = fingerprint
                with sqlite3.connect(database_path) as database:
                    database.execute('UPDATE operations SET plan_json=?,fingerprint=? WHERE id=?',
                                     (json.dumps(raw), fingerprint, ORACLE['operation_id']))
                report, _ = self.verify()
                findings = {finding['operation_id']: finding for finding in report['findings']}
                self.assertEqual(findings[ORACLE['operation_id']]['status'], 'Unknown')
                self.assertEqual(findings[other_key]['status'], 'Confirmed')
                self.assertEqual(report['counts'], {'Confirmed': 1, 'Missing': 0, 'Duplicate': 0, 'Mismatched': 0, 'Unknown': 1})

    def test_malformed_remote_containers_become_unknown(self):
        self.cli('plan')
        self.cli('apply')
        for fault in ['malformed_company', 'malformed_connection_coverage']:
            with self.subTest(fault=fault):
                self.control(verification_fault=fault)
                report, _ = self.verify()
                self.assertEqual(self.status(report), 'Unknown')
                self.assertEqual(report['coverage']['state'], 'partial')
                if fault == 'malformed_company':
                    self.assertEqual(report['coverage']['pages_requested'], [])
                else:
                    self.assertEqual(report['findings'][0]['checks']['receipt']['status'], 'Unknown')
