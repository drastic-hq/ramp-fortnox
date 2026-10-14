import argparse
import json
from pathlib import Path
import sys
import sqlite3

from .connector import Connector
from .domain import load_json, Refusal
from .journal import Journal, STEPS
from .transport import ExternalFailure
from .verification import verify_sync, replay_verification


def main():
    parser = argparse.ArgumentParser(description='Fixture-only P0 card-purchase export. Live writes are blocked.')
    parser.add_argument('--workspace', type=Path, help='Local durable SQLite journal directory')
    parser.add_argument('--fixture', type=Path, help='Synthetic source and evidence JSON')
    parser.add_argument('--base-url', help='Literal HTTP loopback fixture service URL')
    parser.add_argument('command', choices=['plan', 'apply', 'reconcile', 'status', 'verify-sync', 'replay-verification'])
    parser.add_argument('--crash-after-intent', choices=STEPS, help='Fixture fault injection only')
    parser.add_argument('--company', help='Bound Fortnox company identity')
    parser.add_argument('--year', type=int, help='Fortnox financial-year identity')
    parser.add_argument('--series', help='Fortnox voucher series')
    parser.add_argument('--operation-id', action='append', default=[], help='Optional frozen operation selection')
    parser.add_argument('--output', type=Path, help='Report and captured inputs directory outside ledger')
    parser.add_argument('--source', type=Path, help='Captured source.json for offline replay')
    parser.add_argument('--observed', type=Path, help='Captured observed.json for offline replay')
    arguments = parser.parse_args()
    journal = None
    try:
        if arguments.command == 'replay-verification':
            if not all([arguments.source, arguments.observed, arguments.output]):
                raise Refusal('replay-verification requires --source, --observed and --output')
            result = replay_verification(arguments.source, arguments.observed, arguments.output)
            print(json.dumps(result, sort_keys=True))
            return 0
        if not all([arguments.workspace, arguments.fixture, arguments.base_url]):
            raise Refusal('this command requires --workspace, --fixture and --base-url')
        fixture = load_json(arguments.fixture.read_text())
        if arguments.command == 'verify-sync':
            if not all([arguments.company, arguments.year, arguments.series, arguments.output]):
                raise Refusal('verify-sync requires --company, --year, --series and --output')
            result = verify_sync(arguments.workspace / 'journal.sqlite', fixture, arguments.base_url,
                                 arguments.company, arguments.year, arguments.series, arguments.output, arguments.operation_id)
            print(json.dumps(result, sort_keys=True))
            return 0
        journal = Journal(arguments.workspace)
        connector = Connector(fixture, journal, arguments.base_url, arguments.crash_after_intent)
        if arguments.command == 'status':
            result = {'operations': journal.all_results()}
            if any(step['state'] == 'dispatching' for operation in result['operations']
                   for step in operation['steps'].values()):
                try:
                    with journal.dispatcher():
                        result = {'operations': journal.all_results()}
                except Refusal:
                    result = {'operations': journal.all_results(), 'dispatcher': 'busy'}
        else:
            result = getattr(connector, arguments.command)()
        print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except (Refusal, ExternalFailure, KeyError, ValueError, TypeError, OSError, sqlite3.Error) as error:
        print(json.dumps({'error': str(error)}, sort_keys=True))
        return 2
    finally:
        if journal is not None:
            journal.connection.close()


if __name__ == '__main__':
    sys.exit(main())
