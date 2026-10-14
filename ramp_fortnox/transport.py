import ipaddress
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

from .domain import canonical, load_json, Refusal


class ExternalFailure(Exception):
    def __init__(self, message, definitive=False):
        super().__init__(message)
        self.definitive = definitive


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def loopback_url(url):
    parsed = urlsplit(url)
    try:
        address = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
    except ValueError:
        raise Refusal('fixture transport requires a literal loopback URL')
    if (parsed.scheme != 'http' or not address.is_loopback or not port or
            parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise Refusal('fixture transport requires HTTP loopback without userinfo')
    return url


class FixtureHTTP:
    def __init__(self, base_url, journal):
        self.base_url = loopback_url(base_url).rstrip('/') + '/'
        self.journal = journal
        self.operation_id = None
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, method, path, payload=None, binary=False):
        url = loopback_url(urljoin(self.base_url, path))
        call_id = self.journal.begin_call(self.operation_id, method, url, payload)
        content = canonical(payload).encode() if payload is not None else None
        request = Request(url, data=content, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=3) as response:
                body = response.read()
                result = body if binary else load_json(body)
                self.journal.finish_call(call_id, {'status': response.status,
                    'body': result.hex() if binary else result})
                return result
        except HTTPError as error:
            self.journal.finish_call(call_id, {'error': 'HTTP ' + str(error.code)})
            raise ExternalFailure('HTTP ' + str(error.code), definitive=error.code == 422)
        except (URLError, OSError, ValueError) as error:
            self.journal.finish_call(call_id, {'error': str(error)})
            raise ExternalFailure('unproven remote outcome: ' + str(error))
