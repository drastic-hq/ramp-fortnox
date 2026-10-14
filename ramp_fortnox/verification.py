import base64
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import build_opener, ProxyHandler, Request

from .domain import canonical, digest, FrozenPlan, load_json, operation_key, plan_purchase, Refusal, require
from .transport import loopback_url, NoRedirects


POLICY = {'provider': 'fortnox', 'country': 'SE', 'currency': 'SEK', 'posting_policy': 'P0'}
STATUSES = ('Confirmed', 'Missing', 'Duplicate', 'Mismatched', 'Unknown')


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ObservationScope:
    company_id: str
    financial_year: int
    series: str
    ramp_business_id: str
    ramp_entity_id: str


@dataclass(frozen=True)
class SourcePopulation:
    scope: ObservationScope
    snapshot_started_at: str
    snapshot_finished_at: str
    selected_operation_ids: list
    excluded_operations: list
    invalid_operations: list
    missing_requested_ids: list
    plans: list
    snapshot_digest: str
    kind: str = 'local_frozen_plans'
    ramp_population_coverage: str = 'not_enumerated'


@dataclass
class Coverage:
    state: str = 'partial'
    company_bound: bool = False
    pages_requested: list = field(default_factory=list)
    pages_received: list = field(default_factory=list)
    declared_total_pages: object = None
    details_requested: list = field(default_factory=list)
    details_received: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    anomalies: list = field(default_factory=list)
    listing_complete: bool = False
    details_complete: bool = False
    temporal_limit: str = 'sequential acquisition; no atomic provider snapshot; later changes unobserved'


@dataclass(frozen=True)
class Observations:
    scope: ObservationScope
    started_at: str
    finished_at: str
    coverage: Coverage
    vouchers: list
    connections: dict
    files: dict
    accounts: dict
    financial_year: dict
    requests_digest: str


@dataclass(frozen=True)
class Finding:
    operation_id: object
    status: str
    reasons: list
    voucher_identities: list
    checks: dict
    count_is_exact: bool
    execution_outcome: str = 'unknown'
    retry_authorized: bool = False


@dataclass(frozen=True)
class VerificationReport:
    source_population: SourcePopulation
    observation_scope: ObservationScope
    observation_interval: dict
    coverage: Coverage
    findings: list
    observed_only: list
    counts: dict
    evidence_manifest: dict
    schema_version: str = 'sync-verification-v1'
    policy_version: str = 'fortnox-fixture-P0-v1'

    def as_dict(self):
        report = asdict(self)
        report['policy'] = POLICY
        report['policy_limits'] = [
            'Local frozen plans only; Ramp SYNC_READY and acknowledgement populations not enumerated.',
            'Fixture protocol only; no live Fortnox or Nordic interoperability proof.',
            'P0 Sweden SEK ordered rows; no account mapping or VAT derivation.',
            'Missing means not observed as of the bounded sequential scan; execution outcome remains unknown.',
            'No finding authorizes retry, repair, posting, source refresh or acknowledgement.',
            'Original bytes required; live attachment completeness unsupported.',
        ]
        decisions = {'policy': report['policy'], 'findings': report['findings'],
                     'observed_only': report['observed_only'], 'counts': report['counts'],
                     'coverage': report['coverage']}
        report['decision_digest'] = digest(decisions)
        return report


def integer(value, label, positive=False):
    require(type(value) is int and (not positive or value > 0), label + ' must be an integer')
    return value


def posting_rows(rows):
    require(isinstance(rows, list) and bool(rows), 'voucher rows missing')
    result = []
    for row in rows:
        account = integer(row['Account'], 'account', True)
        amounts = []
        for name in ['Debit', 'Credit']:
            require(isinstance(row[name], (str, int, Decimal)) and not isinstance(row[name], bool), 'invalid ' + name)
            try:
                amount = Decimal(row[name])
                require(amount.is_finite() and amount >= 0, name + ' must be exact nonnegative ore')
                numerator, denominator = amount.as_integer_ratio()
                ore, remainder = divmod(numerator * 100, denominator)
                require(remainder == 0, name + ' must be exact nonnegative ore')
                amounts.append(ore)
            except InvalidOperation:
                raise Refusal('invalid ' + name)
        result.append({'account': account, 'debit_ore': amounts[0], 'credit_ore': amounts[1]})
    return result


def parse_scope(value):
    scope = ObservationScope(**value)
    require(all(isinstance(value, str) and value for value in [scope.company_id, scope.series,
            scope.ramp_business_id, scope.ramp_entity_id]), 'scope identity missing')
    integer(scope.financial_year, 'financial year', True)
    return scope


def snapshot(database_path, scope, requested_ids):
    started = now()
    path = Path(database_path).resolve()
    require(path.is_file(), 'journal database does not exist; verify-sync never creates one')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        rows = [dict(row) for row in connection.execute('SELECT * FROM operations ORDER BY id')]
        steps = [dict(row) for row in connection.execute('SELECT * FROM steps ORDER BY operation_id,kind')]
        connection.commit()
    finally:
        connection.close()
    return {'schema_version': 'source-capture-v1', 'scope': asdict(scope), 'requested_operation_ids': requested_ids,
            'snapshot_started_at': started, 'snapshot_finished_at': now(), 'rows': rows, 'steps': steps,
            'snapshot_digest': digest({'rows': rows, 'steps': steps}), 'origin': 'read-only SQLite transaction'}


def validate_p0_source_shape(source):
    require(isinstance(source, dict), 'frozen source must be an object')
    for name in ['config', 'evidence', 'transaction', 'receipt']:
        require(isinstance(source[name], dict), 'frozen source ' + name + ' must be an object')
    config, evidence = source['config'], source['evidence']
    transaction, receipt = source['transaction'], source['receipt']
    for label, value in [
        ('config.tax_code_mapping', config.get('tax_code_mapping', {})),
        ('evidence.receipt', evidence.get('receipt', {})),
        ('evidence.units_calibration', evidence.get('units_calibration', {})),
        ('transaction.entity_amount', transaction.get('entity_amount', {})),
        ('transaction.merchant_amount', transaction.get('merchant_amount', {})),
        ('transaction.merchant_location', transaction.get('merchant_location', {})),
        ('receipt.ocr', receipt.get('ocr', {})),
    ]:
        require(isinstance(value, dict), 'frozen source ' + label + ' must be an object')
    for label, value in [
        ('transaction.accounting_field_selections', transaction.get('accounting_field_selections', [])),
        ('receipt.ocr.taxes', receipt.get('ocr', {}).get('taxes', [])),
    ]:
        require(isinstance(value, list) and all(isinstance(item, dict) for item in value),
                'frozen source ' + label + ' must be a list of objects')


def parse_population(captured):
    require(captured['schema_version'] == 'source-capture-v1', 'unsupported source capture')
    require(captured['origin'] == 'read-only SQLite transaction', 'unsupported source provenance')
    require(captured['snapshot_digest'] == digest({'rows': captured['rows'], 'steps': captured['steps']}),
            'source snapshot digest differs')
    scope = parse_scope(captured['scope'])
    requested = captured['requested_operation_ids']
    require(isinstance(requested, list) and all(isinstance(key, str) for key in requested), 'invalid operation selection')
    plans, invalid, excluded, selected = [], [], [], []
    seen = set()
    for row in captured['rows']:
        key = row['id']
        require(key not in seen, 'repeated source operation identity')
        seen.add(key)
        if requested and key not in requested:
            excluded.append({'operation_id': key, 'reason': 'not requested'})
            continue
        if row['business'] != scope.ramp_business_id or row['entity'] != scope.ramp_entity_id:
            excluded.append({'operation_id': key, 'reason': 'outside selected Ramp business/entity'})
            continue
        try:
            raw = load_json(row['plan_json'])
            config = raw['source']['config']
            if (config['fortnox_company_id'], config['financial_year'], config['series']) != (
                    scope.company_id, scope.financial_year, scope.series):
                excluded.append({'operation_id': key, 'reason': 'outside selected Fortnox company/year/series'})
                continue
            selected.append(key)
            validate_p0_source_shape(raw['source'])
            plan = FrozenPlan.restore(raw)
            require(plan.operation_id == key == operation_key(config, row['transaction_id']), 'operation identity differs')
            require(row['kind'] == 'card_purchase' and plan.source['transaction']['id'] == row['transaction_id'], 'source transaction differs')
            require(config['ramp_business_id'] == row['business'] and config['ramp_entity_id'] == row['entity'] and
                    plan.source['transaction']['entity_id'] == row['entity'], 'source binding differs')
            require(config['mode'] == 'fixture' and config['binding_verified'] is True and
                    config['mapping_revision'], 'unsupported source policy')
            require(row['fingerprint'] == plan.fingerprint == digest({'source': plan.source, 'posting': plan.payload}),
                    'frozen fingerprint differs')
            original = base64.b64decode(plan.receipt_base64, validate=True)
            sha = hashlib.sha256(original).hexdigest()
            receipt_evidence = plan.source['evidence']['receipt']
            require(sha == plan.source['original_sha256'] == receipt_evidence['sha256'], 'frozen receipt digest differs')
            require(base64.b64decode(receipt_evidence['original_base64'], validate=True) == original,
                    'frozen receipt evidence differs')
            validated = plan_purchase(plan.source, original)
            require(validated.as_dict() == plan.as_dict(), 'frozen source is not the supported P0 plan')
            voucher = plan.payload['Voucher']
            require(voucher['Description'] == key and voucher['VoucherSeries'] == scope.series, 'frozen posting binding differs')
            transaction_date = date.fromisoformat(voucher['TransactionDate']).isoformat()
            require(receipt_evidence['date'] == transaction_date and
                    receipt_evidence['transaction_id'] == row['transaction_id'] and
                    receipt_evidence['receipt_id'] == plan.source['receipt']['id'] and
                    receipt_evidence['currency'] == 'SEK', 'receipt source binding differs')
            stored_steps = [step for step in captured['steps'] if step['operation_id'] == key]
            require(sorted(step['kind'] for step in stored_steps) == ['ack', 'connect', 'create', 'upload'],
                    'frozen step references are incomplete or repeated')
            refs = {step['kind']: load_json(step['remote_json']) if step['remote_json'] else None for step in stored_steps}
            require(all(value is None or isinstance(value, dict) for value in refs.values()), 'invalid frozen step identity')
            created = refs['create']
            if created is not None:
                integer(created['number'], 'stored voucher number', True)
                require(created['year'] == scope.financial_year and created['series'] == scope.series, 'stored voucher scope differs')
            uploaded = refs['upload']
            if uploaded is not None:
                require(isinstance(uploaded['id'], str) and uploaded['id'] and uploaded['sha256'] == sha, 'stored original file identity differs')
            plans.append({'operation_id': key, 'fingerprint': plan.fingerprint, 'transaction_date': transaction_date,
                          'posting_rows': posting_rows(voucher['VoucherRows']), 'original_sha256': sha,
                          'mapping_revision': config['mapping_revision'], 'step_references': refs})
        except (Refusal, KeyError, TypeError, ValueError) as error:
            if key not in selected:
                selected.append(key)
            invalid.append({'operation_id': key, 'reason': str(error)})
    missing = sorted(set(requested) - seen)
    return SourcePopulation(scope, captured['snapshot_started_at'], captured['snapshot_finished_at'],
                            sorted(selected), excluded, invalid, missing, plans, captured['snapshot_digest'])


class ReadOnlyHTTP:
    def __init__(self, base_url):
        self.base_url = loopback_url(base_url).rstrip('/') + '/'
        require(urlsplit(self.base_url).path == '/' and not urlsplit(self.base_url).query, 'fixture base URL must be an origin')
        self.opener = build_opener(ProxyHandler({}), NoRedirects())
        self.requests = []

    def get(self, path, kind, reference=None, binary=False):
        url = loopback_url(urljoin(self.base_url, path))
        require(urlsplit(url).netloc == urlsplit(self.base_url).netloc, 'observation leaves fixture origin')
        record = {'method': 'GET', 'path': path, 'kind': kind, 'reference': reference,
                  'started_at': now(), 'status': None, 'error': None}
        content = b''
        try:
            with self.opener.open(Request(url, method='GET'), timeout=3) as response:
                record['status'] = response.status
                content = response.read()
        except HTTPError as error:
            record['status'] = error.code
            content = error.read()
            record['error'] = 'HTTP ' + str(error.code)
        except (URLError, OSError, ValueError) as error:
            record['error'] = str(error)
        record.update({'finished_at': now(), 'body_base64': base64.b64encode(content).decode(),
                       'body_sha256': hashlib.sha256(content).hexdigest()})
        self.requests.append(record)
        if record['error'] or record['status'] != 200:
            return None
        if binary:
            return content
        try:
            return load_json(content)
        except (ValueError, UnicodeError):
            return None


def company_matches(content, company_id):
    information = content.get('CompanyInformation') if isinstance(content, dict) else None
    return isinstance(information, dict) and information.get('OrganizationNumber') == company_id


def collect(population, base_url):
    scope = population.scope
    http = ReadOnlyHTTP(base_url)
    started = now()
    company = http.get('/3/companyinformation', 'company')
    if not company_matches(company, scope.company_id):
        return {'schema_version': 'observed-capture-v1', 'scope': asdict(scope), 'started_at': started,
                'finished_at': now(), 'requests': http.requests, 'origin': 'GET-only literal-loopback fixture'}
    http.get('/3/financialyears', 'year')
    http.get('/3/voucherseries?' + urlencode({'financialyear': scope.financial_year}), 'series')
    accounts = sorted({row['account'] for plan in population.plans for row in plan['posting_rows']})
    for account in accounts:
        http.get('/3/accounts/' + str(account) + '?' + urlencode({'financialyear': scope.financial_year}), 'account', account)
    page, total = 1, None
    while True:
        listing = http.get('/3/vouchers?' + urlencode({'financialyear': scope.financial_year,
                         'voucherseries': scope.series, 'page': page}), 'page', page)
        if not isinstance(listing, dict):
            break
        meta = listing.get('MetaInformation', {})
        count, current = meta.get('@TotalPages'), meta.get('@CurrentPage')
        if type(count) is not int or not 1 <= count <= 10000 or current != page or type(current) is not int:
            break
        if total is not None and total != count:
            break
        total = count
        entries = listing.get('Vouchers')
        if not isinstance(entries, list):
            break
        for entry in entries:
            number = entry.get('VoucherNumber') if isinstance(entry, dict) else None
            if type(number) is not int or number <= 0:
                continue
            detail = http.get('/3/vouchers/{}/{}?{}'.format(quote(scope.series, safe=''), number,
                              urlencode({'financialyear': scope.financial_year})), 'detail', number)
            if not isinstance(detail, dict):
                continue
            voucher = detail.get('Voucher', {})
            if not isinstance(voucher, dict) or voucher.get('Description') not in population.selected_operation_ids:
                continue
            inventory = http.get('/__proof/connections?' + urlencode({'voucher_number': number}), 'connections', number)
            if not isinstance(inventory, dict) or not isinstance(inventory.get('connections'), list):
                continue
            for connection in inventory['connections']:
                file_id = connection.get('file_id') if isinstance(connection, dict) else None
                if isinstance(file_id, str) and file_id:
                    http.get('/__proof/files/' + quote(file_id, safe=''), 'file', file_id, binary=True)
        if page >= total:
            break
        page += 1
    return {'schema_version': 'observed-capture-v1', 'scope': asdict(scope), 'started_at': started,
            'finished_at': now(), 'requests': http.requests, 'origin': 'GET-only literal-loopback fixture'}


def validate_request(record, scope):
    kind, reference = record['kind'], record['reference']
    query = urlencode({'financialyear': scope.financial_year})
    if kind in ['page', 'detail', 'connections', 'account']:
        integer(reference, 'captured request reference', True)
    elif kind == 'file':
        require(isinstance(reference, str) and reference, 'invalid captured file reference')
    else:
        require(reference is None, 'unexpected captured request reference')
    paths = {
        'company': '/3/companyinformation', 'year': '/3/financialyears',
        'series': '/3/voucherseries?' + query,
        'page': '/3/vouchers?' + urlencode({'financialyear': scope.financial_year, 'voucherseries': scope.series, 'page': reference}),
        'detail': '/3/vouchers/{}/{}?{}'.format(quote(scope.series, safe=''), reference, query),
        'account': '/3/accounts/{}?{}'.format(reference, query),
        'connections': '/__proof/connections?' + urlencode({'voucher_number': reference}),
        'file': '/__proof/files/' + quote(str(reference), safe=''),
    }
    require(kind in paths and record['path'] == paths[kind], 'captured request provenance does not match scope/reference')
    require(isinstance(record['started_at'], str) and isinstance(record['finished_at'], str), 'request acquisition interval missing')


def request_body(record):
    require(record['method'] == 'GET', 'captured observation contains non-GET request')
    content = base64.b64decode(record['body_base64'], validate=True)
    require(hashlib.sha256(content).hexdigest() == record['body_sha256'], 'raw request body digest differs')
    if record['error'] or record['status'] != 200:
        return None
    if record['kind'] == 'file':
        return content
    try:
        return load_json(content)
    except (ValueError, UnicodeError):
        return None


def parse_observations(captured):
    require(captured['schema_version'] == 'observed-capture-v1' and
            captured['origin'] == 'GET-only literal-loopback fixture', 'unsupported observation provenance')
    scope = parse_scope(captured['scope'])
    coverage = Coverage()
    vouchers, connections, files, accounts, year = {}, {}, {}, {}, {}
    entries, totals, series_seen = [], [], False
    company_records = 0
    for record in captured['requests']:
        validate_request(record, scope)
        content = request_body(record)
        kind, reference = record['kind'], record['reference']
        if kind == 'file':
            previous = files.get(reference)
            current = hashlib.sha256(content).hexdigest() if content is not None else None
            if previous is not None and previous != current:
                files[reference] = None
                coverage.anomalies.append('conflicting original bytes for ' + reference)
            else:
                files[reference] = current
            continue
        if kind == 'connections':
            complete = False
            inventory = []
            if isinstance(content, dict):
                metadata = content.get('coverage', {})
                complete = (isinstance(metadata, dict) and metadata.get('protocol') == 'fixture-connections-v1' and
                            metadata.get('complete') is True and metadata.get('voucher_number') == reference)
                if not isinstance(metadata, dict):
                    coverage.failures.append('voucher {} receipt connection coverage malformed'.format(reference))
                inventory = content.get('connections', [])
                complete = complete and isinstance(inventory, list)
            normalized = {'complete': complete, 'connections': inventory if isinstance(inventory, list) else [],
                          'read_succeeded': content is not None}
            key = str(reference)
            if key in connections and connections[key] != normalized:
                normalized['complete'] = False
                normalized['conflicting'] = True
            connections[key] = normalized
            continue
        if kind == 'page':
            coverage.pages_requested.append(reference)
            if not isinstance(content, dict):
                coverage.failures.append('page {} unreadable'.format(reference))
                continue
            try:
                metadata = content['MetaInformation']
                current = integer(metadata['@CurrentPage'], 'current page', True)
                total = integer(metadata['@TotalPages'], 'total pages', True)
                require(current == reference and total <= 10000, 'page metadata differs from request')
                require(isinstance(content['Vouchers'], list), 'voucher listing invalid')
                totals.append(total)
                coverage.pages_received.append(current)
                for entry in content['Vouchers']:
                    number = integer(entry['VoucherNumber'], 'voucher number', True)
                    entries.append(number)
            except (Refusal, KeyError, TypeError) as error:
                coverage.anomalies.append(str(error))
            continue
        if kind == 'detail':
            coverage.details_requested.append(reference)
            if not isinstance(content, dict):
                coverage.failures.append('voucher {} detail unreadable'.format(reference))
                continue
            try:
                voucher = content['Voucher']
                number = integer(voucher['VoucherNumber'], 'voucher number', True)
                financial_year = integer(voucher['Year'], 'voucher year', True)
                require(number == reference, 'voucher detail number differs')
                require(isinstance(voucher['Description'], str) and isinstance(voucher['VoucherSeries'], str), 'voucher marker or series invalid')
                normalized = {'identity': {'company_id': scope.company_id, 'year': financial_year,
                              'series': voucher['VoucherSeries'], 'number': number},
                              'marker': voucher['Description'], 'transaction_date': date.fromisoformat(voucher['TransactionDate']).isoformat(),
                              'posting_rows': posting_rows(voucher['VoucherRows'])}
                identity = canonical(normalized['identity'])
                if identity in vouchers and vouchers[identity] != normalized:
                    coverage.anomalies.append('conflicting full voucher details')
                else:
                    vouchers[identity] = normalized
                if financial_year != scope.financial_year or voucher['VoucherSeries'] != scope.series:
                    coverage.anomalies.append('voucher outside requested year/series')
                coverage.details_received.append(reference)
            except (Refusal, KeyError, ValueError, TypeError) as error:
                coverage.failures.append('voucher {} invalid detail: {}'.format(reference, error))
            continue
        if kind == 'company':
            company_records += 1
            coverage.company_bound = company_matches(content, scope.company_id)
            if not coverage.company_bound:
                coverage.failures.append('company binding not established')
        elif kind == 'year':
            try:
                years = content['FinancialYears']
                matches = [item for item in years if item['Id'] == scope.financial_year]
                require(len(matches) == 1, 'financial year not uniquely established')
                year = matches[0]
                require(date.fromisoformat(year['FromDate']) <= date.fromisoformat(year['ToDate']), 'invalid financial year interval')
            except (Refusal, KeyError, TypeError, ValueError) as error:
                coverage.failures.append('year metadata unavailable: ' + str(error))
        elif kind == 'series':
            try:
                series_seen = sum(item['Code'] == scope.series for item in content['VoucherSeries']) == 1
            except (KeyError, TypeError):
                series_seen = False
            if not series_seen:
                coverage.failures.append('series metadata unavailable')
        elif kind == 'account':
            try:
                account = content['Account']
                require(type(account['Number']) is int and account['Number'] == reference and
                        type(account['Active']) is bool, 'account metadata differs')
                accounts[str(reference)] = account
            except (Refusal, KeyError, TypeError):
                accounts[str(reference)] = None
    require(company_records == 1, 'observation must contain exactly one company binding read')
    if len(entries) != len(set(entries)):
        coverage.anomalies.append('repeated voucher identity in enumeration')
    if totals:
        coverage.declared_total_pages = totals[0]
        if len(set(totals)) != 1:
            coverage.anomalies.append('pagination total changed')
        expected_pages = list(range(1, totals[0] + 1))
        coverage.listing_complete = coverage.pages_requested == coverage.pages_received == expected_pages
    coverage.details_complete = sorted(entries) == sorted(coverage.details_requested) == sorted(coverage.details_received)
    complete = (coverage.company_bound and bool(year) and series_seen and coverage.listing_complete and
                coverage.details_complete and not coverage.failures and not coverage.anomalies)
    coverage.state = 'complete' if complete else 'partial'
    return Observations(scope, captured['started_at'], captured['finished_at'], coverage,
                        sorted(vouchers.values(), key=lambda voucher: canonical(voucher['identity'])),
                        connections, files, accounts, year, digest(captured['requests']))


def check(status, reasons=None, differences=None):
    return {'status': status, 'reasons': reasons or [], 'differences': differences or []}


def receipt_check(plan, voucher, observations):
    identity = voucher['identity']
    inventory = observations.connections.get(str(identity['number']))
    if not inventory or not inventory['read_succeeded']:
        return check('Unknown', ['receipt connection read unavailable'])
    differences, matched, unreadable = [], 0, False
    expected_file = (plan['step_references'].get('upload') or {}).get('id')
    for connection in inventory['connections']:
        if not isinstance(connection, dict):
            unreadable = True
            continue
        expected = {'voucher_number': identity['number'], 'voucher_year': observations.scope.financial_year,
                    'voucher_series': observations.scope.series}
        if expected_file:
            expected['file_id'] = expected_file
        binding = []
        for field_name, expected_value in expected.items():
            if connection.get(field_name) != expected_value:
                binding.append({'field': field_name, 'expected': expected_value, 'observed': connection.get(field_name)})
        differences.extend(binding)
        file_id = connection.get('file_id')
        sha = observations.files.get(file_id) if isinstance(file_id, str) else None
        if sha is None:
            unreadable = True
        elif sha != plan['original_sha256']:
            differences.append({'field': 'original_sha256', 'expected': plan['original_sha256'], 'observed': sha})
        elif not binding:
            matched += 1
    if differences:
        return check('Mismatched', ['positive attachment identity or original-byte difference'], differences)
    if not inventory['complete']:
        return check('Unknown', ['connection inventory completeness not established'])
    if not inventory['connections']:
        return check('Missing', ['no attachment in complete fixture connection inventory'])
    if unreadable or matched != 1:
        return check('Unknown', ['original-byte proof unavailable or not unique'])
    return check('Confirmed')


def compare(population, observations):
    require(population.scope == observations.scope, 'source and observation scope differ')
    complete = observations.coverage.state == 'complete'
    grouped = {}
    for voucher in observations.vouchers:
        grouped.setdefault(voucher['marker'], []).append(voucher)
    findings = []
    for invalid in population.invalid_operations:
        findings.append(Finding(invalid['operation_id'], 'Unknown', ['invalid frozen source: ' + invalid['reason']], [], {}, False))
    for key in population.missing_requested_ids:
        findings.append(Finding(key, 'Unknown', ['requested operation has no frozen local plan'], [], {}, False))
    for plan in population.plans:
        key = plan['operation_id']
        candidates = grouped.get(key, []) if observations.coverage.company_bound else []
        identities = [voucher['identity'] for voucher in candidates]
        if not candidates:
            status = 'Missing' if complete else 'Unknown'
            reason = ('not observed during completed bounded scan; execution outcome unknown' if complete else
                      'incomplete usable voucher enumeration or detail coverage')
            checks = {dimension: check('Unknown', ['no observed voucher']) for dimension in ['date', 'accounts', 'amounts', 'receipt']}
            checks['voucher'] = check(status, [reason])
            findings.append(Finding(key, status, [reason], [], checks, complete))
            continue
        checks = {}
        for dimension in ['voucher', 'date', 'accounts', 'amounts']:
            differences, unknown = [], []
            for voucher in candidates:
                if dimension == 'voucher':
                    expected = {'company_id': population.scope.company_id, 'year': population.scope.financial_year, 'series': population.scope.series}
                    created = plan['step_references'].get('create')
                    if created is not None:
                        expected['number'] = created['number']
                    for name, value in expected.items():
                        if voucher['identity'][name] != value:
                            differences.append({'field': name, 'expected': value, 'observed': voucher['identity'][name]})
                elif dimension == 'date':
                    if voucher['transaction_date'] != plan['transaction_date']:
                        differences.append({'field': 'transaction_date', 'expected': plan['transaction_date'], 'observed': voucher['transaction_date']})
                    year = observations.financial_year
                    if not year:
                        unknown.append('financial year interval unavailable')
                    elif not year['FromDate'] <= plan['transaction_date'] <= year['ToDate']:
                        unknown.append('frozen expected date outside observed financial year interval')
                else:
                    expected_rows, observed_rows = plan['posting_rows'], voucher['posting_rows']
                    if len(expected_rows) != len(observed_rows):
                        differences.append({'field': 'row_count', 'expected': len(expected_rows), 'observed': len(observed_rows)})
                    for index, (expected_row, observed_row) in enumerate(zip(expected_rows, observed_rows)):
                        names = ['account'] if dimension == 'accounts' else ['debit_ore', 'credit_ore']
                        for name in names:
                            if expected_row[name] != observed_row[name]:
                                differences.append({'field': 'rows[{}].{}'.format(index, name), 'expected': expected_row[name], 'observed': observed_row[name]})
                        if dimension == 'accounts':
                            account = observations.accounts.get(str(expected_row['account']))
                            if account is None:
                                unknown.append('expected account metadata unavailable')
                            elif not account['Active']:
                                unknown.append('expected account inactive at observation time; historical status unknown')
            checks[dimension] = check('Mismatched' if differences else 'Unknown' if unknown else 'Confirmed', unknown, differences)
        receipts = [receipt_check(plan, voucher, observations) for voucher in candidates]
        receipt_status = next((status for status in ['Mismatched', 'Missing', 'Unknown'] if any(item['status'] == status for item in receipts)), 'Confirmed')
        checks['receipt'] = check(receipt_status, [reason for item in receipts for reason in item['reasons']],
                                  [difference for item in receipts for difference in item['differences']])
        if len(candidates) >= 2:
            status, reasons = 'Duplicate', ['at least two distinct voucher identities carry this operation marker']
            checks['voucher'] = check('Duplicate', reasons, checks['voucher']['differences'])
        elif any(item['status'] == 'Mismatched' for item in checks.values()) or receipt_status == 'Missing':
            status, reasons = 'Mismatched', ['positive frozen posting or attachment difference']
        elif not complete or any(item['status'] == 'Unknown' for item in checks.values()):
            status, reasons = 'Unknown', ['coverage, posting metadata or original-byte evidence insufficient']
        else:
            status, reasons = 'Confirmed', ['one bounded-unique full voucher and exactly bound original bytes match']
        if not complete:
            reasons.append('partial scope coverage; additional vouchers and differences may exist')
            if checks['voucher']['status'] == 'Confirmed':
                checks['voucher'] = check('Unknown', ['bounded uniqueness not established'])
        findings.append(Finding(key, status, reasons, identities, checks, complete))
    selected = set(population.selected_operation_ids)
    observed_only = [Finding(voucher['marker'], 'Unknown', ['observed marker outside selected local frozen population'],
                            [voucher['identity']], {}, complete) for voucher in observations.vouchers if voucher['marker'] not in selected]
    findings.sort(key=lambda finding: finding.operation_id)
    counts = {status: sum(finding.status == status for finding in findings) for status in STATUSES}
    return VerificationReport(population, observations.scope,
        {'started_at': observations.started_at, 'finished_at': observations.finished_at}, observations.coverage,
        findings, observed_only, counts, {'source_snapshot_sha256': population.snapshot_digest,
                                        'requests_sha256': observations.requests_digest})


def envelope(payload):
    return {'payload': payload, 'sha256': digest(payload)}


def read_capture(path):
    captured = load_json(Path(path).read_text())
    require(captured['sha256'] == digest(captured['payload']), 'captured input digest differs')
    return captured['payload']


def write_report(report, source, observed, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = report.as_dict()
    artifacts = {name: json.dumps(content, indent=2, sort_keys=True) + '\n'
                 for name, content in [('source.json', envelope(source)), ('observed.json', envelope(observed)), ('report.json', result)]}
    lines = ['Fortnox SE SEK P0 fixture verification.',
             'Population. {} local frozen plans selected. Ramp completeness not enumerated.'.format(len(report.source_population.selected_operation_ids)),
             'Observation. {} through {}.'.format(observed['started_at'], observed['finished_at']),
             'Coverage. ' + report.coverage.state + '.',
             'Counts. ' + ', '.join('{} {}'.format(status, result['counts'][status]) for status in STATUSES) + '.',
             'Missing means not observed during this bounded scan. Execution remains unknown. Retry is never authorized.']
    for finding in report.findings:
        lines.append('{}. {}. {}.'.format(finding.operation_id, finding.status, '; '.join(finding.reasons)))
        for dimension, value in finding.checks.items():
            lines.append('  {}. {}. {}'.format(dimension, value['status'], canonical(value['differences']) if value['differences'] else '; '.join(value['reasons'])))
    for finding in report.observed_only:
        lines.append('Observed only. {}. Unknown.'.format(finding.operation_id))
    lines.extend('Coverage gap. ' + reason + '.' for reason in report.coverage.failures + report.coverage.anomalies)
    lines.extend(result['policy_limits'])
    artifacts['report.txt'] = '\n'.join(lines) + '\n'
    for name in artifacts:
        try:
            mode = (output / name).lstat().st_mode
        except FileNotFoundError:
            continue
        require(stat.S_ISREG(mode), 'artifact destination must be a regular file: ' + name)
    for name, content in artifacts.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, output / name)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return {'decision_digest': result['decision_digest'], 'counts': result['counts'], 'report': str(output / 'report.json')}


def verify_sync(database_path, fixture, base_url, company, year, series, output, operation_ids=None):
    config = fixture['config']
    require(config['mode'] == 'fixture' and config['binding_verified'] is True, 'fixture binding must be verified')
    scope = parse_scope({'company_id': company, 'financial_year': year, 'series': series,
                         'ramp_business_id': config['ramp_business_id'], 'ramp_entity_id': config['ramp_entity_id']})
    output_path = Path(output).resolve()
    database_directory = Path(database_path).resolve().parent
    require(output_path != database_directory and database_directory not in output_path.parents,
            'verification artifacts must be outside the ledger workspace')
    source = snapshot(database_path, scope, operation_ids or [])
    population = parse_population(source)
    observed = collect(population, base_url)
    observations = parse_observations(observed)
    return write_report(compare(population, observations), source, observed, output)


def replay_verification(source_path, observed_path, output):
    source, observed = read_capture(source_path), read_capture(observed_path)
    return write_report(compare(parse_population(source), parse_observations(observed)), source, observed, output)
