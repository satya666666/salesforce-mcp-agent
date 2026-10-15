"""SalesforceGateway against a mocked Salesforce REST API (OAuth, pagination, retries)."""

import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from q2c.salesforce.auth import ClientCredentialsAuth, SalesforceAuthError
from q2c.salesforce.gateway import SalesforceApiError, SalesforceGateway

LOGIN = "https://example.my.salesforce.com"
INSTANCE = "https://example.my.salesforce.com"


class FakeOrg:
    def __init__(self):
        self.token_requests = 0
        self.valid_token = "token-1"
        self.queries: list[str] = []
        self.throttle_next = 0
        self.created: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            form = parse_qs(request.content.decode())
            if form["client_secret"] != ["secret"]:
                return httpx.Response(400, json={"error": "invalid_client", "error_description": "invalid client"})
            self.token_requests += 1
            self.valid_token = f"token-{self.token_requests}"
            return httpx.Response(200, json={"access_token": self.valid_token, "instance_url": INSTANCE})

        if request.headers["Authorization"] != f"Bearer {self.valid_token}":
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}])
        if self.throttle_next:
            self.throttle_next -= 1
            return httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE"}])

        if request.url.path.endswith("/query"):
            self.queries.append(parse_qs(urlparse(str(request.url)).query)["q"][0])
            return httpx.Response(
                200,
                json={
                    "done": False,
                    "nextRecordsUrl": "/services/data/v62.0/query/01g-2000",
                    "records": [_opp("006000000000001", "Acme")],
                },
            )
        if request.url.path.endswith("/query/01g-2000"):
            return httpx.Response(200, json={"done": True, "records": [_opp("006000000000002", "Globex")]})
        if request.method == "POST" and request.url.path.endswith("/sobjects/Quote/"):
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "0Q0000000000001", "success": True, "errors": []})
        return httpx.Response(404, json=[{"errorCode": "NOT_FOUND"}])


def _opp(opp_id, account):
    return {
        "attributes": {"type": "Opportunity"},
        "Id": opp_id,
        "Name": f"{account} renewal",
        "Account": {"attributes": {"type": "Account"}, "Name": account},
    }


@pytest.fixture
def org():
    return FakeOrg()


@pytest.fixture
def gateway(org):
    http = httpx.Client(transport=httpx.MockTransport(org.handler))
    auth = ClientCredentialsAuth(LOGIN, "client", "secret", http=http)
    return SalesforceGateway(auth, http=http, sleep=lambda _: None)


def test_query_follows_pagination_and_flattens_relationships(gateway, org):
    rows = gateway.search_opportunities(name_contains="Ren", stage=None, closing_before=None, limit=10)
    assert [r["AccountName"] for r in rows] == ["Acme", "Globex"]
    assert "attributes" not in rows[0]
    assert org.token_requests == 1


def test_user_input_is_escaped_inside_soql(gateway, org):
    gateway.search_opportunities(name_contains="x' OR Name != '", stage="Closed Won", closing_before=None, limit=5)
    assert "Name LIKE '%x\\' OR Name != \\'%'" in org.queries[0]
    assert "StageName = 'Closed Won'" in org.queries[0]


def test_expired_session_triggers_one_token_refresh(gateway, org):
    gateway.search_opportunities(name_contains=None, stage=None, closing_before=None, limit=1)
    org.valid_token = "rotated-by-admin"
    org.token_requests = 5
    gateway.search_opportunities(name_contains=None, stage=None, closing_before=None, limit=1)
    assert org.token_requests == 6


def test_transient_errors_are_retried(gateway, org):
    org.throttle_next = 2
    assert len(gateway.search_opportunities(name_contains=None, stage=None, closing_before=None, limit=1)) == 2


def test_persistent_errors_surface_as_api_error(gateway, org):
    org.throttle_next = 10
    with pytest.raises(SalesforceApiError) as err:
        gateway.search_opportunities(name_contains=None, stage=None, closing_before=None, limit=1)
    assert err.value.status == 503


def test_create_quote_posts_draft(gateway, org):
    quote = gateway.create_quote("006000000000001", "Renewal S-1", dt.date(2030, 1, 31))
    assert quote["Id"] == "0Q0000000000001"
    assert org.created == [
        {"Name": "Renewal S-1", "OpportunityId": "006000000000001", "ExpirationDate": "2030-01-31", "Status": "Draft"}
    ]


def test_bad_client_secret_fails_without_leaking_it():
    org = FakeOrg()
    http = httpx.Client(transport=httpx.MockTransport(org.handler))
    auth = ClientCredentialsAuth(LOGIN, "client", "wrong-secret", http=http)
    with pytest.raises(SalesforceAuthError) as err:
        auth.token()
    assert "wrong-secret" not in str(err.value)


def test_token_is_cached_until_ttl_expires(org):
    now = [0.0]
    http = httpx.Client(transport=httpx.MockTransport(org.handler))
    auth = ClientCredentialsAuth(LOGIN, "client", "secret", http=http, token_ttl_seconds=600, clock=lambda: now[0])
    auth.token()
    now[0] = 500
    auth.token()
    assert org.token_requests == 1
    now[0] = 560
    auth.token()
    assert org.token_requests == 2


def test_login_url_must_be_https():
    with pytest.raises(ValueError):
        ClientCredentialsAuth("http://login.salesforce.com", "c", "s")
