"""Immutable browser-token issuance binding; legacy rows remain unbound."""

from alembic import op

revision = "20260909_0076"
down_revision = "20260909_0075"
branch_labels = depends_on = None


def upgrade() -> None:
    op.execute(r"""
    ALTER TABLE public.marketplace_accounts
      ADD COLUMN ingestion_binding_version bigint NOT NULL DEFAULT 1,
      ADD CONSTRAINT ck_marketplace_accounts_ingestion_version CHECK (ingestion_binding_version > 0);
    ALTER TABLE public.marketplace_account_ingestion_tokens
      ADD COLUMN binding_schema_version smallint,
      ADD COLUMN binding_external_account_id varchar(128) COLLATE "C",
      ADD COLUMN binding_credential_ref varchar(255) COLLATE "C",
      ADD COLUMN binding_version bigint,
      ADD CONSTRAINT ck_ingestion_binding_owner CHECK
        (organization_id BETWEEN 1 AND 2147483647 AND marketplace_account_id BETWEEN 1 AND 2147483647),
      ADD CONSTRAINT ck_ingestion_binding_shape CHECK (
        (binding_schema_version IS NULL AND binding_external_account_id IS NULL
         AND binding_credential_ref IS NULL AND binding_version IS NULL)
        OR (binding_schema_version IS NOT NULL AND binding_schema_version = 1
         AND binding_external_account_id IS NOT NULL AND length(btrim(binding_external_account_id)) > 0
         AND binding_version IS NOT NULL AND binding_version > 0
         AND (binding_credential_ref IS NULL OR length(btrim(binding_credential_ref)) > 0))),
      ADD CONSTRAINT ck_ingestion_binding_times CHECK (
        isfinite(issued_at) AND isfinite(expires_at)
        AND issued_at >= '0001-01-01 00:00:00+00'::timestamptz
        AND expires_at < '10000-01-01 00:00:00+00'::timestamptz
        AND (revoked_at IS NULL OR (isfinite(revoked_at) AND revoked_at >= issued_at
          AND revoked_at < '10000-01-01 00:00:00+00'::timestamptz))
        AND (last_used_at IS NULL OR (isfinite(last_used_at) AND last_used_at >= issued_at
          AND last_used_at < '10000-01-01 00:00:00+00'::timestamptz)));

    CREATE FUNCTION public.ingestion_account_incarnation_guard() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.ingestion_binding_version IS DISTINCT FROM 1::bigint THEN
          RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ingestion_binding_invalid';
        END IF;
      ELSE
        -- Even explicitly supplying OLD+1 is rejected. Only this trigger advances it.
        IF NEW.ingestion_binding_version IS DISTINCT FROM OLD.ingestion_binding_version THEN
          RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ingestion_binding_immutable';
        END IF;
        IF (OLD.marketplace COLLATE "C" = 'avito' OR NEW.marketplace COLLATE "C" = 'avito')
          AND ROW(NEW.marketplace COLLATE "C", NEW.external_account_id COLLATE "C",
                  NEW.credential_ref COLLATE "C", NEW.status COLLATE "C")
          IS DISTINCT FROM ROW(OLD.marketplace COLLATE "C", OLD.external_account_id COLLATE "C",
                  OLD.credential_ref COLLATE "C", OLD.status COLLATE "C") THEN
          IF OLD.ingestion_binding_version = 9223372036854775807 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ingestion_binding_exhausted';
          END IF;
          NEW.ingestion_binding_version := OLD.ingestion_binding_version + 1;
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER ingestion_account_incarnation_guard BEFORE INSERT OR UPDATE
      ON public.marketplace_accounts FOR EACH ROW EXECUTE FUNCTION public.ingestion_account_incarnation_guard();

    CREATE FUNCTION public.ingestion_token_binding_guard() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE a record;
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF OLD.binding_schema_version IS NOT NULL THEN
          RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ingestion_binding_history_required';
        END IF;
        RETURN OLD;
      ELSIF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.token_id,NEW.organization_id,NEW.marketplace_account_id,NEW.provider COLLATE "C",
          NEW.scope COLLATE "C",NEW.verifier,NEW.issued_at,NEW.expires_at,NEW.binding_schema_version,
          NEW.binding_external_account_id COLLATE "C",NEW.binding_credential_ref COLLATE "C",NEW.binding_version)
          IS DISTINCT FROM ROW(OLD.token_id,OLD.organization_id,OLD.marketplace_account_id,OLD.provider COLLATE "C",
          OLD.scope COLLATE "C",OLD.verifier,OLD.issued_at,OLD.expires_at,OLD.binding_schema_version,
          OLD.binding_external_account_id COLLATE "C",OLD.binding_credential_ref COLLATE "C",OLD.binding_version)
          OR (OLD.revoked_at IS NOT NULL AND ROW(NEW.revoked_at,NEW.revocation_reason_code COLLATE "C")
            IS DISTINCT FROM ROW(OLD.revoked_at,OLD.revocation_reason_code COLLATE "C"))
          OR (OLD.last_used_at IS NOT NULL AND (NEW.last_used_at IS NULL OR NEW.last_used_at < OLD.last_used_at)) THEN
          RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ingestion_binding_immutable';
        END IF;
      ELSIF NEW.binding_schema_version IS NOT NULL THEN
        -- Metadata-only account lock, under the existing tenant RLS. No credential read.
        SELECT external_account_id,credential_ref,status,ingestion_binding_version INTO a
          FROM public.marketplace_accounts WHERE organization_id=NEW.organization_id
          AND marketplace_account_id=NEW.marketplace_account_id AND marketplace COLLATE "C"='avito' FOR UPDATE;
        IF NOT FOUND OR a.status COLLATE "C" <> 'connected'
          OR ROW(a.external_account_id COLLATE "C",a.credential_ref COLLATE "C",a.ingestion_binding_version)
             IS DISTINCT FROM ROW(NEW.binding_external_account_id COLLATE "C",NEW.binding_credential_ref COLLATE "C",NEW.binding_version)
          OR NEW.revoked_at IS NOT NULL OR NEW.last_used_at IS NOT NULL
          OR NEW.expires_at <= clock_timestamp() THEN
          RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ingestion_binding_invalid';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER ingestion_token_binding_guard BEFORE INSERT OR UPDATE OR DELETE
      ON public.marketplace_account_ingestion_tokens FOR EACH ROW EXECUTE FUNCTION public.ingestion_token_binding_guard();
    REVOKE ALL ON FUNCTION public.ingestion_account_incarnation_guard(), public.ingestion_token_binding_guard() FROM PUBLIC;
    """)


def downgrade() -> None:
    # Fail if migration ownership cannot see all rows; never erase hidden history.
    op.execute(r"""
    SET LOCAL row_security = off;
    LOCK TABLE public.marketplace_accounts, public.marketplace_account_ingestion_tokens IN ACCESS EXCLUSIVE MODE;
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM public.marketplace_account_ingestion_tokens WHERE binding_schema_version IS NOT NULL)
        OR EXISTS (SELECT 1 FROM public.marketplace_accounts WHERE ingestion_binding_version <> 1) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ingestion_binding_downgrade_refused';
      END IF;
    END $$;
    DROP TRIGGER ingestion_token_binding_guard ON public.marketplace_account_ingestion_tokens;
    DROP TRIGGER ingestion_account_incarnation_guard ON public.marketplace_accounts;
    DROP FUNCTION public.ingestion_token_binding_guard();
    DROP FUNCTION public.ingestion_account_incarnation_guard();
    ALTER TABLE public.marketplace_account_ingestion_tokens
      DROP CONSTRAINT ck_ingestion_binding_times, DROP CONSTRAINT ck_ingestion_binding_owner,
      DROP CONSTRAINT ck_ingestion_binding_shape, DROP COLUMN binding_schema_version,
      DROP COLUMN binding_external_account_id, DROP COLUMN binding_credential_ref, DROP COLUMN binding_version;
    ALTER TABLE public.marketplace_accounts DROP CONSTRAINT ck_marketplace_accounts_ingestion_version,
      DROP COLUMN ingestion_binding_version;
    """)
