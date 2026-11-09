"""Account-scoped preliminary WB history staging and immutable EOF evidence."""
from alembic import op

revision = "20260910_0082"
down_revision = "20260910_0081"
branch_labels = depends_on = None

TABLES = ("wb_live_history_pages", "wb_live_history_rows", "wb_live_history_requests")


def upgrade():
    op.execute("""
ALTER TABLE public.wb_live_sync_sources DROP CONSTRAINT wb_live_sync_sources_source_check;
ALTER TABLE public.wb_live_sync_sources ADD CONSTRAINT wb_live_sync_sources_source_check
 CHECK(source IN ('content','prices','wb-statistics-supplier-orders'));
CREATE INDEX wb_live_history_quota ON public.wb_live_sync_sources
 (organization_id,marketplace_account_id,next_due_at DESC) WHERE source='wb-statistics-supplier-orders';
CREATE TABLE public.wb_live_history_pages (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, job_id uuid NOT NULL,
 source text NOT NULL CHECK(source='wb-statistics-supplier-orders'), page_id uuid NOT NULL,
 run_id uuid NOT NULL, lease_token uuid NOT NULL,
 credential_id uuid NOT NULL, credential_generation bigint NOT NULL CHECK(credential_generation>0),
 account_incarnation bigint NOT NULL CHECK(account_incarnation>0),
 input_date_from varchar(40) NOT NULL CHECK(length(input_date_from)>=10),
 request_checksum varchar(64) NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{64}$'),
 state text NOT NULL DEFAULT 'staging' CHECK(state IN ('staging','published')),
 receipt jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), published_at timestamptz,
 PRIMARY KEY(organization_id,marketplace_account_id,job_id,source,page_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,source,run_id,lease_token),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,source)
 REFERENCES public.wb_live_sync_sources(organization_id,marketplace_account_id,job_id,source),
 CHECK((state='staging' AND receipt IS NULL AND published_at IS NULL) OR
 (state='published' AND receipt IS NOT NULL AND published_at IS NOT NULL)),
 CHECK(receipt IS NULL OR (jsonb_typeof(receipt)='object'
 AND receipt ?& ARRAY['next_date_from','terminal','row_count','raw_checksum','request_checksum']
 AND receipt-ARRAY['next_date_from','terminal','row_count','raw_checksum','request_checksum']='{}'::jsonb
 AND jsonb_typeof(receipt->'next_date_from')='string' AND length(receipt->>'next_date_from') BETWEEN 10 AND 40
 AND jsonb_typeof(receipt->'terminal')='boolean' AND jsonb_typeof(receipt->'row_count')='number'
 AND (receipt->>'row_count') ~ '^(0|[1-9][0-9]{0,5})$' AND (receipt->>'row_count')::numeric<=100000
 AND (receipt->>'terminal')::boolean=((receipt->>'row_count')::numeric=0)
 AND jsonb_typeof(receipt->'raw_checksum')='string' AND (receipt->>'raw_checksum') ~ '^[0-9a-f]{64}$'
 AND receipt->>'request_checksum'=request_checksum))
);
CREATE INDEX wb_live_history_published ON public.wb_live_history_pages
 (organization_id,marketplace_account_id,published_at,page_id) WHERE state='published';
CREATE TABLE public.wb_live_history_rows (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, job_id uuid NOT NULL,
 source text NOT NULL CHECK(source='wb-statistics-supplier-orders'), page_id uuid NOT NULL,
 ordinal integer NOT NULL CHECK(ordinal BETWEEN 0 AND 99999), srid varchar(512) NOT NULL CHECK(length(srid)>0),
 nm_id bigint NOT NULL CHECK(nm_id>0), barcode varchar(255) CHECK(length(barcode)>0),
 semantic_checksum varchar(64) NOT NULL CHECK(semantic_checksum ~ '^[0-9a-f]{64}$'),
 source_row_checksum varchar(64) NOT NULL CHECK(source_row_checksum ~ '^[0-9a-f]{64}$'),
 observation jsonb NOT NULL CHECK(jsonb_typeof(observation)='object' AND octet_length(observation::text)<=20000),
 source_revision varchar(40) NOT NULL, effective_at timestamptz NOT NULL,
 PRIMARY KEY(organization_id,marketplace_account_id,job_id,source,page_id,ordinal),
 UNIQUE(organization_id,marketplace_account_id,job_id,source,page_id,srid),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,source,page_id)
 REFERENCES public.wb_live_history_pages(organization_id,marketplace_account_id,job_id,source,page_id)
);
-- Inclusive-boundary semantic identity for a future canonical projector. Keep
-- every page ordinal: dedup must not erase page coverage/provenance evidence.
CREATE INDEX wb_live_history_semantic_identity ON public.wb_live_history_rows
 (organization_id,marketplace_account_id,source,srid,semantic_checksum);
CREATE TABLE public.wb_live_history_requests (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, job_id uuid NOT NULL,
 source text NOT NULL DEFAULT 'wb-statistics-supplier-orders' CHECK(source='wb-statistics-supplier-orders'),
 idempotency_key varchar(128) NOT NULL CHECK(idempotency_key ~ '^[A-Za-z0-9._:-]{8,128}$'),
 date_from varchar(40) NOT NULL CHECK(length(date_from)>=10),
 PRIMARY KEY(organization_id,marketplace_account_id,idempotency_key),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,source)
 REFERENCES public.wb_live_sync_sources(organization_id,marketplace_account_id,job_id,source)
);
CREATE FUNCTION public.wb_live_history_immutable() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE page_state text;
BEGIN
 IF TG_TABLE_NAME='wb_live_history_pages' THEN
  IF TG_OP='INSERT' AND NEW.state='staging' AND NEW.receipt IS NULL AND NEW.published_at IS NULL THEN RETURN NEW; END IF;
  IF TG_OP='UPDATE' AND OLD.state='staging' AND NEW.state='published'
   AND (to_jsonb(NEW)-ARRAY['state','receipt','published_at'])=(to_jsonb(OLD)-ARRAY['state','receipt','published_at'])
   THEN RETURN NEW; END IF;
 ELSIF TG_TABLE_NAME='wb_live_history_rows' AND TG_OP='INSERT' THEN
  SELECT state INTO page_state FROM public.wb_live_history_pages
   WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
    AND job_id=NEW.job_id AND source=NEW.source AND page_id=NEW.page_id FOR SHARE;
  IF page_state='staging' THEN RETURN NEW; END IF;
 END IF;
 RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='WB_HISTORY_IMMUTABLE';
END $$;
REVOKE ALL ON FUNCTION public.wb_live_history_immutable() FROM PUBLIC;
CREATE TRIGGER wb_live_history_page_immutable BEFORE INSERT OR UPDATE OR DELETE ON public.wb_live_history_pages
 FOR EACH ROW EXECUTE FUNCTION public.wb_live_history_immutable();
CREATE TRIGGER wb_live_history_row_immutable BEFORE INSERT OR UPDATE OR DELETE ON public.wb_live_history_rows
 FOR EACH ROW EXECUTE FUNCTION public.wb_live_history_immutable();
""")
    predicate = "organization_id::text = current_setting('app.organization_id',true) AND marketplace_account_id::text = current_setting('app.marketplace_account_id',true)"
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_owner ON public.{table} USING ({predicate}) WITH CHECK ({predicate})")
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")


def downgrade():
    # No implicit retention/deletion policy: a data-bearing downgrade is refused.
    op.execute("""
SET LOCAL row_security=off;
LOCK TABLE public.wb_live_sync_sources,public.wb_live_history_pages,public.wb_live_history_rows,
 public.wb_live_history_requests IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
 IF EXISTS(SELECT FROM public.wb_live_sync_sources WHERE source='wb-statistics-supplier-orders')
  OR EXISTS(SELECT FROM public.wb_live_history_pages) OR EXISTS(SELECT FROM public.wb_live_history_rows)
  OR EXISTS(SELECT FROM public.wb_live_history_requests) THEN
  RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='WB_HISTORY_DOWNGRADE_HAS_DATA';
 END IF;
END $$;
DROP TABLE public.wb_live_history_requests;
DROP TABLE public.wb_live_history_rows;
DROP TABLE public.wb_live_history_pages;
DROP FUNCTION public.wb_live_history_immutable();
DROP INDEX public.wb_live_history_quota;
ALTER TABLE public.wb_live_sync_sources DROP CONSTRAINT wb_live_sync_sources_source_check;
ALTER TABLE public.wb_live_sync_sources ADD CONSTRAINT wb_live_sync_sources_source_check CHECK(source IN ('content','prices'));
""")
