"""One durable account-owned WB Prices budget shared by readers and price executors."""
from alembic import op

revision = "20260910_0084"
down_revision = "20260910_0083"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE public.wb_price_quota (
 organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 provider text NOT NULL CHECK(provider='wb'),
 category text NOT NULL CHECK(category='prices_discounts'),
 next_allowed_at timestamptz NOT NULL CHECK(isfinite(next_allowed_at)),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK(isfinite(created_at)),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK(isfinite(updated_at)),
 PRIMARY KEY(organization_id,marketplace_account_id,provider,category),
 FOREIGN KEY(organization_id,marketplace_account_id,provider)
 REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace)
);
ALTER TABLE public.wb_price_quota ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wb_price_quota FORCE ROW LEVEL SECURITY;
CREATE POLICY wb_price_quota_owner ON public.wb_price_quota
 USING(organization_id::text=current_setting('app.organization_id',true)
   AND marketplace_account_id::text=current_setting('app.marketplace_account_id',true))
 WITH CHECK(organization_id::text=current_setting('app.organization_id',true)
   AND marketplace_account_id::text=current_setting('app.marketplace_account_id',true));
REVOKE ALL ON public.wb_price_quota FROM PUBLIC;
-- Remove inherited owner default ACLs only on this newly created object.
-- Global defaults and every other relation remain unchanged.
DO $$ DECLARE recipient record; BEGIN
 FOR recipient IN SELECT DISTINCT x.grantee FROM pg_class c
 CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) x
 WHERE c.oid='public.wb_price_quota'::regclass AND x.grantee<>c.relowner LOOP
  EXECUTE format('REVOKE ALL ON public.wb_price_quota FROM %s',
   CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(recipient.grantee)) END);
 END LOOP;
END $$;
""")


def downgrade():
    op.execute("""
SET LOCAL row_security=off;
LOCK TABLE public.wb_price_quota IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
 IF EXISTS(SELECT FROM public.wb_price_quota) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='WB_PRICE_QUOTA_DOWNGRADE_HAS_DATA';
 END IF;
END $$;
DROP TABLE public.wb_price_quota;
""")
