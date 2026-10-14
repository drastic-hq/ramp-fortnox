"""Durable local HTTP simulation. Upload and proof routes are not live Fortnox contracts."""
import argparse
import base64
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid


class Store:
    def __init__(self, path, fixture):
        self.path = path
        self.fixture_path = fixture
        self.lock = threading.RLock()
        with sqlite3.connect(self.path) as database:
            database.execute('PRAGMA synchronous=FULL')
            database.execute('CREATE TABLE IF NOT EXISTS remote_state(id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
            if not database.execute('SELECT 1 FROM remote_state').fetchone():
                state = {'fixture': json.loads(Path(fixture).read_text()), 'vouchers': [], 'files': [],
                         'connections': [], 'syncs': [], 'create_attempts': 0, 'detail_reads': 0,
                         'list_pages': 0, 'fault': '', 'metadata_fault': '', 'read_fault': '',
                         'method_log': [], 'verification_fault': '', 'page_size': 100, 'delay_create': 0, 'hold_create': False, 'pending_create': None}
                database.execute('INSERT INTO remote_state VALUES(1,?)', (json.dumps(state),))

    def transact(self, action):
        with self.lock, sqlite3.connect(self.path) as database:
            database.execute('PRAGMA synchronous=FULL')
            database.execute('BEGIN IMMEDIATE')
            state = json.loads(database.execute('SELECT body FROM remote_state WHERE id=1').fetchone()[0])
            result = action(state)
            database.execute('UPDATE remote_state SET body=? WHERE id=1', (json.dumps(state),))
            return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *arguments):
        pass

    @property
    def store(self):
        return self.server.store

    def send(self, content, status=200, binary=False):
        encoded = content if isinstance(content, bytes) else json.dumps(content).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/octet-stream' if binary else 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def drop(self):
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()
        self.close_connection = True

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        self.store.transact(lambda state: state.setdefault('method_log', []).append({'method': 'GET', 'path': self.path}))
        if path == '/__redirect':
            self.send_response(302)
            self.send_header('Location', 'https://example.com/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        def read(state):
            fixture = state['fixture']
            verification_fault = state.get('verification_fault', '')
            if path == '/__fixture':
                return state, 200, False
            if path.startswith('/developer/v1/transactions/'):
                return fixture['transaction'], 200, False
            if path.startswith('/developer/v1/receipts/'):
                receipt = copy.deepcopy(fixture['receipt'])
                receipt['receipt_url'] = state.get('receipt_url_override') or self.server.base_url + '/__original'
                return receipt, 200, False
            if path == '/__original':
                return base64.b64decode(fixture['evidence']['receipt']['original_base64']), 200, True
            fault = state['metadata_fault']
            if path == '/3/companyinformation':
                company = 'wrong-company' if fault == 'wrong_company' else fixture['config']['fortnox_company_id']
                if verification_fault == 'malformed_company':
                    return {'CompanyInformation': 'malformed'}, 200, False
                return {'CompanyInformation': {'OrganizationNumber': company}}, 200, False
            if path == '/3/financialyears':
                return {'FinancialYears': [] if fault == 'missing_year' else
                        [{'Id': fixture['config']['financial_year'], 'FromDate': '2026-01-01',
                          'ToDate': '2026-12-31'}]}, 200, False
            if path == '/3/voucherseries':
                return {'VoucherSeries': [] if fault == 'missing_series' else
                        [{'Code': fixture['config']['series']}]}, 200, False
            if path.startswith('/3/accounts/'):
                return {'Account': {'Number': int(path.rsplit('/', 1)[1]),
                                    'Active': fault != 'inactive_account' and
                                    int(path.rsplit('/', 1)[1]) != state.get('inactive_account_number')}}, 200, False
            if path == '/3/vouchers':
                if state['read_fault'] == 'list_fail':
                    return {'error': 'fixture list read failure'}, 503, False
                state['list_pages'] += 1
                vouchers = sorted(state['vouchers'], key=lambda voucher: voucher['number'])
                page = int(query.get('page', ['1'])[0])
                size = state['page_size']
                if verification_fault == 'late_page_fail' and page == 2:
                    return {'error': 'late page failure'}, 503, False
                total = max(1, (len(vouchers) + size - 1) // size)
                selected = vouchers[(page - 1) * size:page * size]
                if verification_fault in ['repeat_identity', 'conflicting_detail'] and page == 2:
                    selected = vouchers[:size]
                if verification_fault == 'changed_total' and page == 2:
                    total += 1
                return {'Vouchers': [{'VoucherNumber': voucher['number']} for voucher in selected],
                        'MetaInformation': {'@TotalPages': total, '@CurrentPage': page + 1 if verification_fault == 'wrong_current_page' else page}}, 200, False
            if path.startswith('/3/vouchers/'):
                if state['read_fault'] == 'detail_fail':
                    return {'error': 'fixture full detail read failure'}, 503, False
                state['detail_reads'] += 1
                number = int(path.rsplit('/', 1)[1])
                if verification_fault == 'late_detail_fail' and number == max(v['number'] for v in state['vouchers']):
                    return {'error': 'late detail failure'}, 503, False
                voucher = next((voucher for voucher in state['vouchers'] if voucher['number'] == number), None)
                if not voucher:
                    return {'error': 'not found'}, 404, False
                detail = copy.deepcopy(voucher['payload']['Voucher'])
                detail.update({'VoucherNumber': number, 'Year': fixture['config']['financial_year'], 'Manual': False})
                if verification_fault == 'conflicting_detail' and state['detail_reads'] > 1:
                    detail['TransactionDate'] = '2026-10-02'
                if verification_fault == 'foreign_year':
                    detail['Year'] = 2
                token = state.get('verification_numeric_token')
                if token is not None:
                    encoded = json.dumps({'Voucher': detail}).replace('"Debit": "1000.00"', '"Debit": ' + token, 1)
                    return encoded.encode(), 200, False
                return {'Voucher': detail}, 200, False
            if path == '/__proof/files':
                return {'files': state['files']}, 200, False
            if path.startswith('/__proof/files/'):
                if verification_fault == 'receipt_fail':
                    return {'error': 'unreadable receipt'}, 503, False
                file_id = path.rsplit('/', 1)[1]
                file = next((file for file in state['files'] if file['id'] == file_id), None)
                if not file:
                    return {'error': 'not found'}, 404, False
                content = b'corrupt receipt' if verification_fault == 'corrupt_receipt' else base64.b64decode(file['original_base64'])
                return content, 200, True
            if path == '/__proof/connections':
                number = int(query.get('voucher_number', ['0'])[0])
                connections = copy.deepcopy([connection for connection in state['connections']
                                             if connection['voucher_number'] == number])
                if verification_fault == 'missing_connection':
                    connections = []
                if verification_fault == 'wrong_connection':
                    for connection in connections:
                        connection['voucher_year'] = 2
                        connection['voucher_series'] = 'X'
                if verification_fault == 'malformed_connection_coverage':
                    return {'connections': connections, 'coverage': 'malformed'}, 200, False
                return {'connections': connections, 'coverage': {
                    'protocol': 'fixture-connections-v1', 'complete': verification_fault != 'partial_connections',
                    'voucher_number': number}}, 200, False
            return {'error': 'unknown fixture route'}, 404, False

        content, status, binary = self.store.transact(read)
        self.send(content, status, binary)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        parsed = urlsplit(self.path)
        path = parsed.path
        self.store.transact(lambda state: state.setdefault('method_log', []).append({'method': 'POST', 'path': self.path}))
        delay = self.store.transact(lambda state: state['delay_create'] if path == '/3/vouchers' else 0)
        if delay:
            time.sleep(delay)
        if path == '/3/vouchers':
            while self.store.transact(lambda state: state['hold_create']):
                time.sleep(0.02)

        def write(state):
            if path == '/__fixture':
                if body.get('reload_fixture'):
                    state['fixture'] = json.loads(Path(self.store.fixture_path).read_text())
                for key in ['fault', 'metadata_fault', 'read_fault', 'page_size', 'delay_create', 'hold_create', 'receipt_url_override', 'inactive_account_number', 'verification_fault', 'verification_numeric_token']:
                    if key in body:
                        state[key] = body[key]
                if 'verification_fault' in body:
                    state['detail_reads'] = 0
                if body.get('commit_pending') and state['pending_create']:
                    payload = state['pending_create']
                    number = max((voucher['number'] for voucher in state['vouchers']), default=0) + 1
                    state['vouchers'].append({'number': number, 'payload': payload})
                    state['pending_create'] = None
                if body.get('tamper_voucher'):
                    state['vouchers'][0]['payload']['Voucher']['VoucherRows'][0]['Debit'] = '999.00'
                if 'voucher_row_fields' in body:
                    state['vouchers'][0]['payload']['Voucher']['VoucherRows'][0].update(body['voucher_row_fields'])
                if body.get('tamper_file'):
                    state['files'][0]['original_base64'] = base64.b64encode(b'wrong original receipt').decode()
                if body.get('erase_vouchers'):
                    state['vouchers'] = []
                if body.get('wrong_connection_identity'):
                    for connection in state['connections']:
                        connection['voucher_series'] = 'X'
                        connection['voucher_year'] = 2
                verification_seed = body.get('verification_seed')
                if verification_seed:
                    baseline = state.setdefault('verification_baseline', copy.deepcopy(state['vouchers']))
                    state['vouchers'] = copy.deepcopy(baseline)
                    state['detail_reads'] = 0
                    target = state['vouchers'][0]
                    if 'duplicate' in verification_seed:
                        extra = copy.deepcopy(target)
                        extra['number'] = 2
                        state['vouchers'].append(extra)
                    if 'foreign' in verification_seed:
                        extra = copy.deepcopy(target)
                        extra['number'] = max(v['number'] for v in state['vouchers']) + 1
                        extra['payload']['Voucher']['Description'] = 'manual foreign voucher'
                        state['vouchers'].append(extra)
                    if 'mismatch' in verification_seed:
                        target['payload']['Voucher']['VoucherRows'][0]['Debit'] = '999.00'
                    if verification_seed == 'field_mismatch':
                        target['payload']['Voucher']['TransactionDate'] = '2026-10-02'
                        target['payload']['Voucher']['VoucherRows'][0]['Account'] = 9999
                        target['payload']['Voucher']['VoucherSeries'] = 'X'
                seed = body.get('seed')
                if seed == 'known_before_and_after':
                    original = copy.deepcopy(state['vouchers'][0])
                    original['number'] = 2
                    seeded = []
                    for number, marker in zip([1, 3], body['known_markers']):
                        payload = copy.deepcopy(original['payload'])
                        payload['Voucher']['Description'] = marker
                        seeded.append({'number': number, 'payload': payload})
                    state['vouchers'] = [seeded[0], original, seeded[1]]
                    state['seed_baseline'] = copy.deepcopy(state['vouchers'])
                    state['list_pages'] = 0
                    state['detail_reads'] = 0
                elif seed in ['foreign', 'duplicate', 'mismatch']:
                    state['vouchers'] = copy.deepcopy(state.get('seed_baseline', state['vouchers']))
                    target = next(voucher for voucher in state['vouchers'] if voucher['number'] == 2)
                    if seed == 'foreign':
                        extra = copy.deepcopy(target)
                        extra['number'] = 4
                        extra['payload']['Voucher']['Description'] = 'manual foreign voucher'
                        state['vouchers'].append(extra)
                    elif seed == 'duplicate':
                        extra = copy.deepcopy(target)
                        extra['number'] = 4
                        state['vouchers'].append(extra)
                    else:
                        target['payload']['Voucher']['VoucherRows'][0]['Debit'] = '999.00'
                return state, 200, False
            fault = state['fault']
            kind = {'/3/vouchers': 'create', '/3/inbox': 'upload',
                    '/3/voucherfileconnections': 'connect',
                    '/developer/v1/accounting/syncs': 'ack'}.get(path)
            if not kind:
                return {'error': 'unknown fixture route'}, 404, False
            injected = fault.startswith(kind + '_')
            if injected:
                state['fault'] = ''
            if kind == 'create':
                state['create_attempts'] += 1
            if injected and fault.endswith('_reject'):
                return {'error': 'definitive fixture non-execution'}, 422, False
            if injected and fault.endswith('_drop_no_commit'):
                if kind == 'create':
                    state['pending_create'] = copy.deepcopy(body)
                return None, 0, True
            if kind == 'create':
                number = max((voucher['number'] for voucher in state['vouchers']), default=0) + 1
                state['vouchers'].append({'number': number, 'payload': copy.deepcopy(body)})
                voucher = copy.deepcopy(body['Voucher'])
                voucher.update({'VoucherNumber': number, 'Year': state['fixture']['config']['financial_year']})
                result = {'Voucher': voucher}
            elif kind == 'upload':
                original = base64.b64decode(body['original_base64'], validate=True)
                if hashlib.sha256(original).hexdigest() != body['sha256']:
                    return {'error': 'invalid uploaded digest'}, 422, False
                file_id = str(uuid.uuid4())
                state['files'].append({'id': file_id, 'operation_id': body['operation_id'],
                                      'sha256': body['sha256'], 'original_base64': body['original_base64']})
                result = {'File': {'Id': file_id}}
            elif kind == 'connect':
                connection = body['VoucherFileConnection']
                file = next((file for file in state['files'] if file['id'] == connection['FileId']), None)
                voucher = next((voucher for voucher in state['vouchers']
                                if voucher['number'] == connection['VoucherNumber']), None)
                if file is None or voucher is None:
                    return {'error': 'missing file or voucher'}, 422, False
                state['connections'].append({'file_id': file['id'], 'sha256': file['sha256'],
                                             'voucher_number': voucher['number'],
                                             'voucher_series': connection['VoucherSeries'],
                                             'voucher_year': connection['VoucherYear']})
                result = body
            else:
                existing = next((sync for sync in state['syncs']
                                 if sync['payload']['idempotency_key'] == body['idempotency_key']), None)
                if existing:
                    if existing['payload'] != body:
                        return {'error': 'idempotency body changed'}, 422, False
                    result = {'sync_id': existing['id']}
                else:
                    sync_id = str(uuid.uuid4())
                    state['syncs'].append({'id': sync_id, 'payload': copy.deepcopy(body)})
                    result = {'sync_id': sync_id}
            if kind == 'ack':
                state['fixture']['transaction']['sync_status'] = 'SYNCED'
                state['fixture']['transaction']['updated_at'] = '2026-10-01T13:01:00Z'
                state['fixture']['transaction']['synced_at'] = '2026-10-01T13:01:00Z'
            if injected and fault.endswith('_drop_commit'):
                return None, 0, True
            if injected and fault.endswith('_malformed'):
                return {}, 201, False
            return result, 201, False

        content, status, drop = self.store.transact(write)
        if drop:
            self.drop()
        else:
            self.send(content, status)


def main():
    parser = argparse.ArgumentParser(description='Local durable fixture API. No vendor connectivity.')
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--ready-file', required=True, type=Path)
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.store = Store(str(args.state), args.fixture)
    server.base_url = 'http://127.0.0.1:' + str(server.server_port)
    args.ready_file.write_text(json.dumps({'base_url': server.base_url}))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
