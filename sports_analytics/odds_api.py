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

    @property
    def total(self):
        return self.used + self.remaining


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
        self._saved = None
        self._account = None
        self._saved_at = 0

    def _request(self, path, key, params=None):
        try:
            response = self.session.get("https://api.the-odds-api.com/v4/" + path,
                                        params={**(params or {}), "apiKey": key}, timeout=(5, 20), allow_redirects=False)
        except requests.RequestException:
            raise DataSourceError("The Odds API could not be reached. Check connectivity and refresh usage.") from None
        if response.status_code != 200:
            messages = {
                401: "The Odds API rejected the API key. Check ODDS_API_KEY in .env and restart the app.",
                403: "The Odds API denied access. Check your API key and subscription access.",
                429: "The Odds API rate limit was reached. Wait briefly before refreshing usage.",
                422: "The Odds API rejected the market request. No additional markets or retries were requested.",
            }
            error = DataSourceError(messages.get(response.status_code,
                                   f"The Odds API returned HTTP {response.status_code}. Refresh usage to retry."))
            headers = CaseInsensitiveDict(response.headers)
            error.usage_headers = {key: headers.get(key) for key in
                                   ("x-requests-last", "x-requests-used", "x-requests-remaining")}
            raise error
        return response

    def _fetch_usage(self, key):
        response = self._request("sports/", key)
        return CreditUsage(*credit_headers(response.headers), datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @staticmethod
    def _key():
        key = os.environ.get("ODDS_API_KEY", "").strip()
        if not key:
            raise DataSourceError("Set ODDS_API_KEY in your project's .env file, restart the app, then open Odds API.")
        return key

    def account_id(self):
        return hashlib.sha256(self._key().encode()).hexdigest()

    def _select_account(self, key):
        account = hashlib.sha256(key.encode()).digest()
        if self._account != account:
            self._saved = None
            self._account = account

    def get(self, path, params=None):
        """One authenticated request, without automatic retries or redirects."""
        if not path.startswith("sports/") or ".." in path or "?" in path:
            raise ValueError("Use a relative sports endpoint.")
        key = self._key()
        with self._lock:
            self._select_account(key)
            response = self._request(path, key, params)
            try:
                self._saved = CreditUsage(*credit_headers(response.headers), datetime.now(timezone.utc).isoformat(timespec="seconds"))
                self._saved_at = time.monotonic()
            except DataSourceError:
                # A successful paid response is still useful without quota headers.
                self._saved = None
            return response

    def usage(self, refresh=False):
        key = self._key()
        with self._lock:
            self._select_account(key)
            if self._saved and not refresh and time.monotonic() - self._saved_at < self.ttl_seconds:
                return self._saved
            try:
                usage = self._fetch_usage(key)
            except DataSourceError as exc:
                if self._saved:
                    return replace(self._saved, stale=True, notice=str(exc))
                raise
            self._saved = usage
            self._saved_at = time.monotonic()
            return usage
