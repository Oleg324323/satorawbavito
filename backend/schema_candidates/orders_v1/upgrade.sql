-- Unregistered candidate. T1 owns revision allocation and runtime enablement.
-- Execute in ONE owner transaction on PostgreSQL 15+. No BEGIN/COMMIT here.
-- Normalized evidence only: no raw provider payloads, secrets or buyer PII.
CREATE FUNCTION orders_exact_text(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
  SELECT value <> '' AND value = btrim(value, U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000');
$$;
CREATE FUNCTION orders_reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  RAISE EXCEPTION 'Orders immutable history cannot be changed';
END $$;
ALTER TABLE marketplace_products ADD CONSTRAINT uq_orders_product_account
  UNIQUE (organization_id, marketplace_account_id, marketplace_product_id);
ALTER TABLE marketplace_offers ADD CONSTRAINT fk_orders_offer_product_account
  FOREIGN KEY (organization_id, marketplace_account_id, marketplace_product_id)
  REFERENCES marketplace_products (organization_id, marketplace_account_id, marketplace_product_id);
ALTER TABLE marketplace_offers ADD CONSTRAINT uq_orders_offer_product_account
  UNIQUE (organization_id, marketplace_account_id, marketplace_product_id, marketplace_offer_id);

CREATE TABLE order_sync_runs (
sync_run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

marketplace VARCHAR(16) NOT NULL CHECK (marketplace IN ('wb','avito')),
source_kind TEXT NOT NULL CHECK (orders_exact_text(source_kind)), source_run_key TEXT NOT NULL CHECK (orders_exact_text(source_run_key)), adapter_version TEXT NOT NULL CHECK (orders_exact_text(adapter_version)),
mapping_version TEXT NOT NULL CHECK (orders_exact_text(mapping_version)), source_contract_version TEXT NOT NULL CHECK (orders_exact_text(source_contract_version)), source_snapshot TEXT NOT NULL CHECK (orders_exact_text(source_snapshot)),
state VARCHAR(24) NOT NULL DEFAULT 'staging' CHECK (state IN ('staging','complete','partial','failed')),
manifest_state VARCHAR(16) NOT NULL DEFAULT 'partial' CHECK (manifest_state IN ('partial','complete')),
requested_from TIMESTAMPTZ, requested_to TIMESTAMPTZ,
source_high_water_mark TEXT, page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
order_count INTEGER NOT NULL DEFAULT 0 CHECK (order_count >= 0),
expected_order_count INTEGER CHECK (expected_order_count >= 0),
payload_checksum TEXT CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMPTZ,
error_code TEXT CHECK (orders_exact_text(error_code)),
FOREIGN KEY (organization_id, marketplace_account_id, marketplace) REFERENCES marketplace_accounts (organization_id, marketplace_account_id, marketplace),
UNIQUE (organization_id, marketplace_account_id, source_kind, source_run_key),
UNIQUE (organization_id, marketplace_account_id, sync_run_id, source_kind, adapter_version),
UNIQUE (organization_id, marketplace_account_id, marketplace, source_kind, sync_run_id),
CHECK ((marketplace='wb' AND source_kind='wb-statistics-supplier-orders') OR
       (marketplace='avito' AND source_kind IN ('avito-order-management','avito-browser'))),
CHECK ((requested_from IS NULL AND requested_to IS NULL) OR (requested_from IS NOT NULL AND requested_to IS NOT NULL AND requested_to > requested_from)),
CHECK (completed_at IS NULL OR completed_at >= started_at),
CHECK ((state='staging' AND completed_at IS NULL) OR (state<>'staging' AND completed_at IS NOT NULL)),
CHECK (state<>'complete' OR (manifest_state='complete' AND payload_checksum IS NOT NULL
       AND page_count>0 AND expected_order_count IS NOT NULL AND expected_order_count=order_count)),
CHECK (manifest_state<>'complete' OR state='complete'),
UNIQUE (organization_id, marketplace_account_id, sync_run_id)
);

CREATE FUNCTION orders_guard_run() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF OLD.state <> 'staging' OR NEW.sync_run_id <> OLD.sync_run_id
     OR (NEW.organization_id, NEW.marketplace_account_id, NEW.marketplace, NEW.source_kind,
         NEW.source_run_key, NEW.adapter_version, NEW.mapping_version,
         NEW.source_contract_version, NEW.source_snapshot, NEW.started_at)
        IS DISTINCT FROM
        (OLD.organization_id, OLD.marketplace_account_id, OLD.marketplace, OLD.source_kind,
         OLD.source_run_key, OLD.adapter_version, OLD.mapping_version,
         OLD.source_contract_version, OLD.source_snapshot, OLD.started_at) THEN
    RAISE EXCEPTION 'Orders immutable terminal run or run identity';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER orders_run_guard BEFORE UPDATE ON order_sync_runs
FOR EACH ROW EXECUTE FUNCTION orders_guard_run();
CREATE TRIGGER orders_run_no_delete BEFORE DELETE ON order_sync_runs
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_run_no_truncate BEFORE TRUNCATE ON order_sync_runs
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

CREATE TABLE marketplace_orders (
order_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

marketplace VARCHAR(16) NOT NULL CHECK (marketplace IN ('wb','avito')),
external_order_id TEXT NOT NULL CHECK (orders_exact_text(external_order_id)), display_order_id TEXT,
source_created_at TIMESTAMPTZ, source_updated_at TIMESTAMPTZ,
raw_status TEXT CHECK (orders_exact_text(raw_status)),
canonical_status VARCHAR(40) CHECK (canonical_status IN ('pending_confirmation','accepted','ready_for_fulfillment','in_delivery','delivered','closed','cancelled','returning','returned','disputed')),
mapping_state VARCHAR(16) NOT NULL DEFAULT 'unmapped' CHECK (mapping_state IN ('mapped','unmapped','ambiguous')),
CHECK ((mapping_state = 'mapped') = (canonical_status IS NOT NULL)), mapping_version TEXT  CHECK (orders_exact_text(mapping_version)),
version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1), last_seen_sync_run_id BIGINT,
created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
FOREIGN KEY (organization_id, marketplace_account_id, marketplace) REFERENCES marketplace_accounts (organization_id, marketplace_account_id, marketplace), FOREIGN KEY (organization_id, marketplace_account_id, last_seen_sync_run_id) REFERENCES order_sync_runs (organization_id, marketplace_account_id, sync_run_id),
UNIQUE (organization_id,marketplace_account_id,external_order_id),
UNIQUE (organization_id,marketplace_account_id,marketplace,order_id),
CHECK (canonical_status IS NULL OR mapping_version IS NOT NULL),
UNIQUE (organization_id, marketplace_account_id, order_id)
);

CREATE TABLE marketplace_order_items (
order_item_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

order_id BIGINT NOT NULL, source_line_key TEXT NOT NULL CHECK (orders_exact_text(source_line_key)),
external_item_id TEXT  CHECK (orders_exact_text(external_item_id)), external_listing_id TEXT  CHECK (orders_exact_text(external_listing_id)),
occurrence_index INTEGER NOT NULL DEFAULT 0 CHECK (occurrence_index>=0),
quantity INTEGER NOT NULL CHECK (quantity>0),
marketplace_product_id INTEGER, marketplace_offer_id INTEGER, catalog_sku_id INTEGER,
resolution_state VARCHAR(32) NOT NULL DEFAULT 'unmapped' CHECK (resolution_state IN ('resolved','unmapped','ambiguous','stale','manual_override')),
resolution_version TEXT NOT NULL CHECK (orders_exact_text(resolution_version)), version BIGINT NOT NULL DEFAULT 1 CHECK (version>=1),
source_created_at TIMESTAMPTZ, source_updated_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
FOREIGN KEY (organization_id, marketplace_account_id, order_id) REFERENCES marketplace_orders (organization_id, marketplace_account_id, order_id),
FOREIGN KEY (organization_id, marketplace_account_id, marketplace_product_id) REFERENCES marketplace_products (organization_id, marketplace_account_id, marketplace_product_id),
FOREIGN KEY (organization_id, marketplace_account_id, marketplace_product_id, marketplace_offer_id) REFERENCES marketplace_offers (organization_id, marketplace_account_id, marketplace_product_id, marketplace_offer_id),
FOREIGN KEY (organization_id,catalog_sku_id) REFERENCES catalog_skus(organization_id,catalog_sku_id),
UNIQUE (organization_id,marketplace_account_id,order_id,source_line_key),
UNIQUE (organization_id,marketplace_account_id,order_id,order_item_id),
CHECK (marketplace_offer_id IS NULL OR marketplace_product_id IS NOT NULL),
CHECK ((resolution_state IN ('resolved','manual_override')) = (catalog_sku_id IS NOT NULL)),
CHECK (resolution_state<>'resolved' OR marketplace_product_id IS NOT NULL),
UNIQUE (organization_id, marketplace_account_id, order_item_id)
);

CREATE FUNCTION orders_guard_order_id() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF (NEW.order_id, NEW.organization_id, NEW.marketplace_account_id, NEW.marketplace, NEW.external_order_id) IS DISTINCT FROM (OLD.order_id, OLD.organization_id, OLD.marketplace_account_id, OLD.marketplace, OLD.external_order_id) THEN
    RAISE EXCEPTION 'Orders immutable identity';
  END IF;
  IF NEW.version <> OLD.version+1 THEN RAISE EXCEPTION 'Orders projection version must increment by one'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER orders_identity_guard BEFORE UPDATE ON marketplace_orders
FOR EACH ROW EXECUTE FUNCTION orders_guard_order_id();
CREATE TRIGGER orders_no_delete BEFORE DELETE ON marketplace_orders
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_no_truncate BEFORE TRUNCATE ON marketplace_orders
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

CREATE FUNCTION orders_guard_order_item_id() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF (NEW.order_item_id, NEW.organization_id, NEW.marketplace_account_id, NEW.order_id, NEW.source_line_key, NEW.external_item_id, NEW.occurrence_index) IS DISTINCT FROM (OLD.order_item_id, OLD.organization_id, OLD.marketplace_account_id, OLD.order_id, OLD.source_line_key, OLD.external_item_id, OLD.occurrence_index) THEN
    RAISE EXCEPTION 'Orders immutable identity';
  END IF;
  IF NEW.version <> OLD.version+1 THEN RAISE EXCEPTION 'Orders projection version must increment by one'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER orders_identity_guard BEFORE UPDATE ON marketplace_order_items
FOR EACH ROW EXECUTE FUNCTION orders_guard_order_item_id();
CREATE TRIGGER orders_no_delete BEFORE DELETE ON marketplace_order_items
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_no_truncate BEFORE TRUNCATE ON marketplace_order_items
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

CREATE TABLE order_observations (
observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

sync_run_id BIGINT NOT NULL, order_id BIGINT NOT NULL, order_item_id BIGINT,
source_kind TEXT NOT NULL CHECK (orders_exact_text(source_kind)), adapter_version TEXT NOT NULL CHECK (orders_exact_text(adapter_version)), source_event_id TEXT  CHECK (orders_exact_text(source_event_id)),
source_revision TEXT  CHECK (orders_exact_text(source_revision)),
payload_checksum TEXT NOT NULL CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
evidence_schema_version INTEGER NOT NULL DEFAULT 1 CHECK (evidence_schema_version=1),
normalized_evidence JSONB NOT NULL CHECK (jsonb_typeof(normalized_evidence)='object'),
source_effective_at TIMESTAMPTZ, observed_at TIMESTAMPTZ NOT NULL,
ingested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
FOREIGN KEY (organization_id, marketplace_account_id, sync_run_id, source_kind, adapter_version) REFERENCES order_sync_runs (organization_id, marketplace_account_id, sync_run_id, source_kind, adapter_version), FOREIGN KEY (organization_id, marketplace_account_id, order_id) REFERENCES marketplace_orders (organization_id, marketplace_account_id, order_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id) REFERENCES marketplace_order_items (organization_id, marketplace_account_id, order_id,order_item_id),
UNIQUE (organization_id,marketplace_account_id,order_id,observation_id),
UNIQUE (organization_id,marketplace_account_id,order_id,order_item_id,observation_id),
UNIQUE (organization_id, marketplace_account_id, observation_id)
);

CREATE UNIQUE INDEX uq_orders_observation_order_replay ON order_observations
(organization_id,marketplace_account_id,order_id,source_kind,adapter_version,payload_checksum)
WHERE order_item_id IS NULL;

CREATE UNIQUE INDEX uq_orders_observation_item_replay ON order_observations
(organization_id,marketplace_account_id,order_id,order_item_id,source_kind,adapter_version,payload_checksum)
WHERE order_item_id IS NOT NULL;

CREATE TABLE order_status_observations (
status_observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

order_id BIGINT NOT NULL, order_item_id BIGINT, observation_id BIGINT NOT NULL,
FOREIGN KEY (organization_id, marketplace_account_id, order_id) REFERENCES marketplace_orders (organization_id, marketplace_account_id, order_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id) REFERENCES marketplace_order_items (organization_id, marketplace_account_id, order_id,order_item_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,observation_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,order_item_id,observation_id),
source_effective_at TIMESTAMPTZ, observed_at TIMESTAMPTZ NOT NULL, raw_status TEXT CHECK (orders_exact_text(raw_status)),
canonical_status VARCHAR(40) CHECK (canonical_status IN ('pending_confirmation','accepted','ready_for_fulfillment','in_delivery','delivered','closed','cancelled','returning','returned','disputed')),
mapping_state VARCHAR(16) NOT NULL DEFAULT 'unmapped' CHECK (mapping_state IN ('mapped','unmapped','ambiguous')),
CHECK ((mapping_state = 'mapped') = (canonical_status IS NOT NULL)), mapping_version TEXT NOT NULL CHECK (orders_exact_text(mapping_version)), evidence_source TEXT NOT NULL CHECK (orders_exact_text(evidence_source)),
UNIQUE (organization_id,marketplace_account_id,observation_id),
UNIQUE (organization_id, marketplace_account_id, status_observation_id)
);

CREATE TABLE order_lifecycle_events (
lifecycle_event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

order_id BIGINT NOT NULL, order_item_id BIGINT, observation_id BIGINT NOT NULL,
FOREIGN KEY (organization_id, marketplace_account_id, order_id) REFERENCES marketplace_orders (organization_id, marketplace_account_id, order_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id) REFERENCES marketplace_order_items (organization_id, marketplace_account_id, order_id,order_item_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,observation_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,order_item_id,observation_id),
source_effective_at TIMESTAMPTZ, observed_at TIMESTAMPTZ NOT NULL, source_event_key TEXT NOT NULL CHECK (orders_exact_text(source_event_key)),
event_kind VARCHAR(32) NOT NULL CHECK (event_kind IN ('cancellation','return','partial_return','status_changed','reconciliation_required')),
evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
UNIQUE (organization_id,marketplace_account_id,order_id,observation_id,source_event_key),
UNIQUE (organization_id, marketplace_account_id, lifecycle_event_id)
);

CREATE TABLE order_deadlines (
deadline_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

order_id BIGINT NOT NULL, order_item_id BIGINT, observation_id BIGINT NOT NULL,
FOREIGN KEY (organization_id, marketplace_account_id, order_id) REFERENCES marketplace_orders (organization_id, marketplace_account_id, order_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id) REFERENCES marketplace_order_items (organization_id, marketplace_account_id, order_id,order_item_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,observation_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,order_item_id,observation_id),
source_effective_at TIMESTAMPTZ, observed_at TIMESTAMPTZ NOT NULL, deadline_kind TEXT NOT NULL CHECK (orders_exact_text(deadline_kind)),
source_deadline_at TIMESTAMPTZ, computed_deadline_at TIMESTAMPTZ,
rule_id TEXT  CHECK (orders_exact_text(rule_id)), rule_version TEXT  CHECK (orders_exact_text(rule_version)), timezone TEXT NOT NULL CHECK (orders_exact_text(timezone)), evidence_source TEXT NOT NULL CHECK (orders_exact_text(evidence_source)),
CHECK (source_deadline_at IS NOT NULL OR computed_deadline_at IS NOT NULL),
CHECK ((computed_deadline_at IS NOT NULL AND rule_id IS NOT NULL AND rule_version IS NOT NULL)
 OR (computed_deadline_at IS NULL AND rule_id IS NULL AND rule_version IS NULL)),
UNIQUE (organization_id,marketplace_account_id,observation_id,deadline_kind),
UNIQUE (organization_id, marketplace_account_id, deadline_id)
);

CREATE TABLE order_sync_coverage (
coverage_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

sync_run_id BIGINT NOT NULL, coverage_kind TEXT NOT NULL CHECK (orders_exact_text(coverage_kind)),
requested_from TIMESTAMPTZ,requested_to TIMESTAMPTZ,
first_cursor TEXT,last_cursor TEXT,first_page INTEGER,last_page INTEGER,
is_complete BOOLEAN NOT NULL,
manifest_checksum TEXT CHECK (manifest_checksum ~ '^[0-9a-f]{64}$'), completed_at TIMESTAMPTZ,
FOREIGN KEY (organization_id, marketplace_account_id, sync_run_id) REFERENCES order_sync_runs (organization_id, marketplace_account_id, sync_run_id),
UNIQUE (organization_id,marketplace_account_id,sync_run_id,coverage_kind),
CHECK (NOT is_complete OR (manifest_checksum IS NOT NULL AND completed_at IS NOT NULL)),
CHECK ((requested_from IS NULL AND requested_to IS NULL) OR (requested_from IS NOT NULL AND requested_to IS NOT NULL AND requested_to > requested_from)),
CHECK ((first_page IS NULL AND last_page IS NULL) OR
       (first_page IS NOT NULL AND last_page IS NOT NULL AND first_page>0 AND last_page>=first_page)),
UNIQUE (organization_id, marketplace_account_id, coverage_id)
);

CREATE TABLE order_sync_memberships (
membership_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

sync_run_id BIGINT NOT NULL, order_id BIGINT NOT NULL, order_item_id BIGINT,
observation_id BIGINT NOT NULL, coverage_role VARCHAR(24) NOT NULL CHECK (coverage_role IN ('observed','quarantined')),
observed_at TIMESTAMPTZ NOT NULL,
FOREIGN KEY (organization_id, marketplace_account_id, sync_run_id) REFERENCES order_sync_runs (organization_id, marketplace_account_id, sync_run_id), FOREIGN KEY (organization_id, marketplace_account_id, order_id) REFERENCES marketplace_orders (organization_id, marketplace_account_id, order_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id) REFERENCES marketplace_order_items (organization_id, marketplace_account_id, order_id,order_item_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,observation_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,order_item_id,observation_id),
UNIQUE (organization_id, marketplace_account_id, membership_id)
);

CREATE UNIQUE INDEX uq_orders_membership_order ON order_sync_memberships
(organization_id,marketplace_account_id,sync_run_id,order_id) WHERE order_item_id IS NULL;

CREATE UNIQUE INDEX uq_orders_membership_item ON order_sync_memberships
(organization_id,marketplace_account_id,sync_run_id,order_id,order_item_id) WHERE order_item_id IS NOT NULL;

CREATE TABLE order_read_snapshots (
snapshot_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),

high_water_mark TEXT NOT NULL CHECK (orders_exact_text(high_water_mark)),
account_scope INTEGER[] NOT NULL CHECK (cardinality(account_scope)>0 AND array_ndims(account_scope)=1 AND array_position(account_scope,NULL) IS NULL AND 0<ALL(account_scope)),
query_checksum TEXT NOT NULL CHECK (query_checksum ~ '^[0-9a-f]{64}$'),
account_coverage JSONB NOT NULL CHECK (jsonb_typeof(account_coverage)='array'),
coverage_state VARCHAR(16) NOT NULL CHECK (coverage_state IN ('complete','partial','missing')),
row_count INTEGER NOT NULL CHECK (row_count>=0),
published_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
expires_at TIMESTAMPTZ, CHECK (expires_at IS NULL OR expires_at>published_at),
UNIQUE (organization_id, snapshot_id)
);

CREATE TABLE order_read_snapshot_rows (
snapshot_row_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
marketplace_account_id INTEGER NOT NULL,

snapshot_id BIGINT NOT NULL, order_id BIGINT NOT NULL, order_item_id BIGINT NOT NULL,
observation_id BIGINT NOT NULL, position BIGINT NOT NULL CHECK (position>=1),
row_version BIGINT NOT NULL CHECK (row_version>=1),
payload_schema_version INTEGER NOT NULL CHECK (payload_schema_version=1),
row_payload JSONB NOT NULL CHECK (jsonb_typeof(row_payload)='object'),
FOREIGN KEY (organization_id,snapshot_id) REFERENCES order_read_snapshots(organization_id,snapshot_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,order_item_id) REFERENCES marketplace_order_items (organization_id, marketplace_account_id, order_id,order_item_id),
FOREIGN KEY (organization_id, marketplace_account_id, order_id,observation_id) REFERENCES order_observations (organization_id, marketplace_account_id, order_id,observation_id),
UNIQUE (organization_id,snapshot_id,position),
UNIQUE (organization_id,snapshot_id,marketplace_account_id,order_item_id),
UNIQUE (organization_id, marketplace_account_id, snapshot_row_id)
);

ALTER TABLE order_sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_sync_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_sync_runs ON order_sync_runs
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_sync_runs FROM PUBLIC;

ALTER TABLE marketplace_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace_orders FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_marketplace_orders ON marketplace_orders
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON marketplace_orders FROM PUBLIC;

ALTER TABLE marketplace_order_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace_order_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_marketplace_order_items ON marketplace_order_items
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON marketplace_order_items FROM PUBLIC;

ALTER TABLE order_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_observations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_observations ON order_observations
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_observations FROM PUBLIC;

CREATE TRIGGER orders_immutable BEFORE UPDATE OR DELETE ON order_observations
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_immutable_truncate BEFORE TRUNCATE ON order_observations
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

ALTER TABLE order_status_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_status_observations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_status_observations ON order_status_observations
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_status_observations FROM PUBLIC;

CREATE TRIGGER orders_immutable BEFORE UPDATE OR DELETE ON order_status_observations
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_immutable_truncate BEFORE TRUNCATE ON order_status_observations
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

ALTER TABLE order_lifecycle_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_lifecycle_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_lifecycle_events ON order_lifecycle_events
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_lifecycle_events FROM PUBLIC;

CREATE TRIGGER orders_immutable BEFORE UPDATE OR DELETE ON order_lifecycle_events
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_immutable_truncate BEFORE TRUNCATE ON order_lifecycle_events
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

ALTER TABLE order_deadlines ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_deadlines FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_deadlines ON order_deadlines
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_deadlines FROM PUBLIC;

CREATE TRIGGER orders_immutable BEFORE UPDATE OR DELETE ON order_deadlines
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_immutable_truncate BEFORE TRUNCATE ON order_deadlines
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

ALTER TABLE order_sync_coverage ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_sync_coverage FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_sync_coverage ON order_sync_coverage
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_sync_coverage FROM PUBLIC;

CREATE TRIGGER orders_immutable BEFORE UPDATE OR DELETE ON order_sync_coverage
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_immutable_truncate BEFORE TRUNCATE ON order_sync_coverage
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

ALTER TABLE order_sync_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_sync_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_sync_memberships ON order_sync_memberships
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_sync_memberships FROM PUBLIC;

CREATE TRIGGER orders_immutable BEFORE UPDATE OR DELETE ON order_sync_memberships
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_immutable_truncate BEFORE TRUNCATE ON order_sync_memberships
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

ALTER TABLE order_read_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_read_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_read_snapshots ON order_read_snapshots
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_read_snapshots FROM PUBLIC;

CREATE TRIGGER orders_immutable BEFORE UPDATE OR DELETE ON order_read_snapshots
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_immutable_truncate BEFORE TRUNCATE ON order_read_snapshots
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

ALTER TABLE order_read_snapshot_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_read_snapshot_rows FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_order_read_snapshot_rows ON order_read_snapshot_rows
USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer)
WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::integer);
REVOKE ALL ON order_read_snapshot_rows FROM PUBLIC;

CREATE TRIGGER orders_immutable BEFORE UPDATE OR DELETE ON order_read_snapshot_rows
FOR EACH ROW EXECUTE FUNCTION orders_reject_history_mutation();
CREATE TRIGGER orders_immutable_truncate BEFORE TRUNCATE ON order_read_snapshot_rows
FOR EACH STATEMENT EXECUTE FUNCTION orders_reject_history_mutation();

CREATE INDEX ix_orders_run_account ON order_sync_runs(organization_id,marketplace_account_id,started_at DESC);
CREATE INDEX ix_orders_run_incomplete ON order_sync_runs(organization_id,marketplace_account_id,state) WHERE state='staging';
CREATE INDEX ix_orders_queue ON marketplace_orders(organization_id,marketplace_account_id,canonical_status,updated_at,order_id);
CREATE INDEX ix_orders_item_resolution ON marketplace_order_items(organization_id,marketplace_account_id,resolution_state,order_item_id);
CREATE INDEX ix_orders_observation_time ON order_observations(organization_id,marketplace_account_id,order_id,observed_at,observation_id);
CREATE INDEX ix_orders_status_time ON order_status_observations(organization_id,marketplace_account_id,order_id,source_effective_at,status_observation_id);
CREATE INDEX ix_orders_lifecycle_time ON order_lifecycle_events(organization_id,marketplace_account_id,order_id,source_effective_at,lifecycle_event_id);
CREATE INDEX ix_orders_deadline_queue ON order_deadlines(organization_id,marketplace_account_id,computed_deadline_at,source_deadline_at,deadline_id);

-- Validate final immutable membership at commit, allowing header + rows in one transaction.
CREATE FUNCTION orders_check_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE header order_read_snapshots%ROWTYPE; actual_count bigint; last_position bigint;
BEGIN
  SELECT * INTO STRICT header FROM order_read_snapshots
    WHERE organization_id=NEW.organization_id AND snapshot_id=NEW.snapshot_id;
  SELECT count(*),max(position) INTO actual_count,last_position FROM order_read_snapshot_rows
    WHERE organization_id=NEW.organization_id AND snapshot_id=NEW.snapshot_id;
  IF actual_count <> header.row_count OR coalesce(last_position,0) <> header.row_count THEN
    RAISE EXCEPTION 'Orders snapshot row count/positions inconsistent';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER orders_snapshot_complete AFTER INSERT ON order_read_snapshots
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION orders_check_snapshot();
CREATE CONSTRAINT TRIGGER orders_snapshot_rows_complete AFTER INSERT ON order_read_snapshot_rows
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION orders_check_snapshot();
CREATE FUNCTION orders_check_snapshot_row() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE allowed_accounts integer[]; observed_item bigint;
BEGIN
  SELECT account_scope INTO STRICT allowed_accounts FROM order_read_snapshots
    WHERE organization_id=NEW.organization_id AND snapshot_id=NEW.snapshot_id;
  IF NOT NEW.marketplace_account_id = ANY(allowed_accounts) THEN
    RAISE EXCEPTION 'Orders snapshot account outside scope';
  END IF;
  SELECT order_item_id INTO STRICT observed_item FROM order_observations
    WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
      AND order_id=NEW.order_id AND observation_id=NEW.observation_id;
  IF observed_item IS NOT NULL AND observed_item <> NEW.order_item_id THEN
    RAISE EXCEPTION 'Orders snapshot observation belongs to another item';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER orders_snapshot_row_scope BEFORE INSERT ON order_read_snapshot_rows
FOR EACH ROW EXECUTE FUNCTION orders_check_snapshot_row();
