"""OAuth 2.0 Client Credentials flow for a Salesforce Connected App / External Client App."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx


class SalesforceAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccessToken:
    value: str
    instance_url: str
    expires_at: float

    def is_fresh(self, now: float, skew_seconds: float = 60.0) -> bool:
        return now < self.expires_at - skew_seconds


class ClientCredentialsAuth:
    """Fetches and caches an access token; thread-safe so concurrent tool calls share one token.

    Salesforce does not return ``expires_in`` for this flow, so the token lifetime is a
    configurable local TTL and the client also refreshes reactively on HTTP 401.
    """

    def __init__(
        self,
        login_url: str,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.Client | None = None,
        token_ttl_seconds: float = 1800.0,
        clock=time.monotonic,
    ) -> None:
        if not login_url.startswith("https://"):
            raise ValueError("login_url must use https")
        self._token_url = login_url.rstrip("/") + "/services/oauth2/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=15.0)
        self._ttl = token_ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._token: AccessToken | None = None

    def token(self) -> AccessToken:
        with self._lock:
            if self._token is None or not self._token.is_fresh(self._clock()):
                self._token = self._fetch()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _fetch(self) -> AccessToken:
        response = self._http.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            # Never echo the request body: it contains the client secret.
            detail = response.json().get("error_description", "") if response.content else ""
            raise SalesforceAuthError(f"token request failed ({response.status_code}): {detail}")
        body = response.json()
        return AccessToken(
            value=body["access_token"],
            instance_url=body["instance_url"].rstrip("/"),
            expires_at=self._clock() + self._ttl,
        )
