"""Account-owned Review Facts and immutable ingestion evidence.

Revision ID: 20260909_0063
Revises: 20260909_0062
"""

import sqlalchemy as sa

from alembic import op

revision = '20260909_0063'
down_revision = '20260909_0062'
branch_labels = None
depends_on = None

UPGRADE_SQL = r"""
CREATE TABLE public.review_sync_runs_v2 (
    sync_run_id UUID PRIMARY KEY,
    organization_id INTEGER NOT NULL,
    marketplace_account_id INTEGER NOT NULL,
    marketplace VARCHAR(16) NOT NULL CHECK (marketplace IN ('wb','avito')),
    source_run_id TEXT NOT NULL CHECK (length(source_run_id)>0),
    run_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE CHECK (run_sequence>0),
    request_checksum VARCHAR(64) NOT NULL CHECK (request_checksum ~ '^[0-9a-f]{64}$'),
    status VARCHAR(16) NOT NULL CHECK (status IN ('running','complete','partial','failed')),
    completeness VARCHAR(16) NOT NULL CHECK (completeness IN ('complete','partial')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    observed_count BIGINT NOT NULL DEFAULT 0 CHECK (observed_count>=0),
    manifest_checksum VARCHAR(64) CHECK (manifest_checksum ~ '^[0-9a-f]{64}$'),
    coverage JSONB NOT NULL CHECK (jsonb_typeof(coverage)='object'),
    error_code VARCHAR(64),
    CONSTRAINT ck_review_run_terminal CHECK (
      (status='running' AND completed_at IS NULL) OR
      (status<>'running' AND completed_at IS NOT NULL)),
    CONSTRAINT ck_review_run_manifest CHECK (status NOT IN ('complete','partial') OR manifest_checksum IS NOT NULL),
    CONSTRAINT ck_review_run_completeness CHECK (status NOT IN ('running','failed') OR completeness='partial'),
    CONSTRAINT uq_review_run_owner UNIQUE (organization_id,marketplace_account_id,marketplace,sync_run_id),
    CONSTRAINT uq_review_run_sequence UNIQUE (organization_id,marketplace_account_id,marketplace,sync_run_id,run_sequence)
);
CREATE INDEX ix_review_runs_started ON public.review_sync_runs_v2
    (organization_id,marketplace_account_id,marketplace,started_at DESC,sync_run_id);

CREATE TABLE public.review_facts (
    review_id UUID PRIMARY KEY,
    organization_id INTEGER NOT NULL,
    marketplace_account_id INTEGER NOT NULL,
    marketplace VARCHAR(16) NOT NULL CHECK (marketplace IN ('wb','avito')),
    external_review_id TEXT NOT NULL CHECK (length(external_review_id)>0),
    current_observation_id UUID,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version>=0),
    last_source_run_id UUID,
    last_source_run_sequence BIGINT,
    source_order_state VARCHAR(16) NOT NULL DEFAULT 'current' CHECK (source_order_state IN ('current','ambiguous')),
    ambiguous_observation_id UUID,
    first_observed_at TIMESTAMPTZ NOT NULL,
    last_observed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_review_fact_watermark CHECK ((last_source_run_id IS NULL)=(last_source_run_sequence IS NULL)),
    CONSTRAINT ck_review_fact_ambiguity CHECK ((source_order_state='ambiguous')=(ambiguous_observation_id IS NOT NULL)),
    CONSTRAINT uq_review_fact_owner UNIQUE (organization_id,marketplace_account_id,marketplace,review_id),
    CONSTRAINT fk_review_fact_run FOREIGN KEY (organization_id,marketplace_account_id,marketplace,last_source_run_id,last_source_run_sequence)
      REFERENCES public.review_sync_runs_v2 (organization_id,marketplace_account_id,marketplace,sync_run_id,run_sequence) ON DELETE RESTRICT
);
CREATE INDEX ix_review_facts_observed ON public.review_facts
    (organization_id,marketplace_account_id,marketplace,last_observed_at DESC,review_id);

CREATE TABLE public.review_observations (
    observation_id UUID PRIMARY KEY,
    organization_id INTEGER NOT NULL,
    marketplace_account_id INTEGER NOT NULL,
    marketplace VARCHAR(16) NOT NULL CHECK (marketplace IN ('wb','avito')),
    review_id UUID NOT NULL,
    revision BIGINT NOT NULL CHECK (revision>0),
    source_run_id UUID NOT NULL,
    external_product_id TEXT,
    source_created_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMPTZ,
    rating SMALLINT CHECK (rating BETWEEN 1 AND 5),
    text TEXT,
    answered BOOLEAN NOT NULL,
    can_answer BOOLEAN,
    source_status TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    source_schema_version TEXT NOT NULL CHECK (length(source_schema_version)>0),
    normalization_version TEXT NOT NULL CHECK (length(normalization_version)>0),
    content_checksum VARCHAR(64) NOT NULL CHECK (content_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_review_observation_revision UNIQUE (organization_id,marketplace_account_id,marketplace,review_id,revision),
    CONSTRAINT uq_review_observation_owner UNIQUE (organization_id,marketplace_account_id,marketplace,review_id,observation_id),
    CONSTRAINT uq_review_observation_checksum UNIQUE (organization_id,marketplace_account_id,marketplace,review_id,observation_id,content_checksum),
    CONSTRAINT fk_review_observation_fact FOREIGN KEY (organization_id,marketplace_account_id,marketplace,review_id)
      REFERENCES public.review_facts (organization_id,marketplace_account_id,marketplace,review_id) ON DELETE RESTRICT,
    CONSTRAINT fk_review_observation_run FOREIGN KEY (organization_id,marketplace_account_id,marketplace,source_run_id)
      REFERENCES public.review_sync_runs_v2 (organization_id,marketplace_account_id,marketplace,sync_run_id) ON DELETE RESTRICT
);
CREATE INDEX ix_review_observations_revision ON public.review_observations
    (organization_id,marketplace_account_id,marketplace,review_id,revision DESC);
ALTER TABLE public.review_facts ADD CONSTRAINT fk_review_fact_current
    FOREIGN KEY (organization_id,marketplace_account_id,marketplace,review_id,current_observation_id)
    REFERENCES public.review_observations (organization_id,marketplace_account_id,marketplace,review_id,observation_id) ON DELETE RESTRICT;
ALTER TABLE public.review_facts ADD CONSTRAINT fk_review_fact_ambiguous
    FOREIGN KEY (organization_id,marketplace_account_id,marketplace,review_id,ambiguous_observation_id)
    REFERENCES public.review_observations (organization_id,marketplace_account_id,marketplace,review_id,observation_id) ON DELETE RESTRICT;

CREATE TABLE public.review_sync_run_items (
    organization_id INTEGER NOT NULL,
    marketplace_account_id INTEGER NOT NULL,
    marketplace VARCHAR(16) NOT NULL CHECK (marketplace IN ('wb','avito')),
    sync_run_id UUID NOT NULL,
    review_id UUID NOT NULL,
    observation_id UUID NOT NULL,
    content_checksum VARCHAR(64) NOT NULL CHECK (content_checksum ~ '^[0-9a-f]{64}$'),
    ordinal BIGINT NOT NULL CHECK (ordinal>=0),
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (organization_id,marketplace_account_id,marketplace,sync_run_id,review_id),
    CONSTRAINT uq_review_run_item_ordinal UNIQUE (organization_id,marketplace_account_id,marketplace,sync_run_id,ordinal),
    CONSTRAINT fk_review_run_item_run FOREIGN KEY (organization_id,marketplace_account_id,marketplace,sync_run_id)
      REFERENCES public.review_sync_runs_v2 (organization_id,marketplace_account_id,marketplace,sync_run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_review_run_item_observation FOREIGN KEY (organization_id,marketplace_account_id,marketplace,review_id,observation_id,content_checksum)
      REFERENCES public.review_observations (organization_id,marketplace_account_id,marketplace,review_id,observation_id,content_checksum) ON DELETE RESTRICT
);
CREATE INDEX ix_review_run_items_review ON public.review_sync_run_items
    (organization_id,marketplace_account_id,marketplace,review_id,sync_run_id);

DO $$
DECLARE relation text;
BEGIN
  FOREACH relation IN ARRAY ARRAY['review_sync_runs_v2','review_facts','review_observations','review_sync_run_items'] LOOP
    EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (organization_id,marketplace_account_id,marketplace) REFERENCES public.marketplace_accounts (organization_id,marketplace_account_id,marketplace) ON DELETE RESTRICT',relation,'fk_'||relation||'_account');
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',relation);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',relation);
    EXECUTE format('CREATE POLICY %I ON public.%I USING (organization_id=NULLIF(current_setting(''app.organization_id'',true),'''')::integer) WITH CHECK (organization_id=NULLIF(current_setting(''app.organization_id'',true),'''')::integer)',relation||'_org',relation);
  END LOOP;
END $$;

-- Opaque TEXT is unbounded by the typed contract. A B-tree UNIQUE on the
-- complete text rejects valid long IDs. Serialize by the existing canonical
-- account and compare exact text instead; no digest is an identity substitute.
-- Under READ COMMITTED each query after acquiring the lock sees fresh commits.
-- Snapshot isolation cannot offer that guarantee without mutating the account.
CREATE FUNCTION public.review_exact_identity_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE duplicate boolean; logical_constraint text;
BEGIN
  IF current_setting('transaction_isolation')<>'read committed' THEN
    RAISE EXCEPTION 'Review identity insertion requires READ COMMITTED' USING ERRCODE='25000';
  END IF;
  PERFORM 1 FROM public.marketplace_accounts
    WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
      AND marketplace=NEW.marketplace FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Review account binding unavailable' USING ERRCODE='23503';
  END IF;
  IF TG_TABLE_NAME='review_sync_runs_v2' THEN
    SELECT EXISTS (SELECT 1 FROM public.review_sync_runs_v2
      WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
        AND marketplace=NEW.marketplace AND source_run_id COLLATE "C"=NEW.source_run_id COLLATE "C") INTO duplicate;
    logical_constraint := 'uq_review_run_source';
  ELSE
    SELECT EXISTS (SELECT 1 FROM public.review_facts
      WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
        AND marketplace=NEW.marketplace AND external_review_id COLLATE "C"=NEW.external_review_id COLLATE "C") INTO duplicate;
    logical_constraint := 'uq_review_fact_external';
  END IF;
  IF duplicate THEN
    RAISE EXCEPTION 'Review identity already exists'
      USING ERRCODE='23505',CONSTRAINT=logical_constraint;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER review_run_exact_identity BEFORE INSERT ON public.review_sync_runs_v2
FOR EACH ROW EXECUTE FUNCTION public.review_exact_identity_guard();
CREATE TRIGGER review_fact_exact_identity BEFORE INSERT ON public.review_facts
FOR EACH ROW EXECUTE FUNCTION public.review_exact_identity_guard();

CREATE FUNCTION public.review_evidence_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
  RAISE EXCEPTION 'Review evidence is immutable' USING ERRCODE='23514';
END $$;
CREATE TRIGGER review_observations_immutable BEFORE UPDATE OR DELETE ON public.review_observations
FOR EACH ROW EXECUTE FUNCTION public.review_evidence_immutable();
CREATE TRIGGER review_observations_no_truncate BEFORE TRUNCATE ON public.review_observations
FOR EACH STATEMENT EXECUTE FUNCTION public.review_evidence_immutable();
CREATE TRIGGER review_run_items_immutable BEFORE UPDATE OR DELETE ON public.review_sync_run_items
FOR EACH ROW EXECUTE FUNCTION public.review_evidence_immutable();
CREATE TRIGGER review_run_items_no_truncate BEFORE TRUNCATE ON public.review_sync_run_items
FOR EACH STATEMENT EXECUTE FUNCTION public.review_evidence_immutable();

CREATE FUNCTION public.review_fact_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
  IF (NEW.review_id,NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,NEW.external_review_id,NEW.first_observed_at)
    IS DISTINCT FROM (OLD.review_id,OLD.organization_id,OLD.marketplace_account_id,OLD.marketplace,OLD.external_review_id,OLD.first_observed_at)
    OR NEW.version IS DISTINCT FROM OLD.version+1 THEN
    RAISE EXCEPTION 'Review fact identity or version conflict' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER review_fact_guard BEFORE UPDATE ON public.review_facts
FOR EACH ROW EXECUTE FUNCTION public.review_fact_guard();

CREATE FUNCTION public.review_fact_complete() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE valid boolean;
BEGIN
  -- Query the final row, not the INSERT event's temporary null pointer/version.
  SELECT current_observation_id IS NOT NULL AND version>0 INTO valid
  FROM public.review_facts WHERE review_id=NEW.review_id
    AND organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
    AND marketplace=NEW.marketplace;
  IF valid IS DISTINCT FROM TRUE THEN
    RAISE EXCEPTION 'Review fact is incomplete' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER review_fact_complete AFTER INSERT OR UPDATE ON public.review_facts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.review_fact_complete();

CREATE FUNCTION public.review_run_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
  IF OLD.status<>'running' OR
    (NEW.sync_run_id,NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,NEW.source_run_id,NEW.run_sequence,NEW.request_checksum,NEW.started_at)
    IS DISTINCT FROM
    (OLD.sync_run_id,OLD.organization_id,OLD.marketplace_account_id,OLD.marketplace,OLD.source_run_id,OLD.run_sequence,OLD.request_checksum,OLD.started_at) THEN
    RAISE EXCEPTION 'Review run identity or terminal conflict' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER review_run_guard BEFORE UPDATE ON public.review_sync_runs_v2
FOR EACH ROW EXECUTE FUNCTION public.review_run_guard();
"""

NARROW_INHERITED_ACL_SQL = r"""
-- Intersect each inherited grantee's privileges, including PUBLIC, with this
-- contract before the expand transaction commits. Never change old/global ACLs.
DO $$
DECLARE relation record; entry record; target text; allowed text[]; privilege text;
BEGIN
  FOR relation IN
    WITH reviews AS (
      SELECT c.oid,c.relname,c.relowner,c.relacl,c.relkind FROM pg_class c
      WHERE c.relnamespace='public'::regnamespace AND c.relkind='r' AND c.relname IN
        ('review_sync_runs_v2','review_facts','review_observations','review_sync_run_items')
    )
    SELECT * FROM reviews UNION ALL
    SELECT s.oid,s.relname,s.relowner,s.relacl,s.relkind FROM reviews t
      JOIN pg_depend d ON d.refobjid=t.oid AND d.refclassid='pg_class'::regclass
        AND d.classid='pg_class'::regclass AND d.deptype='i'
      JOIN pg_class s ON s.oid=d.objid AND s.relkind='S' AND s.relnamespace='public'::regnamespace
  LOOP
    allowed := CASE WHEN relation.relkind='S' THEN ARRAY['USAGE']
      WHEN relation.relname IN ('review_sync_runs_v2','review_facts') THEN ARRAY['SELECT','INSERT','UPDATE']
      ELSE ARRAY['SELECT','INSERT'] END;
    FOR entry IN SELECT grantee,array_agg(DISTINCT privilege_type) AS privileges
      FROM aclexplode(relation.relacl) WHERE grantee<>relation.relowner GROUP BY grantee
    LOOP
      target := CASE WHEN entry.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(entry.grantee)) END;
      EXECUTE format('REVOKE ALL ON %s public.%I FROM %s',
        CASE WHEN relation.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END,relation.relname,target);
      FOREACH privilege IN ARRAY entry.privileges LOOP
        IF privilege=ANY(allowed) THEN
          IF relation.relname='review_sync_runs_v2' AND privilege='INSERT' THEN
            EXECUTE format('GRANT INSERT (sync_run_id,organization_id,marketplace_account_id,marketplace,source_run_id,request_checksum,status,completeness,started_at,completed_at,observed_count,manifest_checksum,coverage,error_code) ON public.review_sync_runs_v2 TO %s',target);
          ELSE
            EXECUTE format('GRANT %s ON %s public.%I TO %s',privilege,
              CASE WHEN relation.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END,relation.relname,target);
          END IF;
        END IF;
      END LOOP;
    END LOOP;
  END LOOP;
END $$;
"""

DOWNGRADE_SQL = r"""
LOCK TABLE public.review_sync_runs_v2,public.review_facts,public.review_observations,
    public.review_sync_run_items IN ACCESS EXCLUSIVE MODE;
SET LOCAL row_security=off;
DO $$
DECLARE relation text; occupied boolean;
BEGIN
  FOREACH relation IN ARRAY ARRAY['review_sync_runs_v2','review_facts','review_observations','review_sync_run_items'] LOOP
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM public.%I)',relation) INTO occupied;
    IF occupied THEN RAISE EXCEPTION 'Review Facts downgrade blocked: nonempty canonical relation'; END IF;
  END LOOP;
END $$;
ALTER TABLE public.review_facts DROP CONSTRAINT fk_review_fact_current;
ALTER TABLE public.review_facts DROP CONSTRAINT fk_review_fact_ambiguous;
DROP TABLE public.review_sync_run_items;
DROP TABLE public.review_observations;
DROP TABLE public.review_facts;
DROP TABLE public.review_sync_runs_v2;
DROP FUNCTION public.review_evidence_immutable();
DROP FUNCTION public.review_fact_guard();
DROP FUNCTION public.review_fact_complete();
DROP FUNCTION public.review_run_guard();
DROP FUNCTION public.review_exact_identity_guard();
"""


def upgrade():
    op.execute(sa.text(UPGRADE_SQL))
    op.execute(sa.text(NARROW_INHERITED_ACL_SQL))


def downgrade():
    op.execute(sa.text(DOWNGRADE_SQL))
