"""Typed, dormant Repricer settings, assignments and liquidation history.

No organization mutation permission is supplied by this migration. The trusted
service owns authentication, mapping authorization and historical replay lookup.
"""

from alembic import op

revision = "20260909_0075"
down_revision = "20260909_0074"
branch_labels = depends_on = None

PREFIX = "wb_repricing_"
FAMILIES = ("settings", "assignment", "liquidation")
TABLES = (
    "settings_versions", "settings_heads", "basket_norm_defaults",
    "assignment_versions", "assignment_heads", "liquidation_campaigns",
    "liquidation_versions", "liquidation_heads", "state_audit",
)
BOOLS = (
    "night_median_enabled", "night_median_collect_enabled", "night_median_global",
    "night_median_auto_apply_enabled", "worker_auto_apply_prices_enabled",
    "min_price_sync_enabled", "price_jump_protection_enabled", "discount_step_enabled",
    "price_rounding_enabled", "liquidation_auto_flag_enabled",
)
DECIMALS = (
    "target_margin_pct", "price_step_pct", "max_price_change_daily_pct",
    "promo_margin_threshold_pct", "price_jump_stock_value_min_pct",
    "price_jump_spp_min_pct", "price_jump_stock_qty_min_pct", "csv_max_cost_drop_pct",
    "csv_max_price_drop_pct", "discount_step_pct", "night_median_apply_delta_pct",
    "warmup_margin_pct", "warmup_daily_limit_pct", "liquidation_step_pct",
    "liquidation_min_cogs_pct",
)
POSITIVE = (
    "sync_interval_minutes", "basket_norm_period_days", "cart_comparison_days",
    "plan_fact_fact_period_days", "plan_fact_interval_hours", "warmup_days",
)
NONNEGATIVE = (
    "cart_high_baskets_threshold", "cart_low_baskets_threshold",
    "basket_norm_auto_min_orders", "warmup_exit_baskets",
)
HOURS = ("night_median_window_start_hour", "night_median_window_end_hour")
ENUMS = {
    "night_median_mode": "'conservative','aggressive'",
    "basket_signal_mode": "'matrix','thresholds'",
    "basket_norm_mode": "'fallback_by_type','auto','manual'",
    "plan_fact_metric": "'orders','revenue','margin'",
}
SETTINGS_FIELDS = BOOLS + DECIMALS + POSITIVE + NONNEGATIVE + HOURS + tuple(ENUMS) + ("night_median_timezone",)
LIQUIDATION_FIELDS = (
    "state", "current_price_kopecks", "target_price_kopecks", "step_pct", "hold_orders_to",
    "next_step_at", "requires_negative_margin_confirm", "confirmed_by_membership_id",
    "confirmed_at", "resulting_approval_id", "resulting_approval_row_id",
)
PRIMITIVES = ("wb_state_uuid(uuid)", "wb_state_time(timestamptz)",
              "wb_state_timestamp(timestamptz)", "wb_state_json(jsonb)")
CODECS = (
    "wb_state_settings_bytes(public.wb_repricing_settings_versions,public.wb_repricing_basket_norm_defaults[])",
    "wb_state_assignment_bytes(public.wb_repricing_assignment_versions)",
    "wb_state_liquidation_bytes(public.wb_repricing_liquidation_versions,public.wb_repricing_liquidation_campaigns)",
)
DEPENDENCIES = ("repricer_exact_text(text)", "repricer_ascii_json_string(text)",
                "wb_sku_override_decimal(numeric)", "wb_sku_override_integral(numeric)")

HELPERS = r"""
CREATE FUNCTION public.wb_state_uuid(v uuid) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT v IS NOT NULL AND v::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' $$;
CREATE FUNCTION public.wb_state_time(v timestamptz) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT v IS NOT NULL AND isfinite(v) AND v>='0001-01-01 00:00:00+00'::timestamptz
 AND v<'10000-01-01 00:00:00+00'::timestamptz $$;
CREATE FUNCTION public.wb_state_timestamp(v timestamptz) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF v IS NULL THEN RETURN NULL; END IF;
 IF NOT public.wb_state_time(v) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_time_invalid'; END IF;
 RETURN to_char(v AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
END $$;
-- JSON exists only transiently in the codec; no JSON storage or input patch API.
CREATE FUNCTION public.wb_state_json(v jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE result text;
BEGIN
 CASE jsonb_typeof(v)
 WHEN 'object' THEN
  SELECT '{'||coalesce(string_agg(public.repricer_ascii_json_string(key)||':'||public.wb_state_json(value),',' ORDER BY key COLLATE "C"),'')||'}'
  INTO result FROM jsonb_each(v);
 WHEN 'array' THEN
  SELECT '['||coalesce(string_agg(public.wb_state_json(value),',' ORDER BY ord),'')||']'
  INTO result FROM jsonb_array_elements(v) WITH ORDINALITY t(value,ord);
 WHEN 'string' THEN result:=public.repricer_ascii_json_string(v#>>'{}');
 WHEN 'number' THEN result:=public.wb_sku_override_decimal((v#>>'{}')::numeric);
 WHEN 'boolean' THEN result:=v::text;
 WHEN 'null' THEN result:='null';
 ELSE RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_invalid';
 END CASE;
 RETURN result;
END $$;
"""


def owner(family):
    return ("organization_id" if family == "settings" else
            "organization_id,marketplace_account_id,catalog_sku_id" +
            (",campaign_id" if family == "liquidation" else ""))


def owner_columns(family):
    result = "organization_id integer NOT NULL CHECK(organization_id>0) REFERENCES public.lk_organizations(organization_id),"
    if family != "settings":
        result += """
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 catalog_sku_id integer NOT NULL CHECK(catalog_sku_id>0),
 marketplace text COLLATE "C" NOT NULL CHECK(marketplace='wb'),
 FOREIGN KEY(organization_id,marketplace_account_id,marketplace)
 REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
 FOREIGN KEY(organization_id,catalog_sku_id) REFERENCES public.catalog_skus(organization_id,catalog_sku_id),
"""
    if family == "liquidation":
        result += "campaign_id uuid NOT NULL CHECK(public.wb_state_uuid(campaign_id)),"
    return result


METADATA = """
 revision numeric NOT NULL CHECK(public.wb_sku_override_integral(revision) AND revision>0),
 parent_revision numeric,
 command_id uuid NOT NULL CHECK(public.wb_state_uuid(command_id)),
 audit_id uuid NOT NULL CHECK(public.wb_state_uuid(audit_id)),
 actor_membership_id integer NOT NULL CHECK(actor_membership_id>0),
 created_at timestamptz NOT NULL CHECK(public.wb_state_time(created_at)),
 request_payload bytea NOT NULL,
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{64}$'
  AND request_checksum=encode(sha256(request_payload),'hex')),
 CHECK((revision=1 AND parent_revision IS NULL) OR
  (revision>1 AND parent_revision IS NOT NULL AND parent_revision=revision-1)),
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
"""
LIQUIDATION_VALUES = """
 state text COLLATE "C" NOT NULL CHECK(state IN ('active','paused','completed','cancelled')),
 current_price_kopecks numeric NOT NULL CHECK(public.wb_sku_override_integral(current_price_kopecks) AND current_price_kopecks>0),
 target_price_kopecks numeric NOT NULL CHECK(public.wb_sku_override_integral(target_price_kopecks) AND target_price_kopecks>0),
 step_pct numeric NOT NULL CHECK(step_pct::text NOT IN ('NaN','Infinity','-Infinity') AND step_pct>0),
 hold_orders_to integer NOT NULL CHECK(hold_orders_to>=0),
 next_step_at timestamptz CHECK(next_step_at IS NULL OR public.wb_state_time(next_step_at)),
 requires_negative_margin_confirm boolean NOT NULL,
 confirmed_by_membership_id integer CHECK(confirmed_by_membership_id>0),
 confirmed_at timestamptz CHECK(confirmed_at IS NULL OR public.wb_state_time(confirmed_at)),
 resulting_approval_id text COLLATE "C" CHECK(resulting_approval_id IS NULL OR public.repricer_exact_text(resulting_approval_id)),
 resulting_approval_row_id uuid,
 CHECK((state='active' AND next_step_at IS NOT NULL) OR (state<>'active' AND next_step_at IS NULL)),
 CHECK((confirmed_by_membership_id IS NULL)=(confirmed_at IS NULL)),
 CHECK((resulting_approval_id IS NULL)=(resulting_approval_row_id IS NULL)),
 FOREIGN KEY(organization_id,confirmed_by_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 FOREIGN KEY(organization_id,marketplace_account_id,resulting_approval_row_id)
 REFERENCES public.wb_repricer_price_approvals(organization_id,marketplace_account_id,approval_row_id) DEFERRABLE INITIALLY DEFERRED,
"""


def models():
    settings = "formula_compatibility_version text COLLATE \"C\" NOT NULL CHECK(public.repricer_exact_text(formula_compatibility_version)),"
    settings += "".join(f"{c} boolean NOT NULL," for c in BOOLS)
    settings += "".join(f"{c} numeric NOT NULL CHECK({c}::text NOT IN ('NaN','Infinity','-Infinity'))," for c in DECIMALS)
    settings += "".join(f"{c} integer NOT NULL CHECK({c}>0)," for c in POSITIVE)
    settings += "".join(f"{c} integer NOT NULL CHECK({c}>=0)," for c in NONNEGATIVE)
    settings += "".join(f"{c} integer NOT NULL CHECK({c} BETWEEN 0 AND 23)," for c in HOURS)
    settings += "".join(f"{c} text COLLATE \"C\" NOT NULL CHECK({c} IN ({values}))," for c, values in ENUMS.items())
    settings += 'night_median_timezone text COLLATE "C" NOT NULL CHECK(public.repricer_exact_text(night_median_timezone)),'
    assignment = """
 strategy_id text COLLATE "C" CHECK(strategy_id IN ('baskets_orders','night_price_mode','plan_fact_interval','illiquid')),
 interval_hours integer CHECK(interval_hours>0),
 assigned_at timestamptz NOT NULL CHECK(public.wb_state_time(assigned_at)),
 source text COLLATE "C" NOT NULL CHECK(source IN ('manual','legacy_import')),
 CHECK((strategy_id IS NOT DISTINCT FROM 'plan_fact_interval' AND interval_hours IS NOT NULL)
 OR (strategy_id IS DISTINCT FROM 'plan_fact_interval' AND interval_hours IS NULL)),
"""
    for family, values in (("settings", settings), ("assignment", assignment), ("liquidation", LIQUIDATION_VALUES)):
        key = owner(family)
        op.execute(f"""CREATE TABLE public.{PREFIX}{family}_versions (
 {owner_columns(family)} {METADATA} {values}
 PRIMARY KEY({key},revision), UNIQUE({key},command_id),
 FOREIGN KEY({key},parent_revision) REFERENCES public.{PREFIX}{family}_versions({key},revision) DEFERRABLE INITIALLY DEFERRED
 )""")
        op.execute(f"""CREATE TABLE public.{PREFIX}{family}_heads (
 {owner_columns(family)}
 current_revision numeric NOT NULL,
 version numeric NOT NULL CHECK(public.wb_sku_override_integral(version) AND version>0 AND version=current_revision),
 updated_at timestamptz NOT NULL CHECK(public.wb_state_time(updated_at)),
 {LIQUIDATION_VALUES if family == 'liquidation' else ''}
 PRIMARY KEY({key}),
 FOREIGN KEY({key},current_revision) REFERENCES public.{PREFIX}{family}_versions({key},revision) DEFERRABLE INITIALLY DEFERRED
 )""")
        op.execute(f"ALTER TABLE public.{PREFIX}{family}_versions ADD CONSTRAINT wb_state_{family}_head_fk FOREIGN KEY({key}) REFERENCES public.{PREFIX}{family}_heads({key}) DEFERRABLE INITIALLY DEFERRED")
    op.execute(f"""CREATE TABLE public.{PREFIX}basket_norm_defaults (
 organization_id integer NOT NULL CHECK(organization_id>0),
 settings_revision numeric NOT NULL CHECK(public.wb_sku_override_integral(settings_revision) AND settings_revision>0),
 garment text COLLATE "C" NOT NULL CHECK(garment IN ('tshirt','hoodie','longsleeve')),
 norm_units integer NOT NULL CHECK(norm_units>=0),
 PRIMARY KEY(organization_id,settings_revision,garment),
 FOREIGN KEY(organization_id,settings_revision) REFERENCES public.{PREFIX}settings_versions(organization_id,revision) DEFERRABLE INITIALLY DEFERRED
 );
 CREATE TABLE public.{PREFIX}liquidation_campaigns (
 {owner_columns('liquidation')}
 created_at timestamptz NOT NULL CHECK(public.wb_state_time(created_at)),
 started_by_membership_id integer NOT NULL CHECK(started_by_membership_id>0),
 start_price_kopecks numeric NOT NULL CHECK(public.wb_sku_override_integral(start_price_kopecks) AND start_price_kopecks>0),
 PRIMARY KEY({owner('liquidation')}),
 FOREIGN KEY(organization_id,started_by_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 FOREIGN KEY({owner('liquidation')}) REFERENCES public.{PREFIX}liquidation_heads({owner('liquidation')}) DEFERRABLE INITIALLY DEFERRED
 );
 ALTER TABLE public.{PREFIX}liquidation_versions ADD CONSTRAINT wb_state_campaign_fk
 FOREIGN KEY({owner('liquidation')}) REFERENCES public.{PREFIX}liquidation_campaigns({owner('liquidation')}) DEFERRABLE INITIALLY DEFERRED;
 CREATE UNIQUE INDEX wb_state_unfinished_slot ON public.{PREFIX}liquidation_heads
 (organization_id,marketplace_account_id,catalog_sku_id) WHERE state IN ('active','paused');
 """)
    matrix = []
    for family in FAMILIES:
        terms = [f"domain='{family}'", f"{family}_after IS NOT NULL"]
        terms += [f"{other}_before IS NULL AND {other}_after IS NULL" for other in FAMILIES if other != family]
        terms += ["marketplace_account_id IS NULL AND catalog_sku_id IS NULL AND marketplace IS NULL AND campaign_id IS NULL" if family == "settings" else
                  "marketplace_account_id IS NOT NULL AND catalog_sku_id IS NOT NULL AND marketplace='wb' AND marketplace IS NOT NULL AND campaign_id IS " + ("NOT NULL" if family == "liquidation" else "NULL")]
        matrix.append("(" + " AND ".join(terms) + ")")
    op.execute(f"""CREATE TABLE public.{PREFIX}state_audit (
 audit_id uuid PRIMARY KEY CHECK(public.wb_state_uuid(audit_id)),
 organization_id integer NOT NULL CHECK(organization_id>0) REFERENCES public.lk_organizations(organization_id),
 domain text COLLATE "C" NOT NULL CHECK(domain IN ('settings','assignment','liquidation')),
 marketplace_account_id integer CHECK(marketplace_account_id>0), catalog_sku_id integer CHECK(catalog_sku_id>0),
 marketplace text COLLATE "C", campaign_id uuid CHECK(campaign_id IS NULL OR public.wb_state_uuid(campaign_id)),
 command_id uuid NOT NULL CHECK(public.wb_state_uuid(command_id)),
 actor_membership_id integer NOT NULL CHECK(actor_membership_id>0),
 occurred_at timestamptz NOT NULL CHECK(public.wb_state_time(occurred_at)),
 event_kind text COLLATE "C" NOT NULL CHECK(event_kind IN ('created','replaced','cleared','paused','resumed','completed','cancelled')),
 settings_before numeric, settings_after numeric,
 assignment_before numeric, assignment_after numeric,
 liquidation_before numeric, liquidation_after numeric,
 UNIQUE(organization_id,audit_id),
 UNIQUE NULLS NOT DISTINCT(organization_id,domain,marketplace_account_id,catalog_sku_id,campaign_id,command_id),
 CHECK({' OR '.join(matrix)}),
 FOREIGN KEY(organization_id,marketplace_account_id,marketplace) REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
 FOREIGN KEY(organization_id,catalog_sku_id) REFERENCES public.catalog_skus(organization_id,catalog_sku_id),
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id)
 )""")
    for family in FAMILIES:
        key = owner(family)
        op.execute(f"""ALTER TABLE public.{PREFIX}state_audit
 ADD CONSTRAINT wb_state_{family}_audit_step CHECK({family}_after IS NULL OR
 (public.wb_sku_override_integral({family}_after) AND {family}_after>0 AND {family}_after=coalesce({family}_before,0)+1
 AND ({family}_before IS NULL OR (public.wb_sku_override_integral({family}_before) AND {family}_before>0)))),
 ADD CONSTRAINT wb_state_{family}_before_fk FOREIGN KEY({key},{family}_before)
 REFERENCES public.{PREFIX}{family}_versions({key},revision) DEFERRABLE INITIALLY DEFERRED,
 ADD CONSTRAINT wb_state_{family}_after_fk FOREIGN KEY({key},{family}_after)
 REFERENCES public.{PREFIX}{family}_versions({key},revision) DEFERRABLE INITIALLY DEFERRED;
 ALTER TABLE public.{PREFIX}{family}_versions ADD CONSTRAINT wb_state_{family}_audit_fk
 FOREIGN KEY(organization_id,audit_id) REFERENCES public.{PREFIX}state_audit(organization_id,audit_id) DEFERRABLE INITIALLY DEFERRED;
 """)


def codecs():
    values = ",".join(f"'{c}'," + (f"public.wb_sku_override_decimal(v.{c})" if c in DECIMALS else f"v.{c}") for c in SETTINGS_FIELDS)
    op.execute(f"""
 CREATE FUNCTION public.wb_state_settings_bytes(v public.{PREFIX}settings_versions, children public.{PREFIX}basket_norm_defaults[]) RETURNS bytea
 LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT convert_to(public.wb_state_json(jsonb_build_object(
 'actorMembershipId',v.actor_membership_id,'basketDefaults',coalesce((SELECT jsonb_agg(jsonb_build_object('garment',c.garment,'normUnits',c.norm_units) ORDER BY c.garment COLLATE "C") FROM unnest(children) c),'[]'::jsonb),
 'commandId',v.command_id::text,'commandKind','replace_settings','expectedVersion',public.wb_sku_override_decimal(v.revision-1),
 'formulaCompatibilityVersion',v.formula_compatibility_version,'organizationId',v.organization_id,
 'schema','wb-repricing-algorithm-settings/v1','values',jsonb_build_object({values}))), 'UTF8') $$;
 CREATE FUNCTION public.wb_state_assignment_bytes(v public.{PREFIX}assignment_versions) RETURNS bytea
 LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT convert_to(public.wb_state_json(jsonb_build_object(
 'actorMembershipId',v.actor_membership_id,'assignedAt',public.wb_state_timestamp(v.assigned_at),
 'catalogSkuId',v.catalog_sku_id,'commandId',v.command_id::text,'commandKind','replace_assignment',
 'expectedVersion',public.wb_sku_override_decimal(v.revision-1),'intervalHours',v.interval_hours,
 'marketplaceAccountId',v.marketplace_account_id,'organizationId',v.organization_id,
 'schema','wb-repricing-assignment/v1','source',v.source,'strategyId',v.strategy_id)), 'UTF8') $$;
 CREATE FUNCTION public.wb_state_liquidation_bytes(v public.{PREFIX}liquidation_versions,c public.{PREFIX}liquidation_campaigns) RETURNS bytea
 LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT convert_to(public.wb_state_json(jsonb_build_object(
 'actorMembershipId',v.actor_membership_id,'campaign',jsonb_build_object(
 'createdAt',public.wb_state_timestamp(c.created_at),'startPriceKopecks',public.wb_sku_override_decimal(c.start_price_kopecks),'startedByMembershipId',c.started_by_membership_id),
 'campaignId',v.campaign_id::text,'catalogSkuId',v.catalog_sku_id,'commandId',v.command_id::text,
 'commandKind','replace_liquidation','expectedVersion',public.wb_sku_override_decimal(v.revision-1),
 'marketplaceAccountId',v.marketplace_account_id,'organizationId',v.organization_id,'schema','wb-repricing-liquidation/v1',
 'values',jsonb_build_object('confirmed_at',public.wb_state_timestamp(v.confirmed_at),'confirmed_by_membership_id',v.confirmed_by_membership_id,
 'current_price_kopecks',public.wb_sku_override_decimal(v.current_price_kopecks),'hold_orders_to',v.hold_orders_to,
 'next_step_at',public.wb_state_timestamp(v.next_step_at),'requires_negative_margin_confirm',v.requires_negative_margin_confirm,
 'resulting_approval_id',v.resulting_approval_id,'state',v.state,'step_pct',public.wb_sku_override_decimal(v.step_pct),
 'target_price_kopecks',public.wb_sku_override_decimal(v.target_price_kopecks)))), 'UTF8') $$;
 """)


GUARDS = """
CREATE FUNCTION public.wb_state_lock() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE org text:=current_setting('app.organization_id',true); account text:=current_setting('app.marketplace_account_id',true);
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') OR (TG_OP='UPDATE' AND TG_TABLE_NAME NOT LIKE '%_heads') THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_immutable'; END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN
 RAISE EXCEPTION USING ERRCODE='25000',MESSAGE='wb_state_isolation_invalid'; END IF;
 IF org IS NULL OR org !~ '^[1-9][0-9]{0,9}$' THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_context_invalid'; END IF;
 IF org::numeric>2147483647 THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_context_invalid'; END IF;
 IF TG_ARGV[0]='audit' THEN RETURN NULL; END IF;
 IF TG_ARGV[0]='settings' THEN
 PERFORM 1 FROM public.lk_organizations WHERE organization_id=org::integer FOR UPDATE;
 ELSE
 IF account IS NULL OR account !~ '^[1-9][0-9]{0,9}$' THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_context_invalid'; END IF;
 IF account::numeric>2147483647 THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_context_invalid'; END IF;
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=org::integer AND marketplace_account_id=account::integer AND marketplace='wb' FOR UPDATE;
 END IF;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_owner_missing'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION public.wb_state_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE family text:=TG_ARGV[0]; n jsonb:=to_jsonb(NEW); old_row jsonb;
BEGIN
 IF family='audit' THEN family:=NEW.domain; END IF;
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR (family<>'settings' AND n->>'marketplace_account_id' IS DISTINCT FROM current_setting('app.marketplace_account_id',true)) THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_context_invalid'; END IF;
 IF TG_ARGV[0]='audit' THEN
 IF family='settings' THEN
 PERFORM 1 FROM public.lk_organizations WHERE organization_id=NEW.organization_id FOR UPDATE;
 ELSE
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND marketplace='wb' FOR UPDATE;
 END IF;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_owner_missing'; END IF;
 END IF;
 IF TG_TABLE_NAME LIKE '%_heads' THEN
 IF TG_OP='INSERT' THEN
 IF NEW.version IS DISTINCT FROM 1::numeric THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_conflict'; END IF;
 ELSE
 old_row:=to_jsonb(OLD);
 IF NEW.version IS DISTINCT FROM OLD.version+1 OR NEW.updated_at<OLD.updated_at
 OR n->'organization_id' IS DISTINCT FROM old_row->'organization_id'
 OR n->'marketplace_account_id' IS DISTINCT FROM old_row->'marketplace_account_id'
 OR n->'catalog_sku_id' IS DISTINCT FROM old_row->'catalog_sku_id'
 OR n->'campaign_id' IS DISTINCT FROM old_row->'campaign_id'
 OR n->'marketplace' IS DISTINCT FROM old_row->'marketplace' THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_conflict'; END IF;
 END IF;
 ELSIF TG_OP<>'INSERT' THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_immutable';
 END IF;
 IF coalesce((n->>'created_at')::timestamptz,(n->>'updated_at')::timestamptz,(n->>'occurred_at')::timestamptz)>clock_timestamp() THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_time_invalid'; END IF;
 RETURN NEW;
END $$;
"""


def witnesses():
    for family in FAMILIES:
        key = owner(family).split(",")
        scope = " AND ".join(f"t.{c}=NEW.{c}" for c in key)
        audit_scope = " AND ".join(f"e.{c}=NEW.{c}" for c in key) + f" AND e.domain='{family}'"
        extra = ""
        payload = f"public.wb_state_{family}_bytes(v)"
        event = "CASE WHEN v.parent_revision IS NULL THEN 'created' ELSE 'replaced' END"
        declarations = ""
        if family == "settings":
            declarations = f"children public.{PREFIX}basket_norm_defaults[];"
            extra = f"""
 SELECT array_agg(t ORDER BY t.garment COLLATE "C") INTO children FROM public.{PREFIX}basket_norm_defaults t
 WHERE t.organization_id=v.organization_id AND t.settings_revision=v.revision;
 IF v.basket_norm_mode='fallback_by_type' AND coalesce(cardinality(children),0)<>3 THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_children_invalid'; END IF;
 """
            payload = "public.wb_state_settings_bytes(v,children)"
        elif family == "assignment":
            event = "CASE WHEN v.strategy_id IS NULL THEN 'cleared' WHEN v.parent_revision IS NULL THEN 'created' ELSE 'replaced' END"
            extra = "IF v.assigned_at>v.created_at THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_time_invalid'; END IF;"
        else:
            declarations = f"c public.{PREFIX}liquidation_campaigns%ROWTYPE; previous public.{PREFIX}liquidation_versions%ROWTYPE;"
            extra = f"""
 SELECT * INTO c FROM public.{PREFIX}liquidation_campaigns t WHERE {scope};
 IF NOT FOUND OR c.created_at>v.created_at OR c.created_at>clock_timestamp()
 OR (v.confirmed_at IS NOT NULL AND (v.confirmed_at<c.created_at OR v.confirmed_at>v.created_at)) THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_time_invalid'; END IF;
 IF v.revision=1 THEN
 IF v.state<>'active' OR v.actor_membership_id<>c.started_by_membership_id THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_transition_invalid'; END IF;
 ELSE
 IF NOT ((previous.state='active' AND v.state IN ('active','paused','completed','cancelled'))
 OR (previous.state='paused' AND v.state IN ('active','completed','cancelled'))) THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_transition_invalid'; END IF;
 IF ROW(v.target_price_kopecks,v.step_pct) IS DISTINCT FROM ROW(previous.target_price_kopecks,previous.step_pct)
 AND v.requires_negative_margin_confirm AND v.confirmed_by_membership_id IS NOT NULL THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_confirmation_invalid'; END IF;
 END IF;
 IF v.resulting_approval_row_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM public.wb_repricer_price_approvals p WHERE p.organization_id=v.organization_id
 AND p.marketplace_account_id=v.marketplace_account_id AND p.approval_row_id=v.resulting_approval_row_id
 AND p.catalog_sku_id=v.catalog_sku_id AND p.approval_id=v.resulting_approval_id) THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_approval_invalid'; END IF;
 """
            event = "CASE WHEN v.parent_revision IS NULL THEN 'created' WHEN v.state='active' AND previous.state='active' THEN 'replaced' WHEN v.state='active' THEN 'resumed' WHEN v.state='paused' THEN 'paused' ELSE v.state END"
            payload = "public.wb_state_liquidation_bytes(v,c)"
        head_projection = ""
        captured_projection = ""
        if family == "liquidation":
            head_projection = "IF ROW(" + ",".join("h." + c for c in LIQUIDATION_FIELDS) + ") IS DISTINCT FROM ROW(" + ",".join("previous." + c for c in LIQUIDATION_FIELDS) + ") THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_projection_invalid'; END IF;"
            captured_projection = "IF ROW(" + ",".join("NEW." + c for c in LIQUIDATION_FIELDS) + ") IS DISTINCT FROM ROW(" + ",".join("v." + c for c in LIQUIDATION_FIELDS) + ") THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_projection_invalid'; END IF;"
        op.execute(f"""
 CREATE FUNCTION public.wb_state_{family}_witness() RETURNS trigger
 LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 DECLARE h public.{PREFIX}{family}_heads%ROWTYPE; v public.{PREFIX}{family}_versions%ROWTYPE;
 a public.{PREFIX}state_audit%ROWTYPE; previous_revision numeric:=NULL; previous_time timestamptz:=NULL;
 captured jsonb:=to_jsonb(NEW); {declarations}
 BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 {"OR captured->>'marketplace_account_id' IS DISTINCT FROM current_setting('app.marketplace_account_id',true)" if family != 'settings' else ''} THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_context_invalid'; END IF;
 SELECT * INTO h FROM public.{PREFIX}{family}_heads t WHERE {scope};
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_graph_invalid'; END IF;
 IF TG_TABLE_NAME='{PREFIX}{family}_heads' THEN
 SELECT * INTO v FROM public.{PREFIX}{family}_versions t WHERE {scope} AND t.revision=NEW.current_revision;
 IF NOT FOUND OR v.created_at IS DISTINCT FROM NEW.updated_at OR v.revision IS DISTINCT FROM NEW.version THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_graph_invalid'; END IF;
 {captured_projection}
 IF TG_OP='UPDATE' THEN
 IF v.parent_revision IS DISTINCT FROM OLD.version THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_conflict'; END IF;
 ELSE
 IF v.parent_revision IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_conflict'; END IF;
 END IF;
 END IF;
 IF (SELECT count(*)::numeric FROM public.{PREFIX}{family}_versions t WHERE {scope})<>h.version
 OR (SELECT count(*)::numeric FROM public.{PREFIX}state_audit e WHERE {audit_scope})<>h.version THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_graph_invalid'; END IF;
 FOR v IN SELECT * FROM public.{PREFIX}{family}_versions t WHERE {scope} ORDER BY t.revision LOOP
 SELECT * INTO a FROM public.{PREFIX}state_audit e WHERE {audit_scope} AND e.audit_id=v.audit_id;
 IF NOT FOUND OR v.revision<>coalesce(previous_revision,0)+1 OR v.parent_revision IS DISTINCT FROM previous_revision
 OR a.{family}_before IS DISTINCT FROM v.parent_revision OR a.{family}_after IS DISTINCT FROM v.revision
 OR a.command_id IS DISTINCT FROM v.command_id OR a.actor_membership_id IS DISTINCT FROM v.actor_membership_id
 OR a.occurred_at IS DISTINCT FROM v.created_at OR v.created_at>clock_timestamp()
 OR (previous_time IS NOT NULL AND v.created_at<previous_time) THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_graph_invalid'; END IF;
 {extra}
 IF a.event_kind IS DISTINCT FROM ({event}) THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_audit_invalid'; END IF;
 IF v.request_payload IS DISTINCT FROM {payload} THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_payload_invalid'; END IF;
 previous_revision:=v.revision; previous_time:=v.created_at;
 {'previous:=v;' if family == 'liquidation' else ''}
 END LOOP;
 IF previous_revision IS DISTINCT FROM h.current_revision OR previous_time IS DISTINCT FROM h.updated_at THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_graph_invalid'; END IF;
 {head_projection}
 RETURN NULL;
 END $$;
 """)


def acl():
    # Clear every nonowner direct/default ACL on these exact new objects. This
    # also closes inherited paths through group roles carrying default grants.
    tables = ",".join("'" + PREFIX + t + "'" for t in TABLES)
    functions = PRIMITIVES + CODECS + ("wb_state_lock()", "wb_state_guard()") + tuple(f"wb_state_{f}_witness()" for f in FAMILIES)
    signatures = ",".join(f"'public.{f}'::regprocedure" for f in functions)
    op.execute(f"""
 DO $$ DECLARE t record; a record; c record; f record; who text;
 BEGIN
 FOR t IN SELECT oid,relowner,relname,relacl FROM pg_class WHERE relnamespace='public'::regnamespace AND relname IN ({tables}) LOOP
 FOR a IN SELECT DISTINCT grantee FROM aclexplode(t.relacl) WHERE grantee<>t.relowner LOOP
 who:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
 EXECUTE format('REVOKE ALL ON public.%I FROM %s',t.relname,who);
 END LOOP;
 FOR c IN SELECT at.attname,x.grantee FROM pg_attribute at CROSS JOIN LATERAL aclexplode(at.attacl) x
 WHERE at.attrelid=t.oid AND x.grantee<>t.relowner LOOP
 who:=CASE WHEN c.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(c.grantee)) END;
 EXECUTE format('REVOKE ALL (%I) ON public.%I FROM %s',c.attname,t.relname,who);
 END LOOP;
 EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',t.relname);
 END LOOP;
 FOR f IN SELECT oid,proowner,proacl,oid::regprocedure signature FROM pg_proc WHERE oid IN ({signatures}) LOOP
 FOR a IN SELECT DISTINCT grantee FROM aclexplode(f.proacl) WHERE grantee<>f.proowner LOOP
 who:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
 EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,who);
 END LOOP;
 EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
 END $$;
 """)


def upgrade():
    op.execute(HELPERS)
    models()
    codecs()
    op.execute(GUARDS)
    witnesses()
    for table in TABLES:
        family = ("audit" if table == "state_audit" else "settings" if table == "basket_norm_defaults" else table.split("_")[0])
        predicate = 'organization_id::text COLLATE "C"=current_setting(\'app.organization_id\',true) COLLATE "C"'
        if family not in ("settings", "audit"):
            predicate += ' AND marketplace_account_id::text COLLATE "C"=current_setting(\'app.marketplace_account_id\',true) COLLATE "C"'
        elif family == "audit":
            predicate += ' AND (domain=\'settings\' OR marketplace_account_id::text COLLATE "C"=current_setting(\'app.marketplace_account_id\',true) COLLATE "C")'
        op.execute(f"""
 ALTER TABLE public.{PREFIX}{table} ENABLE ROW LEVEL SECURITY;
 ALTER TABLE public.{PREFIX}{table} FORCE ROW LEVEL SECURITY;
 CREATE POLICY wb_state_scope ON public.{PREFIX}{table} USING ({predicate}) WITH CHECK ({predicate});
 CREATE TRIGGER wb_state_first BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.{PREFIX}{table}
 FOR EACH STATEMENT EXECUTE FUNCTION public.wb_state_lock('{family}');
 CREATE TRIGGER wb_state_row BEFORE INSERT OR UPDATE ON public.{PREFIX}{table}
 FOR EACH ROW EXECUTE FUNCTION public.wb_state_guard('{family}');
 """)
        for target in FAMILIES if family == "audit" else (family,):
            condition = f"WHEN (NEW.domain='{target}')" if family == "audit" else ""
            op.execute(f"CREATE CONSTRAINT TRIGGER wb_state_{target}_graph AFTER INSERT OR UPDATE ON public.{PREFIX}{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW {condition} EXECUTE FUNCTION public.wb_state_{target}_witness()")
    acl()


def downgrade():
    op.execute("LOCK TABLE " + ",".join("public." + PREFIX + t for t in TABLES) + " IN ACCESS EXCLUSIVE MODE")
    # FORCE RLS plus row_security=off fails rather than hiding rows when the
    # maintenance caller lacks actual BYPASSRLS/superuser authority.
    op.execute("SET LOCAL row_security=off")
    for table in TABLES:
        op.execute(f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.{PREFIX}{table}) THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='wb_state_downgrade_nonempty'; END IF; END $$")
    for table in TABLES:
        family = ("audit" if table == "state_audit" else "settings" if table == "basket_norm_defaults" else table.split("_")[0])
        for target in FAMILIES if family == "audit" else (family,):
            op.execute(f"DROP TRIGGER wb_state_{target}_graph ON public.{PREFIX}{table}")
        op.execute(f"DROP TRIGGER wb_state_row ON public.{PREFIX}{table}; DROP TRIGGER wb_state_first ON public.{PREFIX}{table}")
    for function in tuple(f"wb_state_{f}_witness()" for f in FAMILIES) + ("wb_state_guard()", "wb_state_lock()") + CODECS:
        op.execute("DROP FUNCTION public." + function)
    for family in FAMILIES:
        op.execute(f"ALTER TABLE public.{PREFIX}{family}_versions DROP CONSTRAINT wb_state_{family}_head_fk, DROP CONSTRAINT wb_state_{family}_audit_fk")
    op.execute(f"ALTER TABLE public.{PREFIX}liquidation_versions DROP CONSTRAINT wb_state_campaign_fk")
    for table in ("state_audit", "basket_norm_defaults", "liquidation_campaigns", "liquidation_heads", "liquidation_versions", "assignment_heads", "assignment_versions", "settings_heads", "settings_versions"):
        op.execute("DROP TABLE public." + PREFIX + table)
    for function in reversed(PRIMITIVES):
        op.execute("DROP FUNCTION public." + function)
