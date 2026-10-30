"""Server-side connection to The Odds API and its account quota headers.

The sports-list endpoint costs no usage credits. Never send keys, response
bodies, request URLs, or raw HTTP exceptions to dashboard components.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import os
import threading
import time

import requests
from requests.structures import CaseInsensitiveDict

from .data import DataSourceError

DOCUMENTATION = "https://the-odds-api.com/liveapi/guides/v4/#response-headers"


@dataclass(frozen=True)
class CreditUsage:
    used: int
    remaining: int
    checked_at: str
    stale: bool = False
    notice: str = ""
    accounts: tuple = ()

    @property
    def total(self):
        return self.used + self.remaining


@dataclass(frozen=True)
class AccountUsage:
    label: str
    account_id: str
    usage: CreditUsage | None
    active: bool = False
    exhausted: bool = False
    notice: str = ''


def credit_headers(headers):
    headers = CaseInsensitiveDict(headers)
    counts = []
    for name in ("x-requests-used", "x-requests-remaining"):
        try:
            value = Decimal(str(headers[name]))
            if not value.is_finite() or value < 0 or value != value.to_integral_value():
                raise ValueError("Invalid credit count")
            counts.append(int(value))
        except (KeyError, InvalidOperation, ValueError, TypeError, OverflowError):
            raise DataSourceError("The Odds API did not return valid credit usage headers. Refresh usage to retry.") from None
    return counts


class OddsApiService:
    def __init__(self, session=None, ttl_seconds=300):
        self.session = session or requests.Session()
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._saved = {}
        self._saved_at = {}
        self._exhausted = {}

    def _request(self, path, key, params=None):
        account = self._fingerprint(key)
        try:
            response = self.session.get("https://api.the-odds-api.com/v4/" + path,
                                        params={**(params or {}), "apiKey": key}, timeout=(5, 20), allow_redirects=False)
        except requests.RequestException:
            error = DataSourceError("The Odds API could not be reached. Check connectivity and refresh usage.")
            error.odds_account_id = account
            raise error from None
        if response.status_code != 200:
            messages = {
                401: "The Odds API rejected the API key. Check its entry in .env and restart the app.",
                403: "The Odds API denied access. Check your API key and subscription access.",
                429: "The Odds API rate limit was reached. Wait briefly before refreshing usage.",
                422: "The Odds API rejected the market request. No additional markets or retries were requested.",
            }
            # Read only the documented error code; never surface raw bodies or
            # URLs, which can contain credentials. 401 alone is not exhaustion.
            try:
                payload = response.json()
                exhausted = isinstance(payload, dict) and payload.get('error_code') == 'OUT_OF_USAGE_CREDITS'
            except (ValueError, TypeError):
                exhausted = False
            error = DataSourceError("The Odds API credit allowance is exhausted." if exhausted else messages.get(response.status_code,
                                   f"The Odds API returned HTTP {response.status_code}. Refresh usage to retry."))
            headers = CaseInsensitiveDict(response.headers)
            error.usage_headers = {key: headers.get(key) for key in
                                   ("x-requests-last", "x-requests-used", "x-requests-remaining")}
            error.odds_account_id = account
            error.quota_exhausted = exhausted
            raise error
        response.odds_account_id = account
        return response

    @staticmethod
    def _key():
        return OddsApiService._keys()[0][1]

    @staticmethod
    def _keys():
        configured = []; seen = set()
        for label, name in [('Primary', 'ODDS_API_KEY'), ('Backup', 'ODDS_API_KEY_2')]:
            key = os.environ.get(name, '').strip()
            if key and key not in seen:
                configured.append((f'{label} ({name})', key))
                seen.add(key)
        if not configured:
            raise DataSourceError("Set ODDS_API_KEY (and optionally ODDS_API_KEY_2) in your project's .env file, restart the app, then open Odds API.")
        return configured

    @staticmethod
    def _fingerprint(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def account_id(self):
        # A stable click namespace preserves duplicate-pull protection when the
        # paying key changes. Actual charges carry the response's account ID.
        return self._fingerprint(self._key())

    def account_ids(self):
        return list(self.account_labels())

    def account_labels(self):
        return {self._fingerprint(key): label for label, key in self._keys()}

    def _sync_accounts(self, keys):
        allowed = {self._fingerprint(key) for _, key in keys}
        for values in (self._saved, self._saved_at, self._exhausted):
            for account in set(values)-allowed:values.pop(account)

    def _remember(self, key, headers):
        account = self._fingerprint(key)
        try:
            usage = CreditUsage(*credit_headers(headers), datetime.now(timezone.utc).isoformat(timespec='seconds'))
        except DataSourceError:
            return None
        self._saved[account] = usage
        self._saved_at[account] = time.monotonic()
        if usage.remaining == 0:self._exhausted[account] = time.monotonic()
        else:self._exhausted.pop(account, None)
        return usage

    def _usage_for(self, key, refresh=False):
        account = self._fingerprint(key)
        saved = self._saved.get(account)
        if saved and not refresh and time.monotonic()-self._saved_at[account] < self.ttl_seconds:
            return saved
        try:
            response = self._request('sports/', key)
            usage = self._remember(key, response.headers)
            if usage is None:raise DataSourceError('The Odds API did not return valid credit usage headers. Refresh usage to retry.')
            return usage
        except DataSourceError as exc:
            if getattr(exc, 'quota_exhausted', False):
                reported = self._remember(key, getattr(exc, 'usage_headers', {}))
                self._exhausted[account] = time.monotonic()
                if reported:return replace(reported, notice=str(exc))
            if saved:
                stale = replace(saved, stale=True, notice=str(exc))
                self._saved[account] = stale
                return stale
            raise

    def _available(self, key):
        account = self._fingerprint(key)
        exhausted_at = self._exhausted.get(account)
        if exhausted_at is None:return True
        if time.monotonic()-exhausted_at < self.ttl_seconds:return False
        # The free endpoint can discover a replenished allowance without risking
        # a second paid request. Failure leaves the exhausted key unavailable.
        try:
            usage = self._usage_for(key, refresh=True)
            return not usage.stale and usage.remaining > 0 and account not in self._exhausted
        except DataSourceError:
            self._exhausted[account] = time.monotonic()
            return False

    def get(self, path, params=None):
        """Try the next configured key only after confirmed quota rejection.

        Network errors, malformed responses, rate limits and authorization
        failures are never automatically repeated against another allowance.
        """
        if not path.startswith("sports/") or ".." in path or "?" in path:
            raise ValueError("Use a relative sports endpoint.")
        keys = self._keys()
        with self._lock:
            self._sync_accounts(keys)
            attempts = []
            for label, key in keys:
                if not self._available(key):continue
                account = self._fingerprint(key)
                try:
                    response = self._request(path, key, params)
                except DataSourceError as exc:
                    exc.odds_attempts = list(attempts)
                    if not getattr(exc, 'quota_exhausted', False):raise
                    reported = self._remember(key, getattr(exc, 'usage_headers', {}))
                    if reported is None:
                        self._saved.pop(account, None)
                        self._saved_at.pop(account, None)
                    self._exhausted[account] = time.monotonic()
                    # An explicitly charged error is not safe to resubmit.
                    headers = CaseInsensitiveDict(getattr(exc, 'usage_headers', {}))
                    charge = headers.get('x-requests-last')
                    if charge is not None:
                        try:charged = Decimal(str(charge)) != 0
                        except InvalidOperation:charged = True
                        if charged:raise
                    attempts.append(dict(account=account, headers=dict(headers), status='Quota exhausted / not saved'))
                    continue
                if self._remember(key, response.headers) is None:
                    self._saved.pop(account, None)
                    self._saved_at.pop(account, None)
                response.odds_attempts = attempts
                return response
            error = DataSourceError('All configured Odds API keys have exhausted their credit allowance. Refresh usage after credits replenish, or update the keys in .env.')
            error.odds_attempts = attempts
            error.no_request_sent = not attempts
            if attempts:
                error.odds_account_id = attempts[-1]['account']
                error.usage_headers = attempts[-1]['headers']
            raise error

    def usage(self, refresh=False):
        keys = self._keys()
        with self._lock:
            self._sync_accounts(keys)
            accounts = []; active = False
            for label, key in keys:
                account = self._fingerprint(key)
                try:usage = self._usage_for(key, refresh)
                except DataSourceError as exc:
                    exhausted = account in self._exhausted
                    selected = not active and not exhausted
                    active = active or selected
                    accounts.append(AccountUsage(label, account, None, selected, exhausted, notice=str(exc)))
                    continue
                exhausted = account in self._exhausted
                selected = not active and not exhausted
                active = active or selected
                accounts.append(AccountUsage(label, account, usage, selected, exhausted))
            known = [a.usage for a in accounts if a.usage is not None]
            if not known:
                raise DataSourceError('No credit usage available. '+ ' '.join(a.label+': '+a.notice for a in accounts))
            notices = [a.label+': '+(a.notice or a.usage.notice) for a in accounts if a.notice or (a.usage and a.usage.notice)]
            if len(known)<len(accounts):notices.insert(0,'Combined totals include only keys with reported counters; unavailable balances are unknown.')
            return CreditUsage(sum(u.used for u in known), sum(u.remaining for u in known), min(u.checked_at for u in known),
                               any(u.stale for u in known) or len(known)<len(accounts), ' '.join(notices), tuple(accounts))
