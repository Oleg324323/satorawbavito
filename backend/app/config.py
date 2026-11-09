from __future__ import annotations

import os
import re
from dataclasses import dataclass

from app.security.marketplace_credentials import CredentialCryptoError, CredentialKeyring


def _parse_marketplace_role(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value) is None:
        raise RuntimeError("marketplace_executor_configuration_invalid") from None
    return value


def _parse_csv_env(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    items = tuple(item.strip().rstrip("/") for item in value.split(",") if item.strip())
    return items or default


def _parse_int_csv_env(value: str | None, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    if value is None:
        return default
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.append(int(item))
        except ValueError:
            continue
    return tuple(result) or default


def _default_auth_cookie_secure(environment: str) -> bool:
    return environment.lower() not in {"local", "dev", "development", "test"}


def _is_non_production(environment: str) -> bool:
    return environment.lower() in {"local", "dev", "development", "test"}


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


_REVIEW_SHADOW_CONFIGURATION_INVALID = "review_shadow_configuration_invalid"
_INT4_MAX = 2_147_483_647


def _canonical_notification_settings() -> dict[str, object]:
    invalid = "canonical_notifications_configuration_invalid"
    enabled = os.getenv("VELLA_CANONICAL_NOTIFICATIONS_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise RuntimeError(invalid)
    raw = os.getenv("VELLA_CANONICAL_NOTIFICATION_ACCOUNTS", "")
    accounts: list[tuple[int, int, str]] = []
    for entry in raw.split(",") if raw.strip() else ():
        match = re.fullmatch(r"([1-9][0-9]{0,9}):([1-9][0-9]{0,9}):(wb|avito)", entry.strip())
        if match is None:
            raise RuntimeError(invalid)
        org, account, provider = match.groups()
        item = (int(org), int(account), provider)
        if item[0] > _INT4_MAX or item[1] > _INT4_MAX or item in accounts:
            raise RuntimeError(invalid)
        accounts.append(item)
    return {"canonical_notifications_enabled": enabled == "true",
            "canonical_notification_accounts": tuple(accounts)}


def _canonical_account_discovery_enabled() -> bool:
    raw = os.getenv("VELLA_CANONICAL_ACCOUNT_DISCOVERY_ENABLED", "false").strip().lower()
    if raw not in {"true", "false"}:
        raise RuntimeError("canonical_account_discovery_configuration_invalid")
    return raw == "true"


def _wb_sku_override_settings() -> dict[str, object]:
    invalid = "wb_sku_overrides_configuration_invalid"
    enabled = os.getenv("VELLA_WB_SKU_OVERRIDES_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise RuntimeError(invalid)
    raw = os.getenv("VELLA_WB_SKU_OVERRIDE_ACCOUNT_PAIRS", "")
    pairs: list[tuple[int, int]] = []
    for entry in raw.split(",") if raw.strip() else ():
        match = re.fullmatch(r"([1-9][0-9]{0,9}):([1-9][0-9]{0,9})", entry.strip())
        if match is None:
            raise RuntimeError(invalid)
        pair = tuple(int(value) for value in match.groups())
        if any(value > _INT4_MAX for value in pair) or pair in pairs:
            raise RuntimeError(invalid)
        pairs.append(pair)
    return {"wb_sku_overrides_enabled": enabled == "true", "wb_sku_override_account_pairs": tuple(pairs)}


def _raise_review_shadow_configuration_invalid() -> None:
    raise RuntimeError(_REVIEW_SHADOW_CONFIGURATION_INVALID) from None


def _parse_review_shadow_enabled(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    _raise_review_shadow_configuration_invalid()
    return False


def _parse_review_shadow_id(value: str) -> int:
    if (
        not 1 <= len(value) <= 10
        or value[0] not in "123456789"
        or any(character not in "0123456789" for character in value)
    ):
        _raise_review_shadow_configuration_invalid()
    parsed = int(value)
    if parsed > _INT4_MAX:
        _raise_review_shadow_configuration_invalid()
    return parsed


def _parse_review_shadow_account_pairs(
    value: str | None,
) -> tuple[tuple[int, int], ...]:
    if value is None or not value.strip():
        return ()
    result: list[tuple[int, int]] = []
    for raw_pair in value.split(","):
        parts = raw_pair.strip().split(":")
        if len(parts) != 2:
            _raise_review_shadow_configuration_invalid()
        pair = (
            _parse_review_shadow_id(parts[0]),
            _parse_review_shadow_id(parts[1]),
        )
        if pair in result:
            _raise_review_shadow_configuration_invalid()
        result.append(pair)
    return tuple(result)


def _canonical_avito_stats_settings() -> dict[str, object]:
    invalid = "canonical_avito_stats_configuration_invalid"
    enabled = os.getenv("VELLA_CANONICAL_AVITO_STATS_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise RuntimeError(invalid)
    try:
        # Same strict int4/no-leading-zero/no-duplicate pair grammar as SKU.
        pairs = _parse_review_shadow_account_pairs(
            os.getenv("VELLA_CANONICAL_AVITO_STATS_ACCOUNT_PAIRS")
        )
    except RuntimeError:
        pairs = None
    if pairs is None:
        raise RuntimeError(invalid)
    return {
        "canonical_avito_stats_enabled": enabled == "true",
        "canonical_avito_stats_account_pairs": pairs,
    }


def _canonical_avito_order_status_settings() -> dict[str, object]:
    invalid = "canonical_avito_order_status_configuration_invalid"
    enabled = os.getenv(
        "VELLA_CANONICAL_AVITO_ORDER_STATUS_ENABLED", "false"
    ).strip().lower()
    if enabled not in {"true", "false"}:
        raise RuntimeError(invalid)
    try:
        # Identical strict internal int4 pair grammar to Avito Stats.
        pairs = _parse_review_shadow_account_pairs(
            os.getenv("VELLA_CANONICAL_AVITO_ORDER_STATUS_ACCOUNT_PAIRS")
        )
    except RuntimeError:
        pairs = None
    if pairs is None:
        raise RuntimeError(invalid)
    return {
        "canonical_avito_order_status_enabled": enabled == "true",
        "canonical_avito_order_status_account_pairs": pairs,
    }


def _canonical_avito_reviews_preview_settings() -> dict[str, object]:
    invalid = "canonical_avito_reviews_preview_configuration_invalid"
    enabled = os.getenv(
        "VELLA_CANONICAL_AVITO_REVIEWS_PREVIEW_ENABLED", "false"
    ).strip().lower()
    if enabled not in {"true", "false"}:
        raise RuntimeError(invalid)
    try:
        # Identical strict internal int4 pair grammar to Avito Stats/Orders.
        pairs = _parse_review_shadow_account_pairs(
            os.getenv("VELLA_CANONICAL_AVITO_REVIEWS_PREVIEW_ACCOUNT_PAIRS")
        )
    except RuntimeError:
        pairs = None
    if pairs is None:
        raise RuntimeError(invalid)
    return {
        "canonical_avito_reviews_preview_enabled": enabled == "true",
        "canonical_avito_reviews_preview_account_pairs": pairs,
    }


def _heartbeat_env_settings() -> dict[str, object]:
    """Retain malformed policy as invalid; never repair IDs or disable a bad flag."""
    prefix = "VELLA_PROCESS_HEARTBEAT_"
    raw = os.getenv(prefix + "ENABLED", "false").strip().lower()
    enabled = {"true": True, "false": False}.get(raw)
    values: dict[str, object] = {"process_heartbeat_enabled": enabled}
    if enabled is False:
        return values
    for field in ("namespace", "worker_instance_id", "beat_instance_id"):
        values["process_heartbeat_" + field] = os.getenv(prefix + field.upper())
    for field in ("expected_worker_ids", "expected_beat_ids"):
        raw = os.getenv(prefix + field.upper())
        values["process_heartbeat_" + field] = tuple(raw.split(",")) if raw is not None else ()
    for field in ("max_age_seconds", "retention_seconds", "beat_max_interval_seconds",
                  "redis_connect_timeout_seconds", "redis_socket_timeout_seconds", "redis_retry_attempts"):
        raw = os.getenv(prefix + field.upper())
        try:
            parser = int if field in {"retention_seconds", "redis_retry_attempts"} else float
            value = parser(raw) if raw is not None else None
        except (ValueError, OverflowError):
            value = None
        values["process_heartbeat_" + field] = value
    return values


def _parse_positive_int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_key_versions_env(name: str) -> tuple[int, ...]:
    raw = os.getenv(name)
    if raw is None:
        return ()
    try:
        values = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError:
        return ()
    if not values or any(value < 1 for value in values) or len(values) != len(set(values)):
        return ()
    return values


@dataclass(frozen=True)
class Settings:
    app_name: str = "Vella WB Backend"
    environment: str = "local"
    api_prefix: str = "/api/v1"
    contract_version: str = "0.19.05-runtime-boundary"
    real_price_apply_enabled: bool = False
    repricer_local_price_apply_enabled: bool = True
    repricer_preserve_local_price_overrides: bool = True
    repricer_scheduler_enabled: bool = False
    repricer_execute_interval_minutes: int = 60
    repricer_wb_sync_enabled: bool = True
    wb_live_sync_enabled: bool = False
    repricer_wb_sync_interval_minutes: int = 40
    repricer_wb_sync_period_days: int = 30
    finance_shadow_ingest_enabled: bool = False
    finance_shadow_ingest_organization_ids: tuple[int, ...] = ()
    advertising_shadow_ingest_enabled: bool = False
    advertising_shadow_ingest_organization_ids: tuple[int, ...] = ()
    canonical_shadow_collection_enabled: bool = False
    canonical_shadow_collection_organization_ids: tuple[int, ...] = ()
    review_shadow_enabled: bool = False
    review_shadow_account_pairs: tuple[tuple[int, int], ...] = ()
    canonical_avito_stats_enabled: bool = False
    canonical_avito_stats_account_pairs: tuple[tuple[int, int], ...] = ()
    canonical_avito_order_status_enabled: bool = False
    canonical_avito_order_status_account_pairs: tuple[tuple[int, int], ...] = ()
    canonical_avito_reviews_preview_enabled: bool = False
    canonical_avito_reviews_preview_account_pairs: tuple[tuple[int, int], ...] = ()
    marketplace_credentials_enabled: bool = False
    # Optional nonsecret JSON policy. Parsing belongs to explicit ingestion
    # bootstrap: malformed/partial policy must not prevent unrelated app startup.
    ingestion_policy_config: str | None = None
    marketplace_executor_role: str | None = None
    marketplace_api_runtime_role: str | None = None
    marketplace_credential_keyring_dir: str | None = None
    marketplace_credential_current_key_version: int | None = None
    marketplace_credential_key_versions: tuple[int, ...] = ()
    avito_repricer_worker_enabled: bool = True
    avito_repricer_price_apply_enabled: bool = False
    avito_repricer_execute_interval_minutes: int = 60
    avito_repricer_period_days: int = 30
    avito_returns_sync_enabled: bool = True
    avito_returns_sync_interval_minutes: int = 15
    avito_returns_period_days: int = 30
    spp_api_base_url: str = "https://41-spp.wbcon.su"
    spp_api_token: str | None = None
    spp_api_timeout_seconds: float = 20.0
    spp_api_verify_ssl: bool = True
    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5433/vella_backend"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    wb_api_mode: str = "fake"
    wb_api_base_url: str = "https://discounts-prices-api.wildberries.ru"
    wb_api_token: str | None = None
    wb_api_client_secret: str | None = None
    wb_api_timeout_seconds: float = 20.0
    wb_statistics_api_base_url: str = "https://statistics-api.wildberries.ru"
    wb_statistics_api_token: str | None = None
    wb_statistics_api_timeout_seconds: float = 20.0
    wb_finance_api_base_url: str = "https://finance-api.wildberries.ru"
    wb_finance_api_token: str | None = None
    wb_finance_api_timeout_seconds: float = 20.0
    wb_common_api_base_url: str = "https://common-api.wildberries.ru"
    wb_common_api_token: str | None = None
    wb_common_api_timeout_seconds: float = 20.0
    wb_marketplace_api_base_url: str = "https://marketplace-api.wildberries.ru"
    wb_marketplace_api_token: str | None = None
    wb_marketplace_api_timeout_seconds: float = 20.0
    wb_supplies_api_base_url: str = "https://supplies-api.wildberries.ru"
    wb_supplies_api_token: str | None = None
    wb_supplies_api_timeout_seconds: float = 20.0
    wb_seller_stock_warehouse_ids: tuple[int, ...] = ()
    wb_analytics_api_base_url: str = "https://seller-analytics-api.wildberries.ru"
    wb_analytics_api_token: str | None = None
    wb_analytics_api_timeout_seconds: float = 20.0
    wb_content_api_base_url: str = "https://content-api.wildberries.ru"
    wb_content_api_token: str | None = None
    wb_content_api_timeout_seconds: float = 20.0
    wb_promotions_api_base_url: str = "https://dp-calendar-api.wildberries.ru"
    wb_promotions_api_token: str | None = None
    wb_promotions_api_timeout_seconds: float = 20.0
    wb_ads_api_base_url: str = "https://advert-api.wildberries.ru"
    wb_ads_api_token: str | None = None
    wb_ads_api_timeout_seconds: float = 20.0
    wb_feedbacks_api_base_url: str = "https://feedbacks-api.wildberries.ru"
    wb_feedbacks_api_token: str | None = None
    wb_feedbacks_api_timeout_seconds: float = 20.0
    wb_feedbacks_send_enabled: bool = False
    avito_api_mode: str = "fake"
    avito_api_base_url: str = "https://api.avito.ru"
    avito_api_access_token: str | None = None
    avito_api_timeout_seconds: float = 20.0
    openai_api_key: str | None = None
    openai_api_base_url: str = "https://api.openai.com/v1"
    openai_review_model: str = "gpt-4o-mini"
    openai_review_timeout_seconds: float = 20.0
    one_c_cash_flow_token: str = "change-me"
    one_c_enabled: bool = True
    api_docs_enabled: bool = True
    auth_secret: str = "change-this-secret"
    auth_access_ttl_seconds: int = 900
    auth_refresh_ttl_seconds: int = 2592000
    auth_refresh_rotation_grace_seconds: int = 30
    auth_refresh_cookie_name: str = "vella_refresh_token"
    auth_cookie_secure: bool = True
    auth_cookie_samesite: str = "none"
    auth_cookie_domain: str | None = None
    cabinet_require_persistent_storage: bool = False
    cors_allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    )
    process_heartbeat_enabled: bool | None = False
    process_heartbeat_namespace: str | None = None
    process_heartbeat_worker_instance_id: str | None = None
    process_heartbeat_beat_instance_id: str | None = None
    process_heartbeat_expected_worker_ids: tuple[str, ...] = ()
    process_heartbeat_expected_beat_ids: tuple[str, ...] = ()
    process_heartbeat_max_age_seconds: float | None = None
    process_heartbeat_retention_seconds: int | None = None
    process_heartbeat_beat_max_interval_seconds: float | None = None
    process_heartbeat_redis_connect_timeout_seconds: float | None = None
    process_heartbeat_redis_socket_timeout_seconds: float | None = None
    process_heartbeat_redis_retry_attempts: int | None = None
    canonical_account_discovery_enabled: bool = False
    wb_sku_overrides_enabled: bool = False
    wb_sku_override_account_pairs: tuple[tuple[int, int], ...] = ()
    canonical_notifications_enabled: bool = False
    canonical_notification_accounts: tuple[tuple[int, int, str], ...] = ()


def get_settings() -> Settings:
    redis_url = os.getenv("VELLA_REDIS_URL", "redis://localhost:6379/0")
    environment = os.getenv("VELLA_ENV", "local")
    auth_cookie_secure_raw = os.getenv("VELLA_AUTH_COOKIE_SECURE")
    return Settings(
        **_canonical_avito_stats_settings(),
        **_canonical_avito_order_status_settings(),
        **_canonical_avito_reviews_preview_settings(),
        **_heartbeat_env_settings(),
        **_canonical_notification_settings(),
        **_wb_sku_override_settings(),
        canonical_account_discovery_enabled=_canonical_account_discovery_enabled(),
        app_name=os.getenv("VELLA_APP_NAME", "Vella WB Backend"),
        environment=environment,
        api_prefix=os.getenv("VELLA_API_PREFIX", "/api/v1"),
        contract_version=os.getenv("VELLA_CONTRACT_VERSION", "0.19.05-runtime-boundary"),
        real_price_apply_enabled=os.getenv("VELLA_REAL_PRICE_APPLY_ENABLED", "false").lower() == "true",
        repricer_local_price_apply_enabled=os.getenv("VELLA_REPRICER_LOCAL_PRICE_APPLY_ENABLED", "true").lower() == "true",
        repricer_preserve_local_price_overrides=os.getenv("VELLA_REPRICER_PRESERVE_LOCAL_PRICE_OVERRIDES", "true").lower() == "true",
        repricer_scheduler_enabled=os.getenv("VELLA_REPRICER_SCHEDULER_ENABLED", "false").lower() == "true",
        repricer_execute_interval_minutes=int(os.getenv("VELLA_REPRICER_EXECUTE_INTERVAL_MINUTES", "60")),
        repricer_wb_sync_enabled=os.getenv("VELLA_REPRICER_WB_SYNC_ENABLED", "true").lower() == "true",
        wb_live_sync_enabled=_parse_bool_env("VELLA_WB_LIVE_SYNC_ENABLED", False),
        repricer_wb_sync_interval_minutes=int(os.getenv("VELLA_REPRICER_WB_SYNC_INTERVAL_MINUTES", "40")),
        repricer_wb_sync_period_days=int(os.getenv("VELLA_REPRICER_WB_SYNC_PERIOD_DAYS", "30")),
        finance_shadow_ingest_enabled=_parse_bool_env("VELLA_FINANCE_SHADOW_INGEST_ENABLED", False),
        finance_shadow_ingest_organization_ids=_parse_int_csv_env(
            os.getenv("VELLA_FINANCE_SHADOW_INGEST_ORGANIZATION_IDS")
        ),
        advertising_shadow_ingest_enabled=_parse_bool_env(
            "VELLA_ADVERTISING_SHADOW_INGEST_ENABLED", False
        ),
        advertising_shadow_ingest_organization_ids=_parse_int_csv_env(
            os.getenv("VELLA_ADVERTISING_SHADOW_INGEST_ORGANIZATION_IDS")
        ),
        canonical_shadow_collection_enabled=_parse_bool_env(
            "VELLA_CANONICAL_SHADOW_COLLECTION_ENABLED", False
        ),
        canonical_shadow_collection_organization_ids=_parse_int_csv_env(
            os.getenv("VELLA_CANONICAL_SHADOW_COLLECTION_ORGANIZATION_IDS")
        ),
        review_shadow_enabled=_parse_review_shadow_enabled(
            os.getenv("VELLA_REVIEW_SHADOW_ENABLED")
        ),
        review_shadow_account_pairs=_parse_review_shadow_account_pairs(
            os.getenv("VELLA_REVIEW_SHADOW_ACCOUNT_PAIRS")
        ),
        marketplace_credentials_enabled=_parse_bool_env(
            "VELLA_MARKETPLACE_CREDENTIALS_ENABLED", False
        ),
        ingestion_policy_config=os.getenv("VELLA_INGESTION_POLICY"),
        marketplace_executor_role=_parse_marketplace_role(os.getenv("VELLA_MARKETPLACE_EXECUTOR_ROLE")),
        marketplace_api_runtime_role=_parse_marketplace_role(os.getenv("VELLA_MARKETPLACE_API_RUNTIME_ROLE")),
        marketplace_credential_keyring_dir=os.getenv(
            "VELLA_MARKETPLACE_CREDENTIAL_KEYRING_DIR"
        ),
        marketplace_credential_current_key_version=_parse_positive_int_env(
            "VELLA_MARKETPLACE_CREDENTIAL_CURRENT_KEY_VERSION"
        ),
        marketplace_credential_key_versions=_parse_key_versions_env(
            "VELLA_MARKETPLACE_CREDENTIAL_KEY_VERSIONS"
        ),
        avito_repricer_worker_enabled=os.getenv("VELLA_AVITO_REPRICER_WORKER_ENABLED", "true").lower() == "true",
        avito_repricer_price_apply_enabled=os.getenv("VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED", "false").lower() == "true",
        avito_repricer_execute_interval_minutes=int(os.getenv("VELLA_AVITO_REPRICER_EXECUTE_INTERVAL_MINUTES", "60")),
        avito_repricer_period_days=int(os.getenv("VELLA_AVITO_REPRICER_PERIOD_DAYS", "30")),
        avito_returns_sync_enabled=os.getenv("VELLA_AVITO_RETURNS_SYNC_ENABLED", "true").lower() == "true",
        avito_returns_sync_interval_minutes=int(os.getenv("VELLA_AVITO_RETURNS_SYNC_INTERVAL_MINUTES", "15")),
        avito_returns_period_days=int(os.getenv("VELLA_AVITO_RETURNS_PERIOD_DAYS", "30")),
        spp_api_base_url=os.getenv("VELLA_SPP_API_BASE_URL", "https://41-spp.wbcon.su").rstrip("/"),
        spp_api_token=os.getenv("VELLA_SPP_API_TOKEN"),
        spp_api_timeout_seconds=float(os.getenv("VELLA_SPP_API_TIMEOUT_SECONDS", "20")),
        spp_api_verify_ssl=os.getenv("VELLA_SPP_API_VERIFY_SSL", "true").lower() == "true",
        database_url=os.getenv(
            "VELLA_DATABASE_URL",
            "postgresql+psycopg://postgres:postgres@127.0.0.1:5433/vella_backend",
        ),
        redis_url=redis_url,
        celery_broker_url=os.getenv("VELLA_CELERY_BROKER_URL", redis_url),
        celery_result_backend=os.getenv("VELLA_CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
        wb_api_mode=os.getenv("VELLA_WB_API_MODE", "fake").lower(),
        wb_api_base_url=os.getenv("VELLA_WB_API_BASE_URL", "https://discounts-prices-api.wildberries.ru").rstrip("/"),
        wb_api_token=os.getenv("VELLA_WB_API_TOKEN"),
        wb_api_client_secret=os.getenv("VELLA_WB_API_CLIENT_SECRET"),
        wb_api_timeout_seconds=float(os.getenv("VELLA_WB_API_TIMEOUT_SECONDS", "20")),
        wb_statistics_api_base_url=os.getenv("VELLA_WB_STATISTICS_API_BASE_URL", "https://statistics-api.wildberries.ru").rstrip("/"),
        wb_statistics_api_token=os.getenv("VELLA_WB_STATISTICS_API_TOKEN"),
        wb_statistics_api_timeout_seconds=float(os.getenv("VELLA_WB_STATISTICS_API_TIMEOUT_SECONDS", "20")),
        wb_finance_api_base_url=os.getenv("VELLA_WB_FINANCE_API_BASE_URL", "https://finance-api.wildberries.ru").rstrip("/"),
        wb_finance_api_token=os.getenv("VELLA_WB_FINANCE_API_TOKEN"),
        wb_finance_api_timeout_seconds=float(os.getenv("VELLA_WB_FINANCE_API_TIMEOUT_SECONDS", "20")),
        wb_common_api_base_url=os.getenv("VELLA_WB_COMMON_API_BASE_URL", "https://common-api.wildberries.ru").rstrip("/"),
        wb_common_api_token=os.getenv("VELLA_WB_COMMON_API_TOKEN") or os.getenv("VELLA_WB_API_TOKEN"),
        wb_common_api_timeout_seconds=float(os.getenv("VELLA_WB_COMMON_API_TIMEOUT_SECONDS", "20")),
        wb_marketplace_api_base_url=os.getenv("VELLA_WB_MARKETPLACE_API_BASE_URL", "https://marketplace-api.wildberries.ru").rstrip("/"),
        wb_marketplace_api_token=os.getenv("VELLA_WB_MARKETPLACE_API_TOKEN") or os.getenv("VELLA_WB_API_TOKEN"),
        wb_marketplace_api_timeout_seconds=float(os.getenv("VELLA_WB_MARKETPLACE_API_TIMEOUT_SECONDS", "20")),
        wb_supplies_api_base_url=os.getenv("VELLA_WB_SUPPLIES_API_BASE_URL", "https://supplies-api.wildberries.ru").rstrip("/"),
        wb_supplies_api_token=os.getenv("VELLA_WB_SUPPLIES_API_TOKEN") or os.getenv("VELLA_WB_API_TOKEN"),
        wb_supplies_api_timeout_seconds=float(os.getenv("VELLA_WB_SUPPLIES_API_TIMEOUT_SECONDS", "20")),
        wb_seller_stock_warehouse_ids=_parse_int_csv_env(os.getenv("VELLA_WB_SELLER_STOCK_WAREHOUSE_IDS")),
        wb_analytics_api_base_url=os.getenv("VELLA_WB_ANALYTICS_API_BASE_URL", "https://seller-analytics-api.wildberries.ru").rstrip("/"),
        wb_analytics_api_token=os.getenv("VELLA_WB_ANALYTICS_API_TOKEN"),
        wb_analytics_api_timeout_seconds=float(os.getenv("VELLA_WB_ANALYTICS_API_TIMEOUT_SECONDS", "20")),
        wb_content_api_base_url=os.getenv("VELLA_WB_CONTENT_API_BASE_URL", "https://content-api.wildberries.ru").rstrip("/"),
        wb_content_api_token=os.getenv("VELLA_WB_CONTENT_API_TOKEN"),
        wb_content_api_timeout_seconds=float(os.getenv("VELLA_WB_CONTENT_API_TIMEOUT_SECONDS", "20")),
        wb_promotions_api_base_url=os.getenv("VELLA_WB_PROMOTIONS_API_BASE_URL", "https://dp-calendar-api.wildberries.ru").rstrip("/"),
        wb_promotions_api_token=os.getenv("VELLA_WB_PROMOTIONS_API_TOKEN"),
        wb_promotions_api_timeout_seconds=float(os.getenv("VELLA_WB_PROMOTIONS_API_TIMEOUT_SECONDS", "20")),
        wb_ads_api_base_url=os.getenv("VELLA_WB_ADS_API_BASE_URL", "https://advert-api.wildberries.ru").rstrip("/"),
        wb_ads_api_token=os.getenv("VELLA_WB_ADS_API_TOKEN"),
        wb_ads_api_timeout_seconds=float(os.getenv("VELLA_WB_ADS_API_TIMEOUT_SECONDS", "20")),
        wb_feedbacks_api_base_url=os.getenv("VELLA_WB_FEEDBACKS_API_BASE_URL", "https://feedbacks-api.wildberries.ru").rstrip("/"),
        wb_feedbacks_api_token=os.getenv("VELLA_WB_FEEDBACKS_API_TOKEN"),
        wb_feedbacks_api_timeout_seconds=float(os.getenv("VELLA_WB_FEEDBACKS_API_TIMEOUT_SECONDS", "20")),
        wb_feedbacks_send_enabled=os.getenv("VELLA_WB_FEEDBACKS_SEND_ENABLED", "false").lower() == "true",
        avito_api_mode=os.getenv("VELLA_AVITO_API_MODE", "fake").lower(),
        avito_api_base_url=os.getenv("VELLA_AVITO_API_BASE_URL", "https://api.avito.ru").rstrip("/"),
        avito_api_access_token=os.getenv("VELLA_AVITO_API_ACCESS_TOKEN"),
        avito_api_timeout_seconds=float(os.getenv("VELLA_AVITO_API_TIMEOUT_SECONDS", "20")),
        openai_api_key=os.getenv("OPENAI_API_KEY") or os.getenv("VELLA_OPENAI_API_KEY"),
        openai_api_base_url=os.getenv("VELLA_OPENAI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_review_model=os.getenv("VELLA_OPENAI_REVIEW_MODEL", "gpt-4o-mini"),
        openai_review_timeout_seconds=float(os.getenv("VELLA_OPENAI_REVIEW_TIMEOUT_SECONDS", "20")),
        one_c_cash_flow_token=os.getenv("VELLA_1C_CASH_FLOW_TOKEN", "change-me"),
        one_c_enabled=_parse_bool_env("VELLA_1C_ENABLED", _is_non_production(environment)),
        api_docs_enabled=_parse_bool_env("VELLA_API_DOCS_ENABLED", _is_non_production(environment)),
        auth_secret=os.getenv("VELLA_AUTH_SECRET", "change-this-secret"),
        auth_access_ttl_seconds=int(os.getenv("VELLA_AUTH_ACCESS_TTL_SECONDS", "900")),
        auth_refresh_ttl_seconds=int(os.getenv("VELLA_AUTH_REFRESH_TTL_SECONDS", "2592000")),
        auth_refresh_rotation_grace_seconds=int(os.getenv("VELLA_AUTH_REFRESH_ROTATION_GRACE_SECONDS", "30")),
        auth_refresh_cookie_name=os.getenv("VELLA_AUTH_REFRESH_COOKIE_NAME", "vella_refresh_token"),
        auth_cookie_secure=(
            auth_cookie_secure_raw.lower() == "true"
            if auth_cookie_secure_raw is not None
            else _default_auth_cookie_secure(environment)
        ),
        auth_cookie_samesite=os.getenv(
            "VELLA_AUTH_COOKIE_SAMESITE",
            "lax" if environment.lower() in {"local", "dev", "development", "test"} else "none",
        ).lower(),
        auth_cookie_domain=os.getenv("VELLA_AUTH_COOKIE_DOMAIN"),
        cabinet_require_persistent_storage=_parse_bool_env("VELLA_CABINET_REQUIRE_PERSISTENT_STORAGE", False),
        cors_allowed_origins=_parse_csv_env(
            os.getenv("VELLA_CORS_ALLOWED_ORIGINS"),
            (
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                "http://localhost:5174",
                "http://127.0.0.1:5174",
            ),
        ),
    )


def is_review_shadow_enabled(
    settings: Settings,
    *,
    organization_id: int,
    marketplace_account_id: int,
) -> bool:
    enabled = settings.review_shadow_enabled
    pairs = settings.review_shadow_account_pairs
    if type(enabled) is not bool or type(pairs) is not tuple:
        _raise_review_shadow_configuration_invalid()
    if any(
        type(pair) is not tuple
        or len(pair) != 2
        or not all(type(value) is int and 1 <= value <= _INT4_MAX for value in pair)
        for pair in pairs
    ) or len(pairs) != len(set(pairs)):
        _raise_review_shadow_configuration_invalid()
    if not (
        type(organization_id) is int
        and 1 <= organization_id <= _INT4_MAX
        and type(marketplace_account_id) is int
        and 1 <= marketplace_account_id <= _INT4_MAX
    ):
        return False
    return settings.review_shadow_enabled and (
        organization_id,
        marketplace_account_id,
    ) in settings.review_shadow_account_pairs


def is_wb_sku_overrides_enabled(*, organization_id, marketplace_account_id, settings=None) -> bool:
    settings = get_settings() if settings is None else settings
    if type(settings) is not Settings:
        raise RuntimeError("wb_sku_overrides_configuration_invalid")
    enabled, pairs = settings.wb_sku_overrides_enabled, settings.wb_sku_override_account_pairs
    if (type(enabled) is not bool or type(pairs) is not tuple or any(
            type(pair) is not tuple or len(pair) != 2
            or any(type(value) is not int or not 0 < value <= _INT4_MAX for value in pair)
            for pair in pairs) or len(pairs) != len(set(pairs))):
        raise RuntimeError("wb_sku_overrides_configuration_invalid")
    if any(type(value) is not int or not 0 < value <= _INT4_MAX
           for value in (organization_id, marketplace_account_id)):
        return False
    return enabled and (organization_id, marketplace_account_id) in pairs


def load_marketplace_credential_keyring(settings: Settings) -> CredentialKeyring:
    directory = settings.marketplace_credential_keyring_dir
    current = settings.marketplace_credential_current_key_version
    versions = settings.marketplace_credential_key_versions
    if (
        not settings.marketplace_credentials_enabled
        or directory is None
        or current is None
        or not versions
        or current not in versions
        or not os.path.isabs(directory)
    ):
        raise RuntimeError("marketplace_credential_keyring_invalid")
    try:
        return CredentialKeyring.from_directory(
            directory,
            current_key_version=current,
            required_key_versions=versions,
        )
    except CredentialCryptoError:
        raise RuntimeError("marketplace_credential_keyring_invalid") from None


def validate_security_settings(settings: Settings) -> None:
    if settings.marketplace_credentials_enabled:
        load_marketplace_credential_keyring(settings)
    if _is_non_production(settings.environment):
        return
    if len(settings.auth_secret) < 32 or settings.auth_secret == "change-this-secret":
        raise RuntimeError("VELLA_AUTH_SECRET must be a random value of at least 32 characters in production")
    if settings.one_c_enabled and (
        len(settings.one_c_cash_flow_token) < 32 or settings.one_c_cash_flow_token == "change-me"
    ):
        raise RuntimeError("VELLA_1C_CASH_FLOW_TOKEN must be random when the legacy 1C bridge is enabled")
