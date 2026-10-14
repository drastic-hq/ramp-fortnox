import base64
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json


class Refusal(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_json(content):
    return json.loads(content, parse_float=Decimal)


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def ore(value, field):
    require(type(value) is int, field + ' must be integer ore')
    return value


def decimal(value, field):
    require(not isinstance(value, bool) and value is not None, field + ' is missing')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Refusal(field + ' is not a decimal')
    require(number.is_finite(), field + ' must be finite')
    return number


def money(value):
    return format(Decimal(value) / Decimal(100), '.2f')


@dataclass(frozen=True)
class FrozenPlan:
    operation_id: str
    fingerprint: str
    payload: dict
    source: dict
    receipt_base64: str

    def as_dict(self):
        return {'operation_id': self.operation_id, 'fingerprint': self.fingerprint,
                'payload': self.payload, 'source': self.source,
                'receipt_base64': self.receipt_base64}

    @classmethod
    def restore(cls, value):
        return cls(**value)


def operation_key(config, transaction_id):
    return 'ramp:{}:{}:{}:card_purchase'.format(
        config['ramp_business_id'], config['ramp_entity_id'], transaction_id)


def source_snapshot(fixture, transaction, receipt, original):
    receipt = dict(receipt)
    receipt.pop('receipt_url', None)
    return {'config': fixture['config'], 'evidence': fixture['evidence'],
            'transaction': transaction, 'receipt': receipt,
            'original_sha256': hashlib.sha256(original).hexdigest()}


def supported_p0_row_semantics(row):
    return (isinstance(row, dict) and row.get('Removed') in (None, False, '', 'false') and
            all(row.get(field) in (None, False, '') for field in ['CostCenter', 'Project']))


def plan_purchase(source, original):
    config, evidence = source['config'], source['evidence']
    transaction, receipt = source['transaction'], source['receipt']
    require(config.get('mode') == 'fixture', 'live writes blocked pending sandbox gates')
    require(config.get('binding_verified') is True, 'entity binding is unverified')
    require(transaction.get('entity_id') == config.get('ramp_entity_id'), 'wrong Ramp entity')
    require(transaction.get('all_requirements_met_and_approved') is True,
            'transaction must be approved with all requirements met')
    require(transaction.get('state') == 'CLEARED', 'transaction must be cleared')
    require(transaction.get('sync_status') == 'SYNC_READY', 'transaction must be SYNC_READY')
    require(not transaction.get('line_items'), 'transaction line items are unsupported in P0')
    require(transaction.get('requires_accounting_vendor_creation_to_sync') is not True,
            'accounting vendor creation is unsupported in P0')
    require(transaction.get('updated_at'), 'transaction updated_at is missing')
    amounts = []
    for field in ['entity_amount', 'merchant_amount']:
        amount = transaction.get(field) or {}
        require(amount.get('currency') == 'SEK', field + ' currency must be SEK')
        amounts.append(ore(amount.get('value'), field))
    gross, merchant = amounts
    require(gross > 0 and merchant > 0, 'only positive purchases are supported')
    require(gross == merchant, 'merchant and settlement amounts must agree')
    require(evidence.get('merchant_country') == 'SE' and
            (transaction.get('merchant_location') or {}).get('country') == 'SE',
            'merchant must be domestic')
    selections = transaction.get('accounting_field_selections') or []
    accounts = [selection for selection in selections if selection.get('type') == 'GL_ACCOUNT']
    tax_codes = [selection for selection in selections if selection.get('type') == 'TAX_CODE']
    require(len(accounts) == 1 and len(tax_codes) == 1 and
            all(selection.get('provider_name') == config.get('provider_name')
                for selection in accounts + tax_codes), 'fully coded accounting selections are required')
    require(accounts[0].get('external_id') == str(config.get('expense_account')),
            'selected expense account mapping differs')
    coded_rate = (config.get('tax_code_mapping') or {}).get(tax_codes[0].get('external_id'))
    require(coded_rate is not None, 'selected tax code mapping is missing')
    require(transaction.get('receipts') == [receipt.get('id')], 'one linked receipt is required')
    require(receipt.get('transaction_id') == transaction.get('id'), 'receipt transaction link differs')
    original_evidence = evidence.get('receipt') or {}
    require(original_evidence.get('original_match_verified') is True,
            'original receipt match must be verified')
    require(original_evidence.get('transaction_id') == transaction.get('id') and
            original_evidence.get('receipt_id') == receipt.get('id'), 'receipt evidence link differs')
    require(original_evidence.get('sha256') == source['original_sha256'], 'receipt hash differs')
    try:
        declared_original = base64.b64decode(original_evidence.get('original_base64', ''), validate=True)
    except ValueError:
        raise Refusal('receipt original bytes are invalid')
    require(declared_original == original, 'receipt original bytes differ')
    require(original_evidence.get('currency') == 'SEK', 'receipt currency must be SEK')
    require(original_evidence.get('merchant') == transaction.get('merchant_name'),
            'receipt merchant differs')
    receipt_date = transaction.get('accounting_date', '')[:10]
    try:
        date.fromisoformat(receipt_date)
    except ValueError:
        raise Refusal('invalid accounting date')
    require(original_evidence.get('date') == receipt_date, 'receipt date differs')
    ocr = receipt.get('ocr') or {}
    require(ocr.get('currency_code') == 'SEK', 'OCR receipt currency must be SEK')
    taxes = ocr.get('taxes') or []
    require(len(taxes) == 1, 'single explicit receipt tax is required')
    tax = taxes[0]
    require(type(tax.get('tax_amount')) in [int, Decimal] and
            type(tax.get('tax_rate')) in [int, Decimal], 'explicit numeric tax fields are required')
    calibration = evidence.get('units_calibration') or {}
    require(calibration.get('verified') is True and
            calibration.get('field_path') == 'receipt.ocr.taxes[].tax_amount' and
            calibration.get('unit') in ['SEK_MAJOR', 'SEK_MINOR'] and
            calibration.get('rate_scale') == 'percent' and
            calibration.get('rounding') == 'HALF_UP_ORE' and
            bool(calibration.get('source_reference')) and
            bool(calibration.get('contract_revision')), 'tax units calibration is required')
    require(decimal(tax.get('tax_rate'), 'tax rate') == Decimal(25) and
            decimal(coded_rate, 'coded tax rate') == Decimal(25),
            'only explicitly coded 25 percent VAT is supported')
    vat_decimal = decimal(tax.get('tax_amount'), 'tax amount')
    if calibration['unit'] == 'SEK_MAJOR':
        vat_decimal *= Decimal(100)
    require(vat_decimal == vat_decimal.to_integral_value(), 'tax has fractions of an ore')
    vat = int(vat_decimal)
    expected_vat = int((Decimal(gross) / Decimal(5)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    require(vat > 0 and vat == expected_vat, 'explicit tax disagrees with 25 percent gross')
    net = gross - vat
    require(original_evidence.get('gross_ore') == gross and
            original_evidence.get('vat_ore') == vat and
            original_evidence.get('net_ore') == net, 'original receipt totals or tax disagree')
    require(type(config.get('card_liability_account')) is int and
            config['card_liability_account'] > 0, 'explicit card liability account is required')
    require(config.get('vat_account') == 2641, 'P0 VAT account must be 2641')
    require(type(config.get('expense_account')) is int and config['expense_account'] > 0 and
            evidence.get('coded_expense_account') == config['expense_account'],
            'coded expense account mapping differs')
    require(len({config['expense_account'], config['vat_account'],
                 config['card_liability_account']}) == 3, 'posting accounts must differ')
    require(bool(config.get('series')) and type(config.get('financial_year')) is int and
            bool(config.get('mapping_revision')) and bool(config.get('fortnox_company_id')),
            'series, year, company and mapping revision are required')
    key = operation_key(config, transaction['id'])
    rows = [
        {'Account': config['expense_account'], 'Debit': money(net), 'Credit': '0.00'},
        {'Account': config['vat_account'], 'Debit': money(vat), 'Credit': '0.00'},
        {'Account': config['card_liability_account'], 'Debit': '0.00', 'Credit': money(gross)},
    ]
    payload = {'Voucher': {'VoucherSeries': config['series'], 'TransactionDate': receipt_date,
                            'Description': key, 'VoucherRows': rows}}
    return FrozenPlan(key, digest({'source': source, 'posting': payload}), payload, source,
                      base64.b64encode(original).decode())
