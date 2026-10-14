from decimal import Decimal
import hashlib
import os
from urllib.parse import urlencode

from .domain import digest, operation_key, plan_purchase, Refusal, require, source_snapshot, supported_p0_row_semantics
from .journal import STEPS
from .transport import ExternalFailure, FixtureHTTP


class Connector:
    def __init__(self, fixture, journal, base_url, crash_after_intent=None):
        self.fixture = fixture
        self.journal = journal
        self.http = FixtureHTTP(base_url, journal)
        self.crash_after_intent = crash_after_intent

    def source(self):
        transaction_id = self.fixture['transaction']['id']
        transaction = self.http.request('GET', '/developer/v1/transactions/' + transaction_id)
        receipt_ids = transaction.get('receipts') or []
        if not receipt_ids:
            raise Refusal('one linked receipt is required')
        receipt = self.http.request('GET', '/developer/v1/receipts/' + receipt_ids[0] + '?include_ocr_data=true')
        original = self.http.request('GET', receipt['receipt_url'], binary=True)
        return source_snapshot(self.fixture, transaction, receipt, original), original

    def terminal(self):
        key = operation_key(self.fixture['config'], self.fixture['transaction']['id'])
        try:
            stored = self.journal.plan(key)
        except Refusal:
            return None
        if self.journal.result(key)['state'] not in ['complete', 'investigate']:
            return None
        require(stored.source['config'] == self.fixture['config'] and
                stored.source['evidence'] == self.fixture['evidence'],
                'stale terminal operation binding or evidence differs')
        return stored

    def plan(self):
        completed = self.terminal()
        if completed is not None:
            result = self.journal.result(completed.operation_id)
            result.update({'posting': completed.payload, 'fingerprint': completed.fingerprint,
                           'receipt_sha256': completed.source['original_sha256']})
            return result
        source, original = self.source()
        plan = plan_purchase(source, original)
        source['fortnox_preflight'] = self.metadata(plan)
        plan = plan_purchase(source, original)
        self.journal.register(plan)
        result = self.journal.result(plan.operation_id)
        result.update({'posting': plan.payload, 'fingerprint': plan.fingerprint,
                       'receipt_sha256': source['original_sha256'],
                       'evidence': source['evidence']})
        return result

    def frozen(self):
        config = self.fixture['config']
        key = operation_key(config, self.fixture['transaction']['id'])
        plan = self.journal.plan(key)
        self.http.operation_id = plan.operation_id
        source, original = self.source()
        expected_source = dict(plan.source)
        expected_source.pop('fortnox_preflight', None)
        steps = self.journal.steps(plan.operation_id)
        if (steps['ack']['state'] in ['repairable', 'confirmed'] and
                all(steps[kind]['state'] == 'confirmed' for kind in ['create', 'upload', 'connect']) and
                source['transaction'].get('sync_status') == 'SYNCED'):
            source['transaction'] = dict(source['transaction'])
            for field in ['sync_status', 'updated_at', 'synced_at']:
                if field in expected_source['transaction']:
                    source['transaction'][field] = expected_source['transaction'][field]
                else:
                    source['transaction'].pop(field, None)
        require(digest(source) == digest(expected_source),
                'stale frozen plan; source, evidence, mapping or receipt changed')
        source['fortnox_preflight'] = self.metadata(plan)
        if digest({'source': source, 'posting': plan.payload}) != plan.fingerprint:
            raise Refusal('stale frozen plan; source, evidence, mapping or receipt changed')
        current = plan_purchase(source, original)
        require(current.payload == plan.payload, 'stale posting payload')
        return plan

    def metadata(self, plan):
        config = plan.source['config']
        company = self.http.request('GET', '/3/companyinformation')
        require(company.get('CompanyInformation', {}).get('OrganizationNumber') ==
                config['fortnox_company_id'], 'wrong Fortnox company after connection')
        years = self.http.request('GET', '/3/financialyears')
        year = next((year for year in years.get('FinancialYears', [])
                     if year.get('Id') == config['financial_year']), None)
        require(year is not None and year['FromDate'] <= plan.payload['Voucher']['TransactionDate'] <=
                year['ToDate'], 'financial year missing for voucher date')
        series = self.http.request('GET', '/3/voucherseries?' + urlencode({'financialyear': config['financial_year']}))
        require(any(item.get('Code') == config['series'] for item in series.get('VoucherSeries', [])),
                'owned voucher series is missing')
        accounts = []
        for account in [row['Account'] for row in plan.payload['Voucher']['VoucherRows']]:
            result = self.http.request('GET', '/3/accounts/' + str(account) + '?' +
                                       urlencode({'financialyear': config['financial_year']}))
            require(result.get('Account', {}).get('Number') == account and
                    result['Account'].get('Active') is True, 'posting account is inactive or missing')
            accounts.append(result['Account'])
        return {'company': company, 'year': year, 'series': config['series'], 'accounts': accounts}

    def matches(self, remote, plan):
        expected = plan.payload['Voucher']
        if (remote.get('Year') != plan.source['config']['financial_year'] or
                any(remote.get(field) != expected[field]
                    for field in ['VoucherSeries', 'TransactionDate', 'Description'])):
            return False
        rows = remote.get('VoucherRows')
        if not isinstance(rows, list) or len(rows) != len(expected['VoucherRows']):
            return False
        try:
            for actual, wanted in zip(rows, expected['VoucherRows']):
                if not supported_p0_row_semantics(actual):
                    return False
                if actual.get('Account') != wanted['Account']:
                    return False
                if any(Decimal(str(actual[field])) != Decimal(wanted[field]) for field in ['Debit', 'Credit']):
                    return False
        except (KeyError, ValueError, ArithmeticError):
            return False
        return True

    def series(self, plan):
        config = plan.source['config']
        page, total_pages, vouchers = 1, None, []
        while True:
            query = urlencode({'financialyear': config['financial_year'],
                               'voucherseries': config['series'], 'page': page})
            listing = self.http.request('GET', '/3/vouchers?' + query)
            pages = listing.get('MetaInformation', {}).get('@TotalPages')
            require(type(pages) is int and 1 <= pages <= 10000, 'incomplete series pagination')
            require(total_pages is None or total_pages == pages, 'series pagination changed')
            total_pages = pages
            summaries = listing.get('Vouchers')
            require(isinstance(summaries, list), 'incomplete voucher list')
            for summary in summaries:
                number = summary.get('VoucherNumber')
                require(type(number) is int and number > 0, 'voucher list identity missing')
                result = self.http.request('GET', '/3/vouchers/{}/{}?{}'.format(config['series'], number,
                    urlencode({'financialyear': config['financial_year']})))
                remote = result.get('Voucher')
                require(isinstance(remote, dict) and remote.get('VoucherNumber') == number,
                        'full voucher details missing')
                vouchers.append(remote)
            if page == total_pages:
                break
            page += 1
        known = self.journal.known_plans()
        keys = set()
        for voucher in vouchers:
            key = voucher.get('Description')
            if key not in known:
                return vouchers, 'foreign or unknown operation key in owned series'
            if key in keys:
                return vouchers, 'duplicate operation key in owned series'
            keys.add(key)
            if not self.matches(voucher, known[key]):
                return vouchers, 'operation key has mismatching voucher payload'
        return vouchers, None

    def voucher_identity(self, voucher, plan):
        require(self.matches(voucher, plan), 'voucher response does not match frozen posting')
        number = voucher.get('VoucherNumber')
        require(type(number) is int and number > 0, 'voucher response lacks identity')
        return {'number': number, 'series': voucher['VoucherSeries'], 'year': voucher['Year']}

    def request_for(self, kind, plan, steps):
        if kind == 'create':
            return '/3/vouchers?' + urlencode({'financialyear': plan.source['config']['financial_year']}), plan.payload
        if kind == 'upload':
            return '/3/inbox?path=Inbox_v', {'operation_id': plan.operation_id,
                'sha256': plan.source['original_sha256'], 'original_base64': plan.receipt_base64}
        voucher = steps['create']['remote']
        if kind == 'connect':
            return '/3/voucherfileconnections', {'VoucherFileConnection': {
                'VoucherNumber': voucher['number'], 'VoucherSeries': voucher['series'],
                'VoucherYear': voucher['year'], 'FileId': steps['upload']['remote']['id']}}
        return '/developer/v1/accounting/syncs', {
            'idempotency_key': plan.operation_id + ':ack', 'sync_type': 'TRANSACTION_SYNC',
            'mark_ready_to_sync': False, 'successful_syncs': [{
                'id': plan.source['transaction']['id'],
                'reference_id': '{}:{}:{}'.format(voucher['year'], voucher['series'], voucher['number'])}]}

    def attachment_matches(self, connection, plan, steps):
        voucher = steps['create']['remote']
        return (connection.get('file_id') == steps['upload']['remote']['id'] and
                connection.get('sha256') == plan.source['original_sha256'] and
                connection.get('voucher_number') == voucher['number'] and
                connection.get('voucher_series') == voucher['series'] and
                connection.get('voucher_year') == voucher['year'])

    def confirm_response(self, kind, result, plan, steps):
        if kind == 'create':
            identity = self.voucher_identity(result.get('Voucher', {}), plan)
            detail = self.http.request('GET', '/3/vouchers/{}/{}?{}'.format(
                identity['series'], identity['number'], urlencode({'financialyear': identity['year']})))
            require(self.voucher_identity(detail.get('Voucher', {}), plan) == identity,
                    'voucher confirmation identity differs')
            return identity
        if kind == 'upload':
            file_id = result.get('File', {}).get('Id')
            require(isinstance(file_id, str) and file_id, 'upload response lacks identity')
            content = self.http.request('GET', '/__proof/files/' + file_id, binary=True)
            require(hashlib.sha256(content).hexdigest() == plan.source['original_sha256'],
                    'uploaded receipt bytes differ')
            return {'id': file_id, 'sha256': plan.source['original_sha256']}
        if kind == 'connect':
            expected = self.request_for(kind, plan, steps)[1]['VoucherFileConnection']
            connection = result.get('VoucherFileConnection', {})
            require(all(connection.get(key) == value for key, value in expected.items()),
                    'connection response lacks confirmed attachment identity')
            connections = self.http.request('GET', '/__proof/connections?' +
                urlencode({'voucher_number': steps['create']['remote']['number']}))['connections']
            matches = [connection for connection in connections
                       if self.attachment_matches(connection, plan, steps)]
            require(len(matches) == 1, 'attachment proof is incomplete')
            return {'file_id': steps['upload']['remote']['id'],
                    'voucher_number': steps['create']['remote']['number'],
                    'voucher_series': steps['create']['remote']['series'],
                    'voucher_year': steps['create']['remote']['year'],
                    'sha256': plan.source['original_sha256']}
        sync_id = result.get('sync_id')
        require(isinstance(sync_id, str) and bool(sync_id), 'ack response lacks sync identity')
        return {'sync_id': sync_id, 'idempotency_key': plan.operation_id + ':ack'}

    def attachment_preflight(self, plan, steps):
        voucher = steps['create']['remote']
        detail = self.http.request('GET', '/3/vouchers/{}/{}?{}'.format(
            voucher['series'], voucher['number'], urlencode({'financialyear': voucher['year']})))
        if (not self.matches(detail.get('Voucher', {}), plan) or
                detail.get('Voucher', {}).get('VoucherNumber') != voucher['number']):
            self.journal.reconcile_step(plan.operation_id, 'create', 'investigate',
                                        reason='confirmed voucher no longer matches frozen posting')
            return False
        content = self.http.request('GET', '/__proof/files/' + steps['upload']['remote']['id'], binary=True)
        if hashlib.sha256(content).hexdigest() != plan.source['original_sha256']:
            self.journal.reconcile_step(plan.operation_id, 'upload', 'investigate',
                                        reason='confirmed receipt bytes no longer match original')
            return False
        connections = self.http.request('GET', '/__proof/connections?' +
            urlencode({'voucher_number': voucher['number']}))['connections']
        matches = [connection for connection in connections if self.attachment_matches(connection, plan, steps)]
        if len(matches) != 1 or len(connections) != 1:
            self.journal.reconcile_step(plan.operation_id, 'connect', 'investigate',
                                        reason='confirmed attachment identity no longer matches')
            return False
        return True

    def apply(self):
        with self.journal.dispatcher():
            completed = self.terminal()
            if completed is not None:
                return self.journal.result(completed.operation_id)
            plan = self.frozen()
            result = self.journal.result(plan.operation_id)
            if result['state'] in ['complete', 'uncertain', 'investigate']:
                return result
            preflight = dict(plan.source['fortnox_preflight'])
            steps = self.journal.steps(plan.operation_id)
            if steps['create']['state'] == 'ready':
                vouchers, conflict = self.series(plan)
                if conflict:
                    self.journal.reconcile_step(plan.operation_id, 'create', 'investigate', reason=conflict)
                    return self.journal.result(plan.operation_id)
                matches = [voucher for voucher in vouchers if voucher['Description'] == plan.operation_id]
                if matches:
                    self.journal.reconcile_step(plan.operation_id, 'create', 'confirmed',
                                                self.voucher_identity(matches[0], plan))
                preflight['last_voucher_number'] = max((voucher['VoucherNumber'] for voucher in vouchers), default=0)
            for kind in STEPS:
                steps = self.journal.steps(plan.operation_id)
                if steps[kind]['state'] == 'confirmed':
                    continue
                if steps[kind]['state'] not in ['ready', 'repairable']:
                    return self.journal.result(plan.operation_id)
                if kind == 'ack' and not self.attachment_preflight(plan, steps):
                    return self.journal.result(plan.operation_id)
                path, request = self.request_for(kind, plan, steps)
                attempt = self.journal.begin_attempt(plan.operation_id, kind, request, preflight)
                if self.crash_after_intent == kind:
                    print(__import__('json').dumps(self.journal.result(plan.operation_id)), flush=True)
                    os._exit(91)
                try:
                    response = self.http.request('POST', path, request)
                    remote = self.confirm_response(kind, response, plan, steps)
                    self.journal.finish_attempt(plan.operation_id, kind, attempt, 'confirmed', remote)
                except (ExternalFailure, Refusal, KeyError, TypeError, ValueError) as error:
                    repairable = kind == 'ack' or (kind in ['upload', 'connect'] and
                        isinstance(error, ExternalFailure) and error.definitive)
                    self.journal.finish_attempt(plan.operation_id, kind, attempt,
                        'repairable' if repairable else 'uncertain', reason=str(error))
                    return self.journal.result(plan.operation_id)
            return self.journal.result(plan.operation_id)

    def reconcile(self):
        with self.journal.dispatcher():
            plan = self.frozen()
            self.metadata(plan)
            try:
                vouchers, conflict = self.series(plan)
                if conflict:
                    self.journal.reconcile_step(plan.operation_id, 'create', 'investigate', reason=conflict)
                    return self.journal.result(plan.operation_id)
                matches = [voucher for voucher in vouchers if voucher['Description'] == plan.operation_id]
                steps = self.journal.steps(plan.operation_id)
                if not matches:
                    if steps['create']['state'] in ['uncertain', 'confirmed']:
                        self.journal.reconcile_step(plan.operation_id, 'create',
                            'investigate' if steps['create']['state'] == 'confirmed' else 'uncertain',
                            reason='no matching voucher; confirmed identity missing or non-execution unprovable')
                    return self.journal.result(plan.operation_id)
                identity = self.voucher_identity(matches[0], plan)
                self.journal.reconcile_step(plan.operation_id, 'create', 'confirmed', identity)
                steps = self.journal.steps(plan.operation_id)
                if steps['upload']['state'] == 'uncertain':
                    files = self.http.request('GET', '/__proof/files')['files']
                    matches = []
                    for file in files:
                        if file.get('operation_id') == plan.operation_id:
                            content = self.http.request('GET', '/__proof/files/' + file['id'], binary=True)
                            if hashlib.sha256(content).hexdigest() == plan.source['original_sha256']:
                                matches.append(file)
                            else:
                                self.journal.reconcile_step(plan.operation_id, 'upload', 'investigate',
                                                            reason='receipt file mismatch')
                                return self.journal.result(plan.operation_id)
                    if len(matches) == 1:
                        self.journal.reconcile_step(plan.operation_id, 'upload', 'confirmed',
                            {'id': matches[0]['id'], 'sha256': plan.source['original_sha256']})
                    elif len(matches) > 1:
                        self.journal.reconcile_step(plan.operation_id, 'upload', 'investigate',
                                                    reason='duplicate receipt files')
                steps = self.journal.steps(plan.operation_id)
                if steps['connect']['state'] == 'uncertain' and steps['upload']['state'] == 'confirmed':
                    connections = self.http.request('GET', '/__proof/connections?' +
                        urlencode({'voucher_number': identity['number']}))['connections']
                    matches = [connection for connection in connections
                               if self.attachment_matches(connection, plan, steps)]
                    if len(matches) == 1:
                        self.journal.reconcile_step(plan.operation_id, 'connect', 'confirmed',
                            {'file_id': steps['upload']['remote']['id'], 'voucher_number': identity['number'],
                             'voucher_series': identity['series'], 'voucher_year': identity['year'],
                             'sha256': plan.source['original_sha256']})
                    elif len(matches) > 1:
                        self.journal.reconcile_step(plan.operation_id, 'connect', 'investigate',
                                                    reason='duplicate receipt connections')
            except (ExternalFailure, Refusal, KeyError, TypeError, ValueError):
                return self.journal.result(plan.operation_id)
            return self.journal.result(plan.operation_id)
