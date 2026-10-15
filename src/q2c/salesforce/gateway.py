"""Sales Cloud data access used by the MCP tools.

``SalesforceGateway`` talks to the real REST API; ``InMemorySalesforce`` mirrors the same
contract against seeded data so the server, agent and tests run without an org.
"""

from __future__ import annotations

import datetime as dt
import itertools
import time
from typing import Any, Protocol

import httpx

from q2c.salesforce import soql
from q2c.salesforce.auth import ClientCredentialsAuth

OPPORTUNITY_FIELDS = "Id, Name, StageName, Amount, CloseDate, Type, AccountId, Account.Name"
QUOTE_FIELDS = "Id, Name, OpportunityId, Status, ExpirationDate, GrandTotal"


class SalesforceApiError(RuntimeError):
    def __init__(self, status: int, errors: list[dict[str, Any]] | Any):
        self.status = status
        self.errors = errors
        super().__init__(f"Salesforce API error {status}: {errors}")


class SalesGateway(Protocol):
    def search_opportunities(
        self, *, name_contains: str | None, stage: str | None, closing_before: dt.date | None, limit: int
    ) -> list[dict[str, Any]]: ...
    def get_opportunity(self, opportunity_id: str) -> dict[str, Any] | None: ...
    def list_quotes(self, opportunity_id: str) -> list[dict[str, Any]]: ...
    def create_quote(self, opportunity_id: str, name: str, expiration_date: dt.date) -> dict[str, Any]: ...
    def upcoming_renewals(self, *, within_days: int, today: dt.date | None = None) -> list[dict[str, Any]]: ...


def _flatten(record: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in record.items() if k != "attributes" and not isinstance(v, dict)}
    for key, value in record.items():
        if isinstance(value, dict) and key != "attributes":
            for sub_key, sub_value in value.items():
                if sub_key != "attributes":
                    out[f"{key}{sub_key}"] = sub_value
    return out


class SalesforceGateway:
    def __init__(
        self,
        auth: ClientCredentialsAuth,
        *,
        api_version: str = "v62.0",
        renewal_type: str = "Renewal",
        http: httpx.Client | None = None,
        max_retries: int = 3,
        sleep=time.sleep,
    ) -> None:
        self._auth = auth
        self._api_version = api_version
        self._renewal_type = renewal_type
        self._http = http or httpx.Client(timeout=20.0)
        self._max_retries = max_retries
        self._sleep = sleep

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            token = self._auth.token()
            url = path if path.startswith("http") else f"{token.instance_url}{path}"
            response = self._http.request(
                method, url, headers={"Authorization": f"Bearer {token.value}", "Accept": "application/json"}, **kwargs
            )
            if response.status_code == 401 and not refreshed:
                self._auth.invalidate()
                refreshed = True
                continue
            if response.status_code in (429, 502, 503, 504) and attempt < self._max_retries:
                self._sleep(min(2**attempt * 0.5, 8.0))
                attempt += 1
                continue
            if response.status_code >= 400:
                raise SalesforceApiError(response.status_code, response.json() if response.content else None)
            return response

    def query(self, statement: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/services/data/{self._api_version}/query", params={"q": statement})
        body = response.json()
        records = list(body["records"])
        while not body.get("done", True):
            body = self._request("GET", body["nextRecordsUrl"]).json()
            records.extend(body["records"])
        return [_flatten(r) for r in records]

    def search_opportunities(self, *, name_contains=None, stage=None, closing_before=None, limit=20):
        clauses = []
        if name_contains:
            clauses.append(f"Name LIKE {soql.contains(name_contains)}")
        if stage:
            clauses.append(f"StageName = {soql.literal(stage)}")
        if closing_before:
            clauses.append(f"CloseDate <= {soql.date(closing_before)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(f"SELECT {OPPORTUNITY_FIELDS} FROM Opportunity{where} ORDER BY CloseDate LIMIT {int(limit)}")

    def get_opportunity(self, opportunity_id):
        rows = self.query(
            f"SELECT {OPPORTUNITY_FIELDS} FROM Opportunity WHERE Id = {soql.record_id(opportunity_id)} LIMIT 1"
        )
        return rows[0] if rows else None

    def list_quotes(self, opportunity_id):
        return self.query(
            f"SELECT {QUOTE_FIELDS} FROM Quote WHERE OpportunityId = {soql.record_id(opportunity_id)} "
            "ORDER BY CreatedDate DESC"
        )

    def create_quote(self, opportunity_id, name, expiration_date):
        soql.record_id(opportunity_id)
        payload = {
            "Name": name,
            "OpportunityId": opportunity_id,
            "ExpirationDate": soql.date(expiration_date),
            "Status": "Draft",
        }
        response = self._request("POST", f"/services/data/{self._api_version}/sobjects/Quote/", json=payload)
        return {"Id": response.json()["id"], **payload}

    def upcoming_renewals(self, *, within_days, today=None):
        today = today or dt.date.today()
        horizon = today + dt.timedelta(days=within_days)
        return self.query(
            f"SELECT {OPPORTUNITY_FIELDS} FROM Opportunity WHERE Type = {soql.literal(self._renewal_type)} "
            f"AND IsClosed = false AND CloseDate >= {soql.date(today)} AND CloseDate <= {soql.date(horizon)} "
            "ORDER BY CloseDate"
        )


class InMemorySalesforce:
    """Seeded stand-in for a Sales Cloud org (demo mode and tests)."""

    def __init__(self, today: dt.date | None = None) -> None:
        today = today or dt.date.today()
        self._ids = itertools.count(1)
        self.opportunities: dict[str, dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        seed = [
            ("Acme Corp", "Acme - FlashArray Renewal FY27", "Negotiation/Review", 120000.0, 20, "Renewal"),
            ("Globex", "Globex - Annual Support Renewal", "Proposal/Price Quote", 38000.0, 45, "Renewal"),
            ("Initech", "Initech - New Storage Expansion", "Prospecting", 80000.0, 30, "New Customer"),
            ("Umbrella", "Umbrella - Cloud Block Store Renewal", "Qualification", 64000.0, 120, "Renewal"),
        ]
        for i, (account, name, stage, amount, days, kind) in enumerate(seed, start=1):
            opp_id = f"006{'0' * 11}{i}"
            self.opportunities[opp_id] = {
                "Id": opp_id,
                "Name": name,
                "StageName": stage,
                "Amount": amount,
                "CloseDate": (today + dt.timedelta(days=days)).isoformat(),
                "Type": kind,
                "AccountId": f"001{'0' * 11}{i}",
                "AccountName": account,
                "IsClosed": False,
            }

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in record.items() if k != "IsClosed"}

    def search_opportunities(self, *, name_contains=None, stage=None, closing_before=None, limit=20):
        rows = [
            o
            for o in self.opportunities.values()
            if (not name_contains or name_contains.lower() in o["Name"].lower())
            and (not stage or o["StageName"] == stage)
            and (not closing_before or o["CloseDate"] <= soql.date(closing_before))
        ]
        return [self._public(o) for o in sorted(rows, key=lambda o: o["CloseDate"])[:limit]]

    def get_opportunity(self, opportunity_id):
        soql.record_id(opportunity_id)
        record = self.opportunities.get(opportunity_id)
        return self._public(record) if record else None

    def list_quotes(self, opportunity_id):
        soql.record_id(opportunity_id)
        return [q for q in self.quotes.values() if q["OpportunityId"] == opportunity_id]

    def create_quote(self, opportunity_id, name, expiration_date):
        if self.get_opportunity(opportunity_id) is None:
            raise SalesforceApiError(404, [{"errorCode": "NOT_FOUND", "message": "Opportunity not found"}])
        quote_id = f"0Q0{next(self._ids):012d}"
        quote = {
            "Id": quote_id,
            "Name": name,
            "OpportunityId": opportunity_id,
            "Status": "Draft",
            "ExpirationDate": soql.date(expiration_date),
            "GrandTotal": None,
        }
        self.quotes[quote_id] = quote
        return quote

    def upcoming_renewals(self, *, within_days, today=None):
        today = today or dt.date.today()
        horizon = (today + dt.timedelta(days=within_days)).isoformat()
        rows = [
            o
            for o in self.opportunities.values()
            if o["Type"] == "Renewal" and not o["IsClosed"] and today.isoformat() <= o["CloseDate"] <= horizon
        ]
        return [self._public(o) for o in sorted(rows, key=lambda o: o["CloseDate"])]
