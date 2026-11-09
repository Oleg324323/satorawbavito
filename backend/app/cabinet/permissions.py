from __future__ import annotations

from typing import Literal


PermissionProfile = Literal["viewer", "settings_editor", "price_sender", "finance_viewer", "admin", "custom"]

PRODUCTION_PERMISSION_KEYS = frozenset({"production:read", "production:create", "production:assign"})
PRODUCTION_READ_PERMISSIONS = frozenset({"production:read"})
PRODUCTION_CREATE_PERMISSIONS = frozenset({"production:read", "production:create"})
PRODUCTION_ASSIGN_PERMISSIONS = frozenset({"production:read", "production:assign"})
WB_SKU_OVERRIDE_READ_PERMISSIONS = frozenset({"settings:read"})
WB_SKU_OVERRIDE_REPLACE_PERMISSIONS = frozenset({"settings:read", "settings:write"})

LEGACY_PROFILE_ALIASES: dict[str, PermissionProfile] = {
    "admin": "admin",
    "owner": "admin",
    "sales": "price_sender",
    "production": "settings_editor",
    "viewer": "viewer",
    "settings_editor": "settings_editor",
    "price_sender": "price_sender",
    "finance_viewer": "finance_viewer",
    "custom": "custom",
}

PROFILE_PERMISSIONS: dict[PermissionProfile, frozenset[str]] = {
    "viewer": frozenset(
        {
            "settings:read",
            "audit:read",
            "sync:read",
            "reviews:read",
            "cabinet:read",
            "team:read",
            "sessions:read",
            "integrations:read",
            "preferences:read",
            "catalog:read",
        }
    ),
    "settings_editor": frozenset(
        {
            "settings:read",
            "settings:write",
            "settings:submit",
            "audit:read",
            "sync:read",
            "reviews:read",
            "reviews:write",
            "reviews:approve",
            "cabinet:read",
            "team:read",
            "sessions:read",
            "integrations:read",
            "preferences:read",
            "preferences:write",
            "catalog:read",
            "catalog:write",
        }
    ),
    "price_sender": frozenset(
        {
            "settings:read",
            "audit:read",
            "sync:read",
            "price:send",
            "reviews:read",
            "cabinet:read",
            "team:read",
            "sessions:read",
            "integrations:read",
            "preferences:read",
            "preferences:write",
            "catalog:read",
        }
    ),
    "finance_viewer": frozenset(
        {
            "settings:read",
            "audit:read",
            "sync:read",
            "finance:read",
            "reviews:read",
            "cabinet:read",
            "team:read",
            "sessions:read",
            "integrations:read",
            "preferences:read",
            "catalog:read",
            "costs:read",
        }
    ),
    "admin": frozenset(
        {
            "settings:read",
            "settings:write",
            "settings:submit",
            "settings:activate",
            "audit:read",
            "audit:write",
            "sync:read",
            "sync:write",
            "sync:run",
            "price:send",
            "finance:read",
            "reviews:read",
            "reviews:write",
            "reviews:approve",
            "reviews:send",
            "cabinet:read",
            "team:read",
            "team:write",
            "sessions:read",
            "sessions:write",
            "integrations:read",
            "integrations:write",
            "preferences:read",
            "preferences:write",
            "catalog:read",
            "catalog:write",
            "costs:read",
            "costs:write",
        }
    ),
    "custom": frozenset(),
}


def normalize_permission_profile(raw_profile: str) -> PermissionProfile:
    profile = LEGACY_PROFILE_ALIASES.get(raw_profile.strip().lower())
    if not profile:
        raise ValueError(f"INVALID_PERMISSION_PROFILE:{raw_profile}")
    return profile


def permissions_from_profile(profile: str) -> frozenset[str]:
    canonical = normalize_permission_profile(profile)
    return PROFILE_PERMISSIONS[canonical]
