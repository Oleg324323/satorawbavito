"""Durable, account-isolated WB live jobs and provider projections."""
from alembic import op

revision = "20260910_0080"
down_revision = "20260909_0079"
branch_labels = depends_on = None

TABLES = ("wb_live_sync_jobs", "wb_live_sync_sources", "wb_live_sync_requests",
          "wb_live_products", "wb_live_product_sizes", "wb_live_pages")

def upgrade():
    op.execute("""
ALTER TABLE marketplace_account_credentials ADD CONSTRAINT wb_live_credential_owner
 UNIQUE(organization_id,marketplace_account_id,provider,credential_kind,credential_id);
CREATE TABLE wb_live_sync_jobs (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL,
 job_id uuid PRIMARY KEY, credential_id uuid NOT NULL,
 provider text NOT NULL DEFAULT 'wb' CHECK(provider='wb'),
 credential_kind text NOT NULL DEFAULT 'wb_api' CHECK(credential_kind='wb_api'),
 credential_generation bigint NOT NULL CHECK(credential_generation>0), account_incarnation bigint NOT NULL,
 external_account_id varchar(128) NOT NULL, credential_ref varchar(255),
 user_id varchar(64) NOT NULL REFERENCES lk_users(user_id), membership_id integer NOT NULL,
 session_id varchar(64) NOT NULL REFERENCES lk_sessions(session_id),
 state text NOT NULL CHECK(state IN ('queued','running','partial','completed','failed')),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(organization_id,marketplace_account_id,job_id),
 FOREIGN KEY(organization_id,marketplace_account_id) REFERENCES marketplace_accounts(organization_id,marketplace_account_id),
 FOREIGN KEY(organization_id,marketplace_account_id,provider,credential_kind,credential_id)
 REFERENCES marketplace_account_credentials(organization_id,marketplace_account_id,provider,credential_kind,credential_id),
 FOREIGN KEY(organization_id,membership_id) REFERENCES iam_memberships(organization_id,membership_id)
);
CREATE UNIQUE INDEX wb_live_one_active_job ON wb_live_sync_jobs(organization_id,marketplace_account_id)
 WHERE state IN ('queued','running','partial');
CREATE INDEX wb_live_latest_job ON wb_live_sync_jobs(organization_id,marketplace_account_id,created_at DESC,job_id DESC);
CREATE TABLE wb_live_sync_sources (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, job_id uuid NOT NULL,
 source text NOT NULL CHECK(source IN ('content','prices')), run_id uuid NOT NULL UNIQUE,
 state text NOT NULL CHECK(state IN ('queued','running','completed','failed')),
 checkpoint jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(checkpoint)='object'),
 processed bigint NOT NULL DEFAULT 0 CHECK(processed>=0), revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
 attempt integer NOT NULL DEFAULT 0 CHECK(attempt>=0),
 lease_token uuid, lease_expires_at timestamptz, next_due_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(), error_code text,
 PRIMARY KEY(organization_id,marketplace_account_id,job_id,source),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id) REFERENCES wb_live_sync_jobs(organization_id,marketplace_account_id,job_id),
 CHECK((lease_token IS NULL)=(lease_expires_at IS NULL)),
 CHECK(error_code IS NULL OR error_code IN ('WB_LIVE_UNAVAILABLE','WB_ACCESS_DENIED','WB_BINDING_CHANGED',
 'WB_RATE_LIMITED','WB_PROVIDER_UNAVAILABLE','WB_RESPONSE_INVALID','WB_RETRY_EXHAUSTED','WB_BROKER_UNAVAILABLE'))
);
CREATE INDEX wb_live_source_due ON wb_live_sync_sources(organization_id,marketplace_account_id,next_due_at)
 WHERE state IN ('queued','running');
CREATE TABLE wb_live_sync_requests (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, idempotency_key varchar(128) NOT NULL,
 job_id uuid NOT NULL, PRIMARY KEY(organization_id,marketplace_account_id,idempotency_key),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id) REFERENCES wb_live_sync_jobs(organization_id,marketplace_account_id,job_id)
);
CREATE TABLE wb_live_products (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, nm_id bigint NOT NULL CHECK(nm_id>0),
 vendor_code text, title text, brand text, subject_id bigint, subject_name text, photo_url text,
 source_updated_at timestamptz, content_updated_at timestamptz, prices_updated_at timestamptz,
 discount_pct integer CHECK(discount_pct BETWEEN 0 AND 100), club_discount_pct integer CHECK(club_discount_pct BETWEEN 0 AND 100),
 PRIMARY KEY(organization_id,marketplace_account_id,nm_id),
 FOREIGN KEY(organization_id,marketplace_account_id) REFERENCES marketplace_accounts(organization_id,marketplace_account_id)
);
CREATE INDEX wb_live_products_vendor ON wb_live_products(organization_id,marketplace_account_id,vendor_code,nm_id);
CREATE INDEX wb_live_products_title ON wb_live_products(organization_id,marketplace_account_id,title,nm_id);
CREATE INDEX wb_live_products_brand ON wb_live_products(organization_id,marketplace_account_id,brand,nm_id);
CREATE TABLE wb_live_product_sizes (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, nm_id bigint NOT NULL,
 chrt_id bigint NOT NULL CHECK(chrt_id>0), tech_size text, skus jsonb,
 price_kopecks bigint CHECK(price_kopecks>=0), discounted_price_kopecks bigint CHECK(discounted_price_kopecks>=0),
 club_price_kopecks bigint CHECK(club_price_kopecks>=0),
 content_updated_at timestamptz, prices_updated_at timestamptz,
 PRIMARY KEY(organization_id,marketplace_account_id,nm_id,chrt_id),
 FOREIGN KEY(organization_id,marketplace_account_id,nm_id) REFERENCES wb_live_products(organization_id,marketplace_account_id,nm_id)
);
CREATE TABLE wb_live_pages (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, job_id uuid NOT NULL,
 source text NOT NULL, run_id uuid NOT NULL, lease_token uuid NOT NULL, page_digest varchar(64) NOT NULL,
 checkpoint jsonb NOT NULL, row_count integer NOT NULL CHECK(row_count>=0), committed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(organization_id,marketplace_account_id,job_id,source,lease_token),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,source)
 REFERENCES wb_live_sync_sources(organization_id,marketplace_account_id,job_id,source)
);
""")
    op.execute("ALTER TABLE marketplace_accounts ADD COLUMN display_name varchar(128)")
    predicate = "organization_id::text = current_setting('app.organization_id',true) AND marketplace_account_id::text = current_setting('app.marketplace_account_id',true)"
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_owner ON {table} USING ({predicate}) WITH CHECK ({predicate})")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    # Definer owns only a SELECT exception. Runtime roles never own these tables.
    # No user-set tenant value can expand the returned projection beyond locators.
    for table in ("wb_live_sync_jobs", "wb_live_sync_sources"):
        op.execute(f"""DO $$ BEGIN EXECUTE format('CREATE POLICY {table}_dispatch ON {table} FOR SELECT TO %I USING (true)', current_user); END $$""")
    op.execute("""
CREATE FUNCTION public.wb_live_due_jobs(p_limit integer)
RETURNS TABLE(org_id integer, account_id integer, job_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT j.organization_id,j.marketplace_account_id,j.job_id
 FROM public.wb_live_sync_jobs j
 WHERE (j.state IN ('queued','running','partial') OR (j.state='completed' AND NOT EXISTS
 (SELECT 1 FROM public.wb_live_sync_jobs newer WHERE newer.organization_id=j.organization_id
 AND newer.marketplace_account_id=j.marketplace_account_id AND (newer.created_at,newer.job_id)>(j.created_at,j.job_id)))) AND EXISTS (
 SELECT 1 FROM public.wb_live_sync_sources s WHERE s.organization_id=j.organization_id
 AND s.marketplace_account_id=j.marketplace_account_id AND s.job_id=j.job_id
 AND s.state IN ('queued','running','completed') AND s.next_due_at<=clock_timestamp()
 AND (s.lease_expires_at IS NULL OR s.lease_expires_at<=clock_timestamp()))
 ORDER BY j.updated_at,j.job_id LIMIT greatest(0,least(p_limit,100));
$$;
REVOKE ALL ON FUNCTION public.wb_live_due_jobs(integer) FROM PUBLIC;
""")

def downgrade():
    op.execute("DROP FUNCTION public.wb_live_due_jobs(integer)")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE marketplace_accounts DROP COLUMN display_name")
    op.execute("ALTER TABLE marketplace_account_credentials DROP CONSTRAINT wb_live_credential_owner")
