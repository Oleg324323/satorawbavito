"""Version the existing notification preferences owner and index inbox discovery.

Mixed-version deployment prerequisite: upgrade all legacy writers before enabling
canonical notifications. The trigger also versions legacy notification changes.
"""
from alembic import op

revision = "20260910_0081"
down_revision = "20260910_0080"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
ALTER TABLE public.lk_user_preferences ADD COLUMN notification_version bigint NOT NULL DEFAULT 1
 CONSTRAINT lk_notification_version_positive CHECK(notification_version > 0);
CREATE FUNCTION public.lk_notification_version_guard() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.notification_version IS DISTINCT FROM 1 THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='preferences_version_invalid';
  END IF;
  RETURN NEW;
 END IF;
 IF NEW.notification_version IS NULL OR NEW.notification_version < OLD.notification_version
  OR NEW.notification_version::numeric > OLD.notification_version::numeric+1 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='preferences_version_invalid';
 END IF;
 IF NEW.notification_settings::jsonb IS DISTINCT FROM OLD.notification_settings::jsonb
  OR NEW.notification_version > OLD.notification_version THEN
  IF OLD.notification_version=9223372036854775807 THEN
   RAISE EXCEPTION USING ERRCODE='22003',MESSAGE='preferences_version_exhausted';
  END IF;
  NEW.notification_version:=OLD.notification_version+1;
 ELSE
  NEW.notification_version:=OLD.notification_version;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.lk_notification_version_guard() FROM PUBLIC;
CREATE TRIGGER lk_notification_version BEFORE INSERT OR UPDATE OF notification_settings,notification_version
 ON public.lk_user_preferences FOR EACH ROW EXECUTE FUNCTION public.lk_notification_version_guard();
CREATE INDEX notification_in_app_account_timeline ON public.notification_in_app_events
 (organization_id,marketplace_account_id,marketplace,occurred_at DESC,event_id DESC);
""")


def downgrade():
    # Serialize against every writer, then refuse to erase any consumed CAS version.
    op.execute("""
LOCK TABLE public.lk_user_preferences IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
 IF EXISTS(SELECT FROM public.lk_user_preferences WHERE notification_version<>1) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='preferences_downgrade_would_lose_versions';
 END IF;
END $$;
DROP INDEX public.notification_in_app_account_timeline;
DROP TRIGGER lk_notification_version ON public.lk_user_preferences;
DROP FUNCTION public.lk_notification_version_guard();
ALTER TABLE public.lk_user_preferences DROP COLUMN notification_version;
""")
