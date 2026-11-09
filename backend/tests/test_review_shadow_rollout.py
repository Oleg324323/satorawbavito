from __future__ import annotations

import os
import traceback

import pytest

from app import config


@pytest.fixture(autouse=True)
def scrub_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        monkeypatch.delenv(name)


def test_direct_settings_select_only_the_exact_enabled_pair() -> None:
    settings = config.Settings(
        review_shadow_enabled=True,
        review_shadow_account_pairs=((1, 11), (2, 22)),
    )

    assert config.is_review_shadow_enabled(
        settings, organization_id=1, marketplace_account_id=11
    )
    assert not config.is_review_shadow_enabled(
        settings, organization_id=1, marketplace_account_id=22
    )


def test_default_settings_disable_review_shadow() -> None:
    assert not config.is_review_shadow_enabled(
        config.Settings(), organization_id=1, marketplace_account_id=11
    )


@pytest.mark.parametrize("enabled", [False])
@pytest.mark.parametrize("pairs", [(), ((1, 11),)])
def test_disabled_valid_policy_selects_nobody(
    enabled: bool, pairs: tuple[tuple[int, int], ...]
) -> None:
    settings = config.Settings(
        review_shadow_enabled=enabled,
        review_shadow_account_pairs=pairs,
    )

    assert not config.is_review_shadow_enabled(
        settings, organization_id=1, marketplace_account_id=11
    )


@pytest.mark.parametrize(
    "enabled",
    [True, False],
)
def test_enabled_or_disabled_empty_allowlist_selects_nobody(enabled: bool) -> None:
    settings = config.Settings(
        review_shadow_enabled=enabled,
        review_shadow_account_pairs=(),
    )

    assert not config.is_review_shadow_enabled(
        settings, organization_id=1, marketplace_account_id=11
    )


@pytest.mark.parametrize(
    "organization_id,marketplace_account_id",
    [
        (0, 11),
        (-1, 11),
        (True, 11),
        (1.0, 11),
        ("1", 11),
        (1, 0),
        (1, -11),
        (1, False),
        (1, 11.0),
        (1, "11"),
        (2_147_483_648, 11),
        (1, 2_147_483_648),
    ],
)
def test_invalid_requested_ids_select_nobody(
    organization_id: object, marketplace_account_id: object
) -> None:
    settings = config.Settings(
        review_shadow_enabled=True,
        review_shadow_account_pairs=((1, 11),),
    )

    assert not config.is_review_shadow_enabled(
        settings,
        organization_id=organization_id,  # type: ignore[arg-type]
        marketplace_account_id=marketplace_account_id,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("enabled", [1, "true", 1.0, None])
def test_direct_settings_reject_non_bool_enabled(enabled: object) -> None:
    settings = config.Settings(review_shadow_enabled=enabled)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="^review_shadow_configuration_invalid$"):
        config.is_review_shadow_enabled(
            settings, organization_id=1, marketplace_account_id=11
        )


@pytest.mark.parametrize(
    "pairs",
    [
        [],
        [(1, 11)],
        ((1, 11), [2, 22]),
        ((1,),),
        ((1, 11, 111),),
        ((True, 11),),
        ((1, False),),
        ((1.0, 11),),
        ((1, 11.0),),
        (("1", 11),),
        ((1, "11"),),
        ((0, 11),),
        ((1, 0),),
        ((-1, 11),),
        ((1, -11),),
        ((2_147_483_648, 11),),
        ((1, 2_147_483_648),),
        ((1, 11), (1, 11)),
    ],
)
def test_direct_settings_reject_malformed_pair_policy(pairs: object) -> None:
    settings = config.Settings(
        review_shadow_enabled=False,
        review_shadow_account_pairs=pairs,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="^review_shadow_configuration_invalid$"):
        config.is_review_shadow_enabled(
            settings, organization_id=1, marketplace_account_id=11
        )


@pytest.mark.parametrize("raw", ["true", " TRUE ", "TrUe"])
def test_get_settings_accepts_only_canonical_true_spelling_variants(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ENABLED", raw)
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ACCOUNT_PAIRS", " 1:11 , 2:22 ")

    settings = config.get_settings()

    assert settings.review_shadow_enabled is True
    assert settings.review_shadow_account_pairs == ((1, 11), (2, 22))


@pytest.mark.parametrize("raw", ["false", " FALSE ", "FaLsE"])
def test_get_settings_accepts_only_canonical_false_spelling_variants(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ENABLED", raw)

    settings = config.get_settings()

    assert settings.review_shadow_enabled is False
    assert settings.review_shadow_account_pairs == ()


def test_get_settings_preserves_order_and_accepts_positive_int4_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ENABLED", "true")
    monkeypatch.setenv(
        "VELLA_REVIEW_SHADOW_ACCOUNT_PAIRS",
        "2147483647:2147483647,2:2147483647",
    )

    settings = config.get_settings()

    assert settings.review_shadow_account_pairs == (
        (2_147_483_647, 2_147_483_647),
        (2, 2_147_483_647),
    )
    assert config.is_review_shadow_enabled(
        settings,
        organization_id=2_147_483_647,
        marketplace_account_id=2_147_483_647,
    )
    assert config.is_review_shadow_enabled(
        settings, organization_id=2, marketplace_account_id=2_147_483_647
    )


@pytest.mark.parametrize("raw", ["yes", "1", "", "on", "no"])
def test_get_settings_rejects_invalid_enabled_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ENABLED", raw)

    with pytest.raises(RuntimeError, match="^review_shadow_configuration_invalid$"):
        config.get_settings()


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_get_settings_treats_missing_or_whitespace_pairs_as_empty(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ENABLED", "true")
    if raw is not None:
        monkeypatch.setenv("VELLA_REVIEW_SHADOW_ACCOUNT_PAIRS", raw)

    settings = config.get_settings()

    assert settings.review_shadow_account_pairs == ()
    assert not config.is_review_shadow_enabled(
        settings, organization_id=1, marketplace_account_id=11
    )


@pytest.mark.parametrize(
    "raw",
    [
        "0:1",
        "-1:1",
        "+1:2",
        "01:2",
        "1:02",
        "1:2:3",
        "1:2,",
        "١:2",
        "1:٢",
        "1:2,1:2",
        "2147483648:1",
        "1:2147483648",
        "1 :2",
        "1: 2",
        "1:*",
        "9" * 5_000 + ":1",
    ],
)
def test_get_settings_rejects_invalid_pair_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ENABLED", "false")
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ACCOUNT_PAIRS", raw)

    with pytest.raises(RuntimeError, match="^review_shadow_configuration_invalid$"):
        config.get_settings()


def test_configuration_error_never_exposes_raw_pair_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "synthetic-secret-sentinel"
    monkeypatch.setenv("VELLA_REVIEW_SHADOW_ACCOUNT_PAIRS", sentinel)

    with pytest.raises(RuntimeError) as caught:
        config.get_settings()

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "review_shadow_configuration_invalid"
    assert repr(caught.value) == "RuntimeError('review_shadow_configuration_invalid')"
    assert sentinel not in rendered
    assert caught.value.__cause__ is None


def test_get_settings_absent_review_env_preserves_unrelated_defaults() -> None:
    settings = config.get_settings()

    assert settings.review_shadow_enabled is False
    assert settings.review_shadow_account_pairs == ()
    assert (
        settings.canonical_shadow_collection_enabled,
        settings.canonical_shadow_collection_organization_ids,
        settings.finance_shadow_ingest_enabled,
        settings.finance_shadow_ingest_organization_ids,
        settings.advertising_shadow_ingest_enabled,
        settings.advertising_shadow_ingest_organization_ids,
        settings.wb_feedbacks_send_enabled,
        settings.repricer_scheduler_enabled,
    ) == (False, (), False, (), False, (), False, False)
