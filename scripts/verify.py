#!/usr/bin/env python3
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='Repeat public CLI end-to-end proof against durable local HTTP fixtures.')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/verification')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scenario_root = output / 'scenarios'
    scenario_root.mkdir(exist_ok=True)
    run_directory = Path(tempfile.mkdtemp(prefix='run-', dir=str(scenario_root)))
    environment = dict(os.environ, RAMP_FORTNOX_ARTIFACT_DIR=str(run_directory))
    command = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*e2e.py', '-v']
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
    transcript = result.stdout + result.stderr
    (output / 'transcript.txt').write_text(transcript)
    scenario_paths = sorted(run_directory.glob('*.json'))
    report = {'scope': 'synthetic CLI, HTTP transport and durable SQLite only; no sandbox verification',
              'checked_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
              'command': command, 'exit_code': result.returncode,
              'elapsed_seconds': round(time.monotonic() - started, 3),
              'scenario_artifacts': [str(path.relative_to(output)) for path in scenario_paths],
              'report_artifacts': [str(path.relative_to(output)) for path in sorted(run_directory.glob('*/verification-*/report.json'))],
              'transcript': 'transcript.txt'}
    (output / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(transcript, end='')
    print('Proof artifact ' + str(output / 'verification.json'))
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
