import argparse
import json
from pathlib import Path
import sys

from .connector import Connector
from .domain import load_json, Refusal
from .journal import Journal, STEPS
from .transport import ExternalFailure


def main():
    parser = argparse.ArgumentParser(description='Fixture-only P0 card-purchase export. Live writes are blocked.')
    parser.add_argument('--workspace', type=Path, required=True, help='Local durable SQLite journal directory')
    parser.add_argument('--fixture', type=Path, required=True, help='Synthetic source and evidence JSON')
    parser.add_argument('--base-url', required=True, help='Literal HTTP loopback fixture service URL')
    parser.add_argument('command', choices=['plan', 'apply', 'reconcile', 'status'])
    parser.add_argument('--crash-after-intent', choices=STEPS, help='Fixture fault injection only')
    arguments = parser.parse_args()
    journal = None
    try:
        fixture = load_json(arguments.fixture.read_text())
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
    except (Refusal, ExternalFailure, KeyError, ValueError, TypeError, OSError) as error:
        print(json.dumps({'error': str(error)}, sort_keys=True))
        return 2
    finally:
        if journal is not None:
            journal.connection.close()


if __name__ == '__main__':
    sys.exit(main())
