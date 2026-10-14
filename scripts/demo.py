#!/usr/bin/env python3
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='A focused two-minute local fixture CLI demo. No sandbox calls.')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/demo')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scenario_root = output / 'scenarios'
    scenario_root.mkdir(exist_ok=True)
    run_directory = Path(tempfile.mkdtemp(prefix='run-', dir=str(scenario_root)))
    scenarios = [
        ('Purchase and replay', 'test_literal_posting_attachment_ack_and_replay'),
        ('Committed lost create response', 'test_create_lost_response_commit_reconciles_and_never_reposts'),
        ('Uncommitted unprovable create response', 'test_create_lost_response_without_commit_stays_uncertain'),
        ('Receipt upload repair', 'test_attachment_repair_and_ack_idempotency'),
    ]
    environment = dict(os.environ, RAMP_FORTNOX_ARTIFACT_DIR=str(run_directory))
    environment['PYTHONPATH'] = os.pathsep.join(
        part for part in [str(ROOT / 'tests'), environment.get('PYTHONPATH')] if part)
    command = [sys.executable, '-m', 'unittest', '-v'] + [
        'test_cli_e2e.CliE2E.' + method for _, method in scenarios]
    started = time.monotonic()
    runner = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
    elapsed = round(time.monotonic() - started, 3)
    (output / 'runner-transcript.txt').write_text(runner.stdout + runner.stderr)
    transcript = ['Fixture-only demo. Live sandbox verification is pending.',
                  '$ ' + shlex.join(command)]
    results = []
    scenario_paths = []
    exit_code = runner.returncode
    for name, method in scenarios:
        path = run_directory / (method + '.json')
        transcript.append('\n' + name)
        if not path.exists():
            transcript.append('No scenario proof artifact was emitted.')
            exit_code = exit_code or 1
            continue
        scenario_paths.append(str(path.relative_to(output)))
        artifact = json.loads(path.read_text())
        states = []
        posting = None
        operation_id = None
        for event in artifact['events']:
            if 'command' not in event:
                continue
            response = json.loads(event['stdout'])
            if 'state' in response:
                states.append(response['state'])
                operation_id = response.get('operation_id', operation_id)
            elif response.get('operations'):
                states.append(response['operations'][0]['state'])
            if event['command'] in ['plan', 'apply', 'reconcile']:
                observed = response.get('state', response.get('error', 'no operation state'))
                transcript.append('$ ' + event['command'] + ' -> ' + observed +
                                  '. Exit code ' + str(event['exit_code']) + '.')
            if posting is None and 'posting' in response:
                posting = response['posting']
                for row in posting['Voucher']['VoucherRows']:
                    transcript.append('Account {}. Debit {}. Credit {} SEK.'.format(
                        row['Account'], row['Debit'], row['Credit']))
        remote = artifact['remote']
        counts = {key: len(remote[key]) for key in ['vouchers', 'files', 'connections', 'syncs']}
        counts['create_attempts'] = remote['create_attempts']
        transcript.append('Observed remote state. Vouchers {}. Files {}. Connections {}. '
                          'Ramp reports {}. Create attempts {}.'.format(
                              counts['vouchers'], counts['files'], counts['connections'],
                              counts['syncs'], counts['create_attempts']))
        results.append({'scenario': name, 'source_scenario': artifact['scenario'],
                        'operation_id': operation_id, 'final_state': states[-1] if states else None,
                        'observed_cli_states': states, 'posting': posting, **counts})
    transcript.append('\nFixture demo ' + ('passed' if exit_code == 0 else 'failed') +
                      ' in ' + str(elapsed) + ' seconds. Sandbox verification remains pending.')
    text = '\n'.join(transcript) + '\n'
    (output / 'transcript.txt').write_text(text)
    (output / 'demo.json').write_text(json.dumps({
        'scope': 'local fixture demonstration only', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'command': command, 'exit_code': exit_code, 'elapsed_seconds': elapsed,
        'scenarios': results, 'scenario_artifacts': scenario_paths,
        'literal_posting': json.loads((ROOT / 'fixtures/expected-posting.json').read_text()),
        'transcript': 'transcript.txt', 'runner_transcript': 'runner-transcript.txt'}, indent=2) + '\n')
    print(text, end='')
    print('Demo artifact ' + str(output / 'demo.json'))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
