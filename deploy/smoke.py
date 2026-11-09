"""CI-only synthetic HTTP smoke, executed inside the fresh API container."""
import json
import secrets
import urllib.request
from sqlalchemy import text
from app.infra.db import get_session_factory, set_tenant_context
from app.security.organization_openai import resolve_key

base = 'http://127.0.0.1:8000'
token = None
def request(path, method='GET', payload=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(base + path, method=method, headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(req, timeout=15) as response:
        body = json.load(response)
    return body.get('data', body)

assert request('/health/ready')['status'] == 'ready'
with get_session_factory()() as session:
    role = session.execute(text('SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user')).one()
    assert role == (False, False)
    assert session.execute(text('SELECT count(*) FROM lk_users')).scalar() == 0

registered = request('/api/v1/auth/register', 'POST', {'email': f'ci-{secrets.token_hex(6)}@example.invalid',
    'password': secrets.token_urlsafe(32), 'fullName': 'Synthetic owner', 'companyName': 'CI test company'})
token = registered['accessToken']
assert registered['user']['permissionProfile'] == 'admin'
assert request('/api/v1/cabinet/me')['organization']['organizationId'] == registered['organization']['organizationId']
assert request('/api/v1/cabinet/openai-key')['storageAvailable']
synthetic_key = 'sk-' + 'synthetic_ci_not_a_provider_key_' * 2
saved = request('/api/v1/cabinet/openai-key', 'PUT', {'apiKey': synthetic_key})
assert saved['configured'] and synthetic_key not in str(saved)
assert request('/api/v1/cabinet/openai-key')['configured']
first_token = token
first_org = registered['organization']['organizationId']
assert resolve_key(first_org) == synthetic_key
second = request('/api/v1/auth/register', 'POST', {'email': f'ci-{secrets.token_hex(6)}@example.invalid',
    'password': secrets.token_urlsafe(32), 'fullName': 'Other synthetic owner', 'companyName': 'Other CI test company'})
token = second['accessToken']
second_org = second['organization']['organizationId']
assert not request('/api/v1/cabinet/openai-key')['configured']
assert resolve_key(second_org) is None
with get_session_factory()() as session:
    set_tenant_context(session, second_org)
    # Raw SQL deliberately omits an organization filter: FORCE RLS must hide
    # the first company's key even without the application's query safeguards.
    assert session.execute(text('SELECT count(*) FROM organization_openai_keys')).scalar() == 0
token = first_token
assert not request('/api/v1/cabinet/openai-key', 'DELETE')['configured']
assert request('/api/v1/wb/browser-prices/accounts') == []
print('Fresh production containers: readiness, restricted role, registration, profile and encrypted OpenAI configuration PASS.')
