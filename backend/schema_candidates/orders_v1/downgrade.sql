-- Owner transaction only; never CASCADE. Lock then check ALL tables before dropping any.
SET LOCAL row_security = off;
LOCK TABLE order_sync_runs, marketplace_orders, marketplace_order_items, order_observations, order_status_observations, order_lifecycle_events, order_deadlines, order_sync_coverage, order_sync_memberships, order_read_snapshots, order_read_snapshot_rows IN ACCESS EXCLUSIVE MODE;
DO $$ DECLARE relation text; occupied boolean; BEGIN
  FOREACH relation IN ARRAY ARRAY['order_sync_runs','marketplace_orders','marketplace_order_items','order_observations','order_status_observations','order_lifecycle_events','order_deadlines','order_sync_coverage','order_sync_memberships','order_read_snapshots','order_read_snapshot_rows'] LOOP
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I)',relation) INTO occupied;
    IF occupied THEN RAISE EXCEPTION 'Orders downgrade blocked: nonempty %',relation; END IF;
  END LOOP;
END $$;
DROP TABLE order_read_snapshot_rows;
DROP TABLE order_read_snapshots;
DROP TABLE order_sync_memberships;
DROP TABLE order_sync_coverage;
DROP TABLE order_deadlines;
DROP TABLE order_lifecycle_events;
DROP TABLE order_status_observations;
DROP TABLE order_observations;
DROP TABLE marketplace_order_items;
DROP TABLE marketplace_orders;
DROP TABLE order_sync_runs;
ALTER TABLE marketplace_offers DROP CONSTRAINT uq_orders_offer_product_account;
ALTER TABLE marketplace_offers DROP CONSTRAINT fk_orders_offer_product_account;
ALTER TABLE marketplace_products DROP CONSTRAINT uq_orders_product_account;
DROP FUNCTION orders_guard_order_item_id();
DROP FUNCTION orders_guard_order_id();
DROP FUNCTION orders_guard_run();
DROP FUNCTION orders_reject_history_mutation();
DROP FUNCTION orders_exact_text(text);
DROP FUNCTION orders_check_snapshot();
DROP FUNCTION orders_check_snapshot_row();
