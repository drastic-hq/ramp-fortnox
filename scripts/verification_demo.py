#!/usr/bin/env python3
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='Repeat bounded read-only sync verification through the public CLI.')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/sync-verification-demo')
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scenarios = output / 'scenarios'
    scenarios.mkdir(exist_ok=True)
    run_directory = Path(tempfile.mkdtemp(prefix='run-', dir=str(scenarios)))
    methods = [
        'test_confirmed_literal_report_and_offline_replay',
        'test_never_dispatched_missing_then_late_commit',
        'test_all_positive_classes_and_foreign_observations',
        'test_late_page_detail_failures_and_repeated_identity',
        'test_numeric_json_preserves_exact_ore_in_scan_and_replay',
    ]
    environment = dict(os.environ, RAMP_FORTNOX_ARTIFACT_DIR=str(run_directory))
    environment['PYTHONPATH'] = os.pathsep.join(
        part for part in [str(ROOT / 'tests'), environment.get('PYTHONPATH')] if part)
    command = [sys.executable, '-m', 'unittest', '-v'] + [
        'test_sync_verification_e2e.SyncVerificationE2E.' + method for method in methods]
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
    (output / 'runner-transcript.txt').write_text(result.stdout + result.stderr)
    transcript = ['Local synthetic Fortnox SE SEK P0 fixture proof.', '$ ' + shlex.join(command)]
    observations = []
    for path in sorted(run_directory.glob('*/verification-*/report.json')):
        report = json.loads(path.read_text())
        observations.append({'report': str(path.relative_to(output)), 'counts': report['counts'],
                             'coverage': report['coverage']['state'], 'decision_digest': report['decision_digest']})
        transcript.append('{} -> {}; coverage {}.'.format(path.parent.parent.name,
            ', '.join('{} {}'.format(status, count) for status, count in report['counts'].items() if count),
            report['coverage']['state']))
    confirmed = run_directory / methods[0] / 'verification-0'
    exit_code = result.returncode
    if confirmed.is_dir():
        for name in ['report.json', 'report.txt', 'source.json', 'observed.json']:
            shutil.copy2(confirmed / name, output / name)
    else:
        exit_code = exit_code or 1
    transcript.extend([
        'Every scan checks GET-only collection, unchanged ledger records and stable DB/WAL bytes, and unchanged remote financial records.',
        'Offline replay runs after stopping the fixture and deleting its ledger, and must reproduce the decision digest.',
        'Missing records absence during the bounded scan; execution remains unknown and retry is never authorized.',
        'Synthetic fixture proof only. No live Fortnox, Ramp population or Nordic interoperability proof.',
        'Result: ' + ('passed' if exit_code == 0 else 'failed') + '.',
    ])
    text = '\n'.join(transcript) + '\n'
    (output / 'transcript.txt').write_text(text)
    (output / 'demo.json').write_text(json.dumps({
        'scope': 'local synthetic read-only CLI verification and captured-input replay',
        'checked_at': datetime.now(timezone.utc).isoformat(), 'command': command,
        'exit_code': exit_code, 'elapsed_seconds': round(time.monotonic() - started, 3),
        'observations': observations, 'transcript': 'transcript.txt',
        'runner_transcript': 'runner-transcript.txt',
        'scenario_artifacts': [str(path.relative_to(output)) for path in sorted(run_directory.glob('*.json'))],
    }, indent=2) + '\n')
    print(text, end='')
    print('Proof artifact ' + str(output / 'demo.json'))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
