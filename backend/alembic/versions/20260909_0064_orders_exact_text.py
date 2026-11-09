"""Preserve exact Orders TEXT identity without full-value B-tree limits.

Writers must lock the authorized account first in READ COMMITTED, then perform
exact replay comparison. Removed arbiters cannot be ON CONFLICT targets.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260909_0064"
down_revision = "20260909_0063"
branch_labels = depends_on = None

OWNER = ("organization_id", "marketplace_account_id")
# Actual PostgreSQL-generated 0062 names are retained as logical diagnostics.
UNIQUES = (
    ("order_sync_runs", "order_sync_runs_organization_id_marketplace_account_id_sour_key", OWNER + ("source_kind", "source_run_key")),
    ("marketplace_orders", "marketplace_orders_organization_id_marketplace_account_id_e_key", OWNER + ("external_order_id",)),
    ("marketplace_order_items", "marketplace_order_items_organization_id_marketplace_account_key", OWNER + ("order_id", "source_line_key")),
    ("order_lifecycle_events", "order_lifecycle_events_organization_id_marketplace_account__key", OWNER + ("order_id", "observation_id", "source_event_key")),
    ("order_deadlines", "order_deadlines_organization_id_marketplace_account_id_obse_key", OWNER + ("observation_id", "deadline_kind")),
    ("order_sync_coverage", "order_sync_coverage_organization_id_marketplace_account_id__key", OWNER + ("sync_run_id", "coverage_kind")),
)
ADAPTER_UNIQUE = ("order_sync_runs", "order_sync_runs_organization_id_marketplace_account_id_sync_key",
                  OWNER + ("sync_run_id", "source_kind", "adapter_version"))
OLD_FK = "order_observations_organization_id_marketplace_account_id__fkey"
NEW_FK = "fk_orders_observation_owner_run"
OLD_FK_DEFINITION = ("FOREIGN KEY (organization_id, marketplace_account_id, sync_run_id, source_kind, adapter_version) "
                     "REFERENCES order_sync_runs(organization_id, marketplace_account_id, sync_run_id, source_kind, adapter_version)")
NEW_FK_DEFINITION = ("FOREIGN KEY (organization_id, marketplace_account_id, sync_run_id) "
                     "REFERENCES order_sync_runs(organization_id, marketplace_account_id, sync_run_id)")
OBS_INDEXES = (
    ("uq_orders_observation_order_replay", OWNER + ("order_id", "source_kind", "adapter_version", "payload_checksum"), "order_item_id IS NULL"),
    ("uq_orders_observation_item_replay", OWNER + ("order_id", "order_item_id", "source_kind", "adapter_version", "payload_checksum"), "order_item_id IS NOT NULL"),
)
GUARDED_TABLES = tuple(table for table, _, _ in UNIQUES) + ("order_observations",)
TABLES = GUARDED_TABLES + ("order_status_observations", "order_sync_memberships", "order_read_snapshots", "order_read_snapshot_rows")
TEXT_KEYS = {"source_kind", "source_run_key", "external_order_id", "source_line_key", "adapter_version",
             "payload_checksum", "source_event_key", "deadline_kind", "coverage_kind"}


def _lock():
    # Lock before inspecting catalog shape or occupied state. Concurrent old
    # writers finish first; no writer can pass through an unguarded transition.
    op.execute("LOCK TABLE " + ",".join("public." + table for table in TABLES) + " IN ACCESS EXCLUSIVE MODE")


def _constraint_check(table, name, definition):
    op.execute(sa.text("""DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='public.""" + table + """'::regclass AND conname='""" + name + """'
          AND convalidated AND NOT condeferrable
          AND pg_get_constraintdef(oid)='""" + definition + """') THEN
        RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='orders_schema_drift';
      END IF;
    END $$;"""))


def _index_sql(name, columns, predicate):
    return (f"CREATE UNIQUE INDEX {name} ON public.order_observations USING btree "
            f"({', '.join(columns)}) WHERE ({predicate})")


def _exact_predicate(columns):
    return " AND ".join(
        f'existing.{column} COLLATE "C" = NEW.{column} COLLATE "C"'
        if column in TEXT_KEYS else f"existing.{column} = NEW.{column}"
        for column in columns
    )


def _guard_sql(table):
    checks = []
    if table == "order_observations":
        # The bounded FK remains authoritative for owner/run existence. This
        # separate exact check preserves the removed adapter/source binding;
        # 0062's run UPDATE guard keeps these parent fields immutable.
        checks.append("""
          IF NOT EXISTS (SELECT 1 FROM public.order_sync_runs parent
            WHERE parent.organization_id=NEW.organization_id
              AND parent.marketplace_account_id=NEW.marketplace_account_id
              AND parent.sync_run_id=NEW.sync_run_id
              AND parent.source_kind COLLATE "C" = NEW.source_kind COLLATE "C"
              AND parent.adapter_version COLLATE "C" = NEW.adapter_version COLLATE "C") THEN
            RAISE EXCEPTION USING ERRCODE='23503', MESSAGE='orders_source_binding_invalid',
              CONSTRAINT='fk_orders_observation_source_adapter';
          END IF;
        """)
        keys = [(name, cols, f"NEW.{predicate}", f"existing.{predicate}")
                for name, cols, predicate in OBS_INDEXES]
    else:
        keys = [(name, cols, "TRUE", "TRUE") for relation, name, cols in UNIQUES if relation == table]
    for name, columns, new_scope, existing_scope in keys:
        checks.append(f"""
          IF {new_scope} AND EXISTS (SELECT 1 FROM public.{table} existing
              WHERE {existing_scope} AND {_exact_predicate(columns)}) THEN
            RAISE EXCEPTION USING ERRCODE='23505', MESSAGE='orders_identity_conflict',
              CONSTRAINT='{name}';
          END IF;
        """)
    return f"""
      CREATE FUNCTION public.orders_exact_insert_{table}() RETURNS trigger
      LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF current_setting('transaction_isolation') <> 'read committed' THEN
          RAISE EXCEPTION USING ERRCODE='25000', MESSAGE='orders_isolation_invalid';
        END IF;
        PERFORM 1 FROM public.marketplace_accounts
          WHERE organization_id=NEW.organization_id
            AND marketplace_account_id=NEW.marketplace_account_id FOR UPDATE;
        -- Each following volatile PL/pgSQL statement takes a fresh RC snapshot,
        -- including when this INSERT's outer snapshot predates the account wait.
        {''.join(checks)}
        RETURN NEW;
      END $$;
      CREATE TRIGGER orders_exact_insert BEFORE INSERT ON public.{table}
        FOR EACH ROW EXECUTE FUNCTION public.orders_exact_insert_{table}();
    """


def upgrade():
    _lock()
    # Validate the complete replaced shape before the first mutation. Unexpected
    # dependencies also fail transactionally, because no drop uses CASCADE.
    for table, name, columns in (*UNIQUES, ADAPTER_UNIQUE):
        _constraint_check(table, name, f"UNIQUE ({', '.join(columns)})")
    _constraint_check("order_observations", OLD_FK, OLD_FK_DEFINITION)
    _constraint_check("order_sync_runs", "order_sync_runs_organization_id_marketplace_account_id_syn_key1",
                      "UNIQUE (organization_id, marketplace_account_id, sync_run_id)")
    for name, columns, predicate in OBS_INDEXES:
        definition = _index_sql(name, columns, predicate)
        op.execute(sa.text(f"""DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_index
            WHERE indexrelid=to_regclass('public.{name}') AND indisunique AND indisvalid
              AND indisready AND indrelid='public.order_observations'::regclass
              AND pg_get_indexdef(indexrelid)='{definition}') THEN
            RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='orders_schema_drift';
          END IF;
        END $$;"""))
    op.execute(f"ALTER TABLE public.order_observations DROP CONSTRAINT {OLD_FK}")
    for table, name, _ in (*UNIQUES, ADAPTER_UNIQUE):
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {name}")
    for name, _, _ in OBS_INDEXES:
        op.execute(f"DROP INDEX public.{name}")
    op.execute(f"ALTER TABLE public.order_observations ADD CONSTRAINT {NEW_FK} {NEW_FK_DEFINITION}")
    for table in GUARDED_TABLES:
        op.execute(sa.text(_guard_sql(table)))


def downgrade():
    _lock()
    # This does NOT bypass FORCE RLS. An actor unable to see all affected rows
    # receives PostgreSQL's error instead of mistaking invisible data for empty.
    op.execute("SET LOCAL row_security=off")
    for table in TABLES:
        op.execute(sa.text(f"""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM public.{table}) THEN
            RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='orders_downgrade_nonempty';
          END IF;
        END $$;"""))
    _constraint_check("order_observations", NEW_FK, NEW_FK_DEFINITION)
    for table in GUARDED_TABLES:
        op.execute(f"DROP TRIGGER orders_exact_insert ON public.{table}")
        op.execute(f"DROP FUNCTION public.orders_exact_insert_{table}()")
    op.execute(f"ALTER TABLE public.order_observations DROP CONSTRAINT {NEW_FK}")
    for table, name, columns in (*UNIQUES, ADAPTER_UNIQUE):
        op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT {name} UNIQUE ({', '.join(columns)})")
    for name, columns, predicate in OBS_INDEXES:
        op.execute(_index_sql(name, columns, predicate))
    op.execute(f"ALTER TABLE public.order_observations ADD CONSTRAINT {OLD_FK} {OLD_FK_DEFINITION}")
