# T4 → T1: strict account-verified Avito OAuth adapter

Status: **IMPLEMENTED / UNVERIFIED**. Source-only delegated package; no tests,
imports, compile, linter, PG, independent review or provider/network calls run.
Do not treat this commit as acceptance, activation or release readiness.

## Exact ownership and imports

Only these three paths belong to this package:

- `app/avito/auth.py`: additive `AvitoOAuthClient.fetch_verified_account_token`.
  Existing class constructor, token model, legacy exchange and resolver/cache
  function bodies remain unchanged; no existing caller is switched.
- `app/avito/credential_exchange.py`: new strict exchange, result/error contract.
- This handoff. No DB/store/cache/router/task/config/grant/schema/frontend edits.

New imports for the writer:

```python
from app.avito.auth import AvitoOAuthClient
from app.avito.credential_exchange import (
    AvitoAccountExchangeError,
    VerifiedAvitoAccountToken,
    fetch_verified_account_token,
)
```

Public method signature:

```python
AvitoOAuthClient.fetch_verified_account_token(
    self, client_id: str, client_secret: str, *,
    expected_external_account_id: str,
    max_response_bytes: int,
    clock,          # trusted Callable[[], aware UTC datetime]
    client=None,    # trusted httpx.Client-compatible stream dependency
)
```

The module function returns `VerifiedAvitoAccountToken` and accepts the same
arguments plus REQUIRED keyword `base_url: str` and `timeout_seconds: float`.
The wrapper forwards existing instance configuration. T1 bootstrap must choose
an explicit timeout when constructing the wrapper; the old constructor default
is retained for legacy compatibility, not approved as a new production policy.
The module function has no default timeout/clock/response budget/base URL.

## Transport and minimal source shape

Only `https://api.avito.ru` is accepted as base. Fixed POST `/token` with
`grant_type=client_credentials`, then fixed GET `/core/v1/accounts/self` using
the returned bearer token. No URL/account route is derived from the response.
This follows the observed endpoints in `auth.py`, `chats.py` and `stats.py`, but
deliberately does not copy the latter two's `user_id/userId/name/profile` aliases.
An unsupported provider envelope fails closed instead of guessing identity.

An owned client is created only on invocation with redirects disabled,
trust_env=False and explicit timeout. Both requests additionally disable redirects
and implicit client auth. Injected clients are trusted transport dependencies:
mock `stream`/`iter_raw` or use an httpx mock transport at the SAME canonical URLs;
do not inject a client with cookies, logging hooks, retry transport or arbitrary
real routing. The adapter does not close caller-owned clients. T1 owns production
transport bootstrap and response-byte-budget selection; neither is invented here.

Both responses require exactly200 and application/json. Error bodies are not
read. Requests ask for identity encoding; any non-identity content encoding is
rejected before reading. Raw streamed chunks are accumulated only up to the
explicit per-response `max_response_bytes`; declared Content-Length is checked
before allocation and against received bytes. JSON decoding is UTF-8 strict,
rejects duplicate keys, NaN/Infinity and non-object roots. Extra provider fields
are ignored, never returned or treated as evidence.

Token requirements:

- `access_token`: native nonblank string, valid UTF-8 within the existing
  `MAX_SECRET_FIELD_BYTES` bound. To put it in a single bearer header without
  normalization, only printable ASCII without whitespace is supported; other
  forms produce typed unsupported. No stripping/coercion or raw-token repr.
- `token_type`: explicit exact `Bearer`, no missing/lowercase default.
- `expires_in`: native positive integer, excluding bool/string/fraction; no
  fallback86400, minimum60, padding or guessed safety TTL. Overflow is rejected.
- `/accounts/self.id`: positive native integer excluding bool, or canonical
  positive ASCII decimal string. Leading zeros/sign/space/fraction/aliases are
  unsupported. Integer conversion is exact, never float. Compare to the supplied
  canonical external ID byte-for-byte; mismatch has its own safe error.

Capture trusted aware UTC time BEFORE exchange. Expiry is that time floored to
seconds plus provider expires_in. After successful identity verification, read
the clock again: backwards clock is invalid configuration and expires_at at or
before verified_at is expired. Network latency cannot extend token life.

## Redacted result and safe errors

`VerifiedAvitoAccountToken` is frozen/slotted and has these fields:

- `credential: DecryptedCredential`, containing exactly `accessToken` and
  canonical UTC second-resolution `expiresAt` using the existing crypto format;
- `external_account_id: str`;
- `exchange_started_at`, `verified_at`, `expires_at`: aware UTC datetimes.

Result and credential repr/str are redacted. Result copy/deepcopy/pickle are
refused; the existing credential wrapper also refuses iteration/copy/pickle.
No generic JSON mapping/token field is provided. Only explicit `credential.reveal()`
returns plaintext for the T1 single encrypted-store boundary. Never place it in
logs, generic JSON, task messages, test failure output, caches or browser storage.
The wrapper is not a memory-zeroization or debugger/traceback-local guarantee.

`AvitoAccountExchangeError.code` is limited to:

```text
avito_oauth_configuration_invalid
avito_oauth_input_invalid
avito_oauth_transport_unavailable
avito_oauth_provider_rejected
avito_oauth_response_unsupported
avito_oauth_response_too_large
avito_oauth_account_mismatch
avito_oauth_token_expired
```

At the public boundary raw exceptions are discarded, not stringified/logged;
the safe error is raised outside the except block, without a raw cause/context
chain. No provider response/header/validation details are attached.

## T1 integration prerequisites and deferred acceptance

This is provider evidence, NOT authority to publish or proof that an actor still
has access. T1 must supply expected_external_account_id from paired account
binding, revalidate actual user/session/account/client credential generation
after exchange and persist through the sole encrypted store. No binding may be
inferred from an HTTP body or generic actor enum. No implicit refresh, client
credential fallback, generation assignment, backfill or writer activation exists.

Deferred verification should cover exact token/self shape, both identity types,
bool/fraction/aliases/leading zeros, duplicate JSON/invalid UTF8, compressed and
oversized streams including dishonest/missing length, redirect/error bodies,
expiry overflow/latency/backwards clocks, redaction/copy/pickle/error-chain behavior,
client ownership and legacy behavior parity. Fake transports only. No such gate
has run in this source-only commit. The separate Review history/frontend working
changes in this worktree are not part of the three-path commit.
