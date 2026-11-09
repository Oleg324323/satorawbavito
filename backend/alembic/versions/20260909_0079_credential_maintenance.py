"""Inert, private credential maintenance metadata; activation is separate.

No roles, source triggers, source views, definer functions or operational data.
"""

from alembic import op

revision = "20260909_0079"
down_revision = "20260909_0078"
branch_labels = depends_on = None


TABLES = r"""
CREATE SCHEMA credential_maintenance;
CREATE TABLE credential_maintenance.targets (
  target_credential_id uuid PRIMARY KEY CHECK (target_credential_id <> '00000000-0000-0000-0000-000000000000'),
  organization_id integer NOT NULL CHECK (organization_id > 0),
  marketplace_account_id integer NOT NULL CHECK (marketplace_account_id > 0),
  provider text COLLATE "C" NOT NULL,
  credential_kind text COLLATE "C" NOT NULL,
  CONSTRAINT cm_target_kind CHECK ((provider,credential_kind) IN (('wb','wb_api'),('avito','avito_oauth_client'))),
  CONSTRAINT cm_target_owner UNIQUE (organization_id,marketplace_account_id,provider,credential_kind),
  CONSTRAINT cm_target_binding UNIQUE (target_credential_id,organization_id,marketplace_account_id,provider,credential_kind)
);
CREATE TABLE credential_maintenance.authorizations (
  authorization_id uuid PRIMARY KEY CHECK (authorization_id <> '00000000-0000-0000-0000-000000000000'),
  target_credential_id uuid NOT NULL,
  organization_id integer NOT NULL CHECK (organization_id > 0),
  marketplace_account_id integer NOT NULL CHECK (marketplace_account_id > 0),
  provider text COLLATE "C" NOT NULL,
  credential_kind text COLLATE "C" NOT NULL,
  source_id integer NOT NULL CHECK (source_id > 0),
  source_user_id varchar(64) COLLATE "C" NOT NULL,
  expected_external_account_id varchar(128) COLLATE "C" NOT NULL CHECK (length(expected_external_account_id) > 0),
  expected_credential_ref varchar(255) COLLATE "C",
  recipient_role_oid oid NOT NULL CHECK (recipient_role_oid <> 0),
  recipient_role_name name NOT NULL CHECK (length(recipient_role_name::text) > 0),
  review_id uuid NOT NULL CHECK (review_id <> '00000000-0000-0000-0000-000000000000'),
  review_authority_id uuid NOT NULL CHECK (review_authority_id <> '00000000-0000-0000-0000-000000000000'),
  proof_reference_id uuid NOT NULL CHECK (proof_reference_id <> '00000000-0000-0000-0000-000000000000'),
  reviewed_at timestamptz NOT NULL CHECK (isfinite(reviewed_at)),
  allow_backfill boolean NOT NULL,
  allow_verify boolean NOT NULL,
  not_before timestamptz NOT NULL CHECK (isfinite(not_before)),
  expires_at timestamptz NOT NULL CHECK (isfinite(expires_at)),
  revoked_at timestamptz,
  revocation_reason_code text COLLATE "C",
  CONSTRAINT cm_auth_target FOREIGN KEY (target_credential_id,organization_id,marketplace_account_id,provider,credential_kind)
    REFERENCES credential_maintenance.targets(target_credential_id,organization_id,marketplace_account_id,provider,credential_kind),
  CONSTRAINT cm_auth_kind CHECK ((provider,credential_kind) IN (('wb','wb_api'),('avito','avito_oauth_client'))),
  CONSTRAINT cm_auth_ref CHECK ((provider <> 'wb' OR expected_credential_ref = 'lk_user_wb_tokens:' || source_id::text)
    AND (expected_credential_ref IS NULL OR length(expected_credential_ref) > 0)
    AND (provider <> 'wb' OR expected_credential_ref IS NOT NULL)),
  CONSTRAINT cm_auth_operations CHECK (allow_backfill OR allow_verify),
  CONSTRAINT cm_auth_validity CHECK (expires_at > not_before),
  CONSTRAINT cm_auth_revocation CHECK ((revoked_at IS NULL AND revocation_reason_code IS NULL) OR
    (revoked_at IS NOT NULL AND isfinite(revoked_at) AND revocation_reason_code IS NOT NULL
      AND revocation_reason_code IN ('source_changed','operator_revoked','security_incident')))
);
CREATE UNIQUE INDEX cm_auth_unrevoked_owner ON credential_maintenance.authorizations
 (organization_id,marketplace_account_id,provider,credential_kind) WHERE revoked_at IS NULL;
CREATE INDEX cm_auth_owner_history ON credential_maintenance.authorizations
 (organization_id,marketplace_account_id,provider,credential_kind,authorization_id);
CREATE INDEX cm_auth_target_history ON credential_maintenance.authorizations(target_credential_id,authorization_id);
CREATE INDEX cm_auth_source_unrevoked ON credential_maintenance.authorizations(provider,source_id,authorization_id)
 WHERE revoked_at IS NULL;
ALTER TABLE credential_maintenance.targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE credential_maintenance.targets FORCE ROW LEVEL SECURITY;
ALTER TABLE credential_maintenance.authorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE credential_maintenance.authorizations FORCE ROW LEVEL SECURITY;
DO $owner$ BEGIN
 EXECUTE format('CREATE POLICY cm_owner_select ON credential_maintenance.targets FOR SELECT TO %I USING (true)', current_user);
 EXECUTE format('CREATE POLICY cm_owner_select ON credential_maintenance.authorizations FOR SELECT TO %I USING (true)', current_user);
END $owner$;
"""

FUNCTIONS = r"""
CREATE FUNCTION credential_maintenance.target_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $body$
BEGIN
 RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_target_immutable';
END $body$;
CREATE FUNCTION credential_maintenance.authorization_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $body$
BEGIN
 IF TG_RELID <> 'credential_maintenance.authorizations'::regclass OR TG_WHEN <> 'BEFORE'
   OR TG_LEVEL <> 'ROW' OR TG_NARGS <> 0 OR TG_OP <> 'UPDATE' THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_authorization_immutable';
 END IF;
 IF (to_jsonb(NEW) - ARRAY['revoked_at','revocation_reason_code']) IS DISTINCT FROM
    (to_jsonb(OLD) - ARRAY['revoked_at','revocation_reason_code'])
    OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NOT isfinite(NEW.revoked_at)
    OR NEW.revocation_reason_code IS NULL
    OR NEW.revocation_reason_code NOT IN ('source_changed','operator_revoked','security_incident') THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_authorization_immutable';
 END IF;
 RETURN NEW;
END $body$;
CREATE TRIGGER cm_targets_immutable BEFORE UPDATE OR DELETE ON credential_maintenance.targets
 FOR EACH ROW EXECUTE FUNCTION credential_maintenance.target_immutable();
CREATE TRIGGER cm_authorizations_immutable BEFORE UPDATE OR DELETE ON credential_maintenance.authorizations
 FOR EACH ROW EXECUTE FUNCTION credential_maintenance.authorization_immutable();

CREATE FUNCTION credential_maintenance.lock_binding(a credential_maintenance.authorizations) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $body$
BEGIN
 IF a.organization_id IS NULL OR a.organization_id <= 0 OR a.marketplace_account_id IS NULL
   OR a.marketplace_account_id <= 0 OR a.source_id IS NULL OR a.source_id <= 0
   OR a.source_user_id IS NULL OR a.provider IS NULL
   OR (a.provider,a.credential_kind) NOT IN (('wb','wb_api'),('avito','avito_oauth_client'))
   OR a.expected_external_account_id IS NULL OR a.expected_external_account_id = ''
   OR (a.provider='wb' AND a.expected_credential_ref IS DISTINCT FROM 'lk_user_wb_tokens:' || a.source_id::text) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_binding_changed';
 END IF;
 -- Validate locator before taking a foreign user/account lock. No payload read.
 IF (a.provider='wb' AND NOT EXISTS (SELECT 1 FROM public.lk_user_wb_tokens s WHERE
   s.token_id=a.source_id AND s.user_id COLLATE "C"=a.source_user_id AND s.organization_id=a.organization_id))
 OR (a.provider='avito' AND NOT EXISTS (SELECT 1 FROM public.lk_user_avito_credentials s WHERE
   s.credentials_id=a.source_id AND s.user_id COLLATE "C"=a.source_user_id AND s.organization_id=a.organization_id)) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_binding_changed';
 END IF;
 PERFORM u.user_id FROM public.lk_users u WHERE u.user_id COLLATE "C"=a.source_user_id FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_binding_changed'; END IF;
 PERFORM m.marketplace_account_id FROM public.marketplace_accounts m
   WHERE m.marketplace_account_id=a.marketplace_account_id AND m.organization_id=a.organization_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_binding_changed'; END IF;
 IF a.provider='wb' THEN
   PERFORM s.token_id FROM public.lk_user_wb_tokens s WHERE s.token_id=a.source_id FOR SHARE;
 ELSE
   PERFORM s.credentials_id FROM public.lk_user_avito_credentials s WHERE s.credentials_id=a.source_id FOR SHARE;
 END IF;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_binding_changed'; END IF;
 -- Fresh statements after all waits, never a pre-wait joined snapshot.
 IF NOT EXISTS (SELECT 1 FROM public.lk_users u WHERE u.user_id COLLATE "C"=a.source_user_id
   AND u.organization_id=a.organization_id AND u.is_active) OR NOT EXISTS (
   SELECT 1 FROM public.marketplace_accounts m WHERE m.marketplace_account_id=a.marketplace_account_id
   AND m.organization_id=a.organization_id AND m.marketplace COLLATE "C"=a.provider
   AND m.status='connected' AND m.external_account_id COLLATE "C"=a.expected_external_account_id
   AND m.credential_ref COLLATE "C" IS NOT DISTINCT FROM a.expected_credential_ref) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_binding_changed';
 END IF;
 IF (a.provider='wb' AND NOT EXISTS (SELECT 1 FROM public.lk_user_wb_tokens s WHERE
   s.token_id=a.source_id AND s.user_id COLLATE "C"=a.source_user_id AND s.organization_id=a.organization_id))
 OR (a.provider='avito' AND NOT EXISTS (SELECT 1 FROM public.lk_user_avito_credentials s WHERE
   s.credentials_id=a.source_id AND s.user_id COLLATE "C"=a.source_user_id AND s.organization_id=a.organization_id)) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_binding_changed';
 END IF;
END $body$;

CREATE FUNCTION credential_maintenance.lock_authorization(p_id uuid,p_operation text)
RETURNS credential_maintenance.authorizations LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp AS $body$
DECLARE a credential_maintenance.authorizations; fresh credential_maintenance.authorizations;
BEGIN
 IF p_operation IS NULL OR p_operation NOT IN ('backfill','verify') THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_contract_invalid';
 END IF;
 SELECT x.* INTO a FROM credential_maintenance.authorizations x JOIN pg_catalog.pg_roles r
 ON r.oid=x.recipient_role_oid AND r.rolname=x.recipient_role_name AND r.rolname=session_user
 WHERE r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication)
 AND x.authorization_id=p_id AND x.revoked_at IS NULL AND x.not_before<=clock_timestamp()
 AND x.expires_at>clock_timestamp() AND CASE p_operation WHEN 'backfill' THEN x.allow_backfill ELSE x.allow_verify END;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_authorization_denied'; END IF;
 PERFORM credential_maintenance.lock_binding(a);
 PERFORM x.authorization_id FROM credential_maintenance.authorizations x WHERE x.authorization_id=p_id FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_authorization_denied'; END IF;
 SELECT x.* INTO fresh FROM credential_maintenance.authorizations x JOIN pg_catalog.pg_roles r
 ON r.oid=x.recipient_role_oid AND r.rolname=x.recipient_role_name AND r.rolname=session_user
 WHERE r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication)
 AND x.authorization_id=p_id AND x.revoked_at IS NULL AND x.not_before<=clock_timestamp()
 AND x.expires_at>clock_timestamp() AND CASE p_operation WHEN 'backfill' THEN x.allow_backfill ELSE x.allow_verify END;
 IF NOT FOUND OR fresh IS DISTINCT FROM a THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_authorization_denied';
 END IF;
 RETURN fresh;
END $body$;

CREATE FUNCTION credential_maintenance.lock_registration(a credential_maintenance.authorizations)
RETURNS TABLE(token text,client_id text,client_secret text) LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp AS $body$
BEGIN
 PERFORM credential_maintenance.lock_binding(a);
 PERFORM x.authorization_id FROM credential_maintenance.authorizations x
 WHERE (x.organization_id,x.marketplace_account_id,x.provider,x.credential_kind)=
   (a.organization_id,a.marketplace_account_id,a.provider,a.credential_kind)
   OR x.target_credential_id=a.target_credential_id OR x.authorization_id=a.authorization_id
 ORDER BY x.authorization_id FOR UPDATE;
 IF a.provider='wb' THEN
   RETURN QUERY SELECT s.wb_token,NULL::text,NULL::text FROM public.lk_user_wb_tokens s WHERE s.token_id=a.source_id;
 ELSE
   RETURN QUERY SELECT NULL::text,s.client_id,s.client_secret FROM public.lk_user_avito_credentials s WHERE s.credentials_id=a.source_id;
 END IF;
END $body$;

CREATE FUNCTION credential_maintenance.register_authorization(a credential_maintenance.authorizations,p_previous uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $body$
DECLARE existing credential_maintenance.authorizations; reserved credential_maintenance.targets;
BEGIN
 -- Privileged SQL caller is trusted to have attested exact source bytes in the
 -- same physical root. This SQL call itself is NOT independent provider proof.
 PERFORM credential_maintenance.lock_registration(a);
 IF a.revoked_at IS NOT NULL OR a.revocation_reason_code IS NOT NULL OR NOT EXISTS (
   SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid=a.recipient_role_oid AND r.rolname=a.recipient_role_name
     AND r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication))
   OR a.expires_at<=clock_timestamp() THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_authorization_denied';
 END IF;
 SELECT x.* INTO existing FROM credential_maintenance.authorizations x WHERE x.authorization_id=a.authorization_id;
 IF FOUND THEN
   IF existing IS DISTINCT FROM a THEN RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_registration_conflict'; END IF;
   RETURN a.authorization_id;
 END IF;
 IF EXISTS (SELECT 1 FROM credential_maintenance.authorizations x WHERE
   ((x.organization_id,x.marketplace_account_id,x.provider,x.credential_kind)=
   (a.organization_id,a.marketplace_account_id,a.provider,a.credential_kind) OR x.target_credential_id=a.target_credential_id)
   AND (x.target_credential_id,x.organization_id,x.marketplace_account_id,x.provider,x.credential_kind,
     x.source_id,x.source_user_id,x.expected_external_account_id,x.expected_credential_ref) IS DISTINCT FROM
   (a.target_credential_id,a.organization_id,a.marketplace_account_id,a.provider,a.credential_kind,
     a.source_id,a.source_user_id,a.expected_external_account_id,a.expected_credential_ref)) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_registration_conflict';
 END IF;
 SELECT t.* INTO reserved FROM credential_maintenance.targets t WHERE t.target_credential_id=a.target_credential_id
   OR (t.organization_id,t.marketplace_account_id,t.provider,t.credential_kind)=
   (a.organization_id,a.marketplace_account_id,a.provider,a.credential_kind);
 IF FOUND THEN
   IF (reserved.target_credential_id,reserved.organization_id,reserved.marketplace_account_id,reserved.provider,reserved.credential_kind)
      IS DISTINCT FROM (a.target_credential_id,a.organization_id,a.marketplace_account_id,a.provider,a.credential_kind) THEN
     RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_registration_conflict';
   END IF;
 ELSE
   INSERT INTO credential_maintenance.targets VALUES
     (a.target_credential_id,a.organization_id,a.marketplace_account_id,a.provider,a.credential_kind);
 END IF;
 SELECT x.* INTO existing FROM credential_maintenance.authorizations x WHERE
   (x.organization_id,x.marketplace_account_id,x.provider,x.credential_kind)=
   (a.organization_id,a.marketplace_account_id,a.provider,a.credential_kind) AND x.revoked_at IS NULL;
 IF FOUND THEN
   IF p_previous IS DISTINCT FROM existing.authorization_id THEN
     RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_registration_conflict';
   END IF;
   UPDATE credential_maintenance.authorizations SET revoked_at=clock_timestamp(),revocation_reason_code='operator_revoked'
     WHERE authorization_id=p_previous AND revoked_at IS NULL;
 ELSIF p_previous IS NOT NULL THEN
   -- Renewal after source invalidation retains and explicitly identifies history.
   IF NOT EXISTS (SELECT 1 FROM credential_maintenance.authorizations x WHERE x.authorization_id=p_previous
     AND x.target_credential_id=a.target_credential_id AND x.revoked_at IS NOT NULL) THEN
     RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_registration_conflict';
   END IF;
 ELSIF EXISTS (SELECT 1 FROM credential_maintenance.authorizations x WHERE x.target_credential_id=a.target_credential_id) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_registration_conflict';
 END IF;
 INSERT INTO credential_maintenance.authorizations SELECT (a).*;
 RETURN a.authorization_id;
END $body$;

CREATE FUNCTION credential_maintenance.invalidate_wb_source() RETURNS trigger LANGUAGE plpgsql
VOLATILE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $body$
DECLARE auth_id uuid;
BEGIN
 IF TG_RELID <> 'public.lk_user_wb_tokens'::regclass OR TG_WHEN <> 'AFTER' OR TG_LEVEL <> 'ROW'
   OR TG_NARGS <> 0 OR TG_OP NOT IN ('UPDATE','DELETE') THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_trigger_invalid';
 END IF;
 FOR auth_id IN SELECT a.authorization_id FROM credential_maintenance.authorizations a
   WHERE a.provider='wb' AND a.source_id=OLD.token_id AND a.revoked_at IS NULL
   ORDER BY a.authorization_id FOR UPDATE LOOP
   UPDATE credential_maintenance.authorizations SET revoked_at=clock_timestamp(),revocation_reason_code='source_changed'
     WHERE authorization_id=auth_id AND provider='wb' AND source_id=OLD.token_id AND revoked_at IS NULL;
 END LOOP;
 RETURN NULL;
END $body$;
CREATE FUNCTION credential_maintenance.append_safe_audit(p_id uuid,p_operation text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $body$
DECLARE a credential_maintenance.authorizations;
BEGIN
 -- Fixed metadata only. The trusted store has already verified persisted AEAD
 -- and exact payload in this same root. An audit call is not proof of decryption.
 a := credential_maintenance.lock_authorization(p_id,p_operation);
 IF NOT EXISTS (SELECT 1 FROM public.marketplace_account_credentials c WHERE c.credential_id=a.target_credential_id
   AND c.organization_id=a.organization_id AND c.marketplace_account_id=a.marketplace_account_id
   AND c.provider COLLATE "C"=a.provider AND c.credential_kind COLLATE "C"=a.credential_kind
   AND c.generation=1 AND c.payload_schema_version=1 AND c.expires_at IS NULL AND c.revoked_at IS NULL
   AND c.revocation_reason_code IS NULL) OR EXISTS (SELECT 1 FROM public.marketplace_account_credentials c
   WHERE c.organization_id=a.organization_id AND c.marketplace_account_id=a.marketplace_account_id
   AND c.provider COLLATE "C"=a.provider AND c.credential_kind COLLATE "C"=a.credential_kind
   AND c.credential_id<>a.target_credential_id) THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_history_conflict';
 END IF;
 INSERT INTO public.lk_audit_events(organization_id,actor_user_id,action,object_type,object_id,details)
 VALUES (a.organization_id,NULL,'integration.marketplace_credential.'||p_operation,
   'marketplace_account_credential',a.target_credential_id::text,
   json_build_object('credentialKind',a.credential_kind,'generation',1,'marketplaceAccountId',a.marketplace_account_id,
     'operation',p_operation,'provider',a.provider,'resultCode','ready'));
END $body$;
CREATE FUNCTION credential_maintenance.invalidate_avito_source() RETURNS trigger LANGUAGE plpgsql
VOLATILE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $body$
DECLARE auth_id uuid;
BEGIN
 IF TG_RELID <> 'public.lk_user_avito_credentials'::regclass OR TG_WHEN <> 'AFTER' OR TG_LEVEL <> 'ROW'
   OR TG_NARGS <> 0 OR TG_OP NOT IN ('UPDATE','DELETE') THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_trigger_invalid';
 END IF;
 FOR auth_id IN SELECT a.authorization_id FROM credential_maintenance.authorizations a
   WHERE a.provider='avito' AND a.source_id=OLD.credentials_id AND a.revoked_at IS NULL
   ORDER BY a.authorization_id FOR UPDATE LOOP
   UPDATE credential_maintenance.authorizations SET revoked_at=clock_timestamp(),revocation_reason_code='source_changed'
     WHERE authorization_id=auth_id AND provider='avito' AND source_id=OLD.credentials_id AND revoked_at IS NULL;
 END LOOP;
 RETURN NULL;
END $body$;
"""

# Only freshly created private objects are scrubbed. No existing role/default ACL
# or unrelated public object is adopted, repaired or revoked by this migration.
SCRUB = r"""
DO $acl$
DECLARE g record; obj record; grantee text;
BEGIN
 FOR obj IN SELECT n.oid,n.nspowner AS owner,'SCHEMA'::text AS kind,quote_ident(n.nspname) AS ident,n.nspacl AS acl
   FROM pg_catalog.pg_namespace n WHERE n.nspname='credential_maintenance'
 UNION ALL SELECT c.oid,c.relowner,'TABLE',format('%I.%I',n.nspname,c.relname),c.relacl
   FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='credential_maintenance' AND c.relkind='r'
 UNION ALL SELECT p.oid,p.proowner,'FUNCTION',p.oid::regprocedure::text,coalesce(p.proacl,acldefault('f',p.proowner))
   FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='credential_maintenance'
 LOOP
   FOR g IN SELECT DISTINCT x.grantee FROM aclexplode(obj.acl) x WHERE x.grantee<>obj.owner LOOP
     grantee := CASE WHEN g.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(g.grantee)) END;
     EXECUTE format('REVOKE ALL PRIVILEGES ON %s %s FROM %s RESTRICT',obj.kind,obj.ident,grantee);
   END LOOP;
 END LOOP;
 FOR obj IN SELECT c.oid,c.relowner,a.attname,a.attacl,format('%I.%I',n.nspname,c.relname) AS ident
   FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
   JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='credential_maintenance'
   AND a.attnum>0 AND NOT a.attisdropped AND a.attacl IS NOT NULL LOOP
   FOR g IN SELECT DISTINCT x.grantee FROM aclexplode(obj.attacl) x WHERE x.grantee<>obj.relowner LOOP
     grantee := CASE WHEN g.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(g.grantee)) END;
     EXECUTE format('REVOKE ALL PRIVILEGES (%I) ON TABLE %s FROM %s RESTRICT',obj.attname,obj.ident,grantee);
   END LOOP;
 END LOOP;
END $acl$;
"""

INERT_ASSERTION = r"""
CREATE FUNCTION credential_maintenance.assert_inert() RETURNS void LANGUAGE plpgsql
SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $body$
DECLARE owner_id oid; rel record; seal jsonb; actual jsonb;
BEGIN
 SELECT n.nspowner INTO owner_id FROM pg_catalog.pg_namespace n WHERE n.nspname='credential_maintenance';
 seal := obj_description('credential_maintenance'::regnamespace,'pg_namespace')::jsonb;
 SELECT jsonb_object_agg(p.oid::regprocedure::text,encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')) INTO actual
   FROM pg_catalog.pg_proc p WHERE p.pronamespace='credential_maintenance'::regnamespace;
 IF seal IS NULL OR seal->'functions' IS DISTINCT FROM actual THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_manifest_invalid';
 END IF;
 SELECT jsonb_object_agg(c.relname,jsonb_build_object(
   'columns',(SELECT jsonb_agg(jsonb_build_array(a.attname,a.atttypid,a.atttypmod,a.attnotnull,a.attcollation,a.attidentity,a.attgenerated,a.attisdropped) ORDER BY a.attnum)
     FROM pg_catalog.pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0),
   'constraints',(SELECT jsonb_agg(jsonb_build_array(x.conname,pg_get_constraintdef(x.oid)) ORDER BY x.conname) FROM pg_catalog.pg_constraint x WHERE x.conrelid=c.oid),
   'indexes',(SELECT jsonb_agg(pg_get_indexdef(i.indexrelid) ORDER BY i.indexrelid::regclass::text) FROM pg_catalog.pg_index i WHERE i.indrelid=c.oid))) INTO actual
 FROM pg_catalog.pg_class c WHERE c.relnamespace='credential_maintenance'::regnamespace AND c.relkind='r';
 IF seal->'relations' IS DISTINCT FROM actual THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_manifest_invalid';
 END IF;
 IF owner_id IS DISTINCT FROM (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user)
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n, LATERAL aclexplode(n.nspacl) a
     WHERE n.nspname='credential_maintenance' AND a.grantee<>owner_id)
   OR (SELECT count(*) FROM pg_catalog.pg_class c WHERE c.relnamespace='credential_maintenance'::regnamespace AND c.relkind='r')<>2
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace='credential_maintenance'::regnamespace
     AND c.relkind NOT IN ('r','i'))
   OR (SELECT count(*) FROM pg_catalog.pg_proc p WHERE p.pronamespace='credential_maintenance'::regnamespace)<>10
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.pronamespace='credential_maintenance'::regnamespace AND
     (p.proowner<>owner_id OR p.prosecdef OR p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']
      OR p.proname NOT IN ('target_immutable','authorization_immutable','lock_binding','lock_authorization',
        'lock_registration','register_authorization','invalidate_wb_source','invalidate_avito_source','append_safe_audit','assert_inert')
      OR EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee<>owner_id))) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_manifest_invalid';
 END IF;
 FOR rel IN SELECT c.* FROM pg_catalog.pg_class c WHERE c.oid IN
   ('credential_maintenance.targets'::regclass,'credential_maintenance.authorizations'::regclass) LOOP
   IF rel.relowner<>owner_id OR NOT rel.relrowsecurity OR NOT rel.relforcerowsecurity
     OR EXISTS (SELECT 1 FROM aclexplode(rel.relacl) a WHERE a.grantee<>owner_id)
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a,LATERAL aclexplode(a.attacl) x
       WHERE a.attrelid=rel.oid AND x.grantee<>owner_id)
     OR (SELECT count(*) FROM pg_catalog.pg_policy p WHERE p.polrelid=rel.oid)<>1
     OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid=rel.oid
       AND p.polname='cm_owner_select' AND p.polcmd='r' AND p.polpermissive
       AND p.polroles=ARRAY[owner_id] AND pg_get_expr(p.polqual,p.polrelid)='true' AND p.polwithcheck IS NULL) THEN
     RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_manifest_invalid';
   END IF;
 END LOOP;
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
   WHERE p.pronamespace='credential_maintenance'::regnamespace AND
     NOT (t.tgrelid='credential_maintenance.targets'::regclass AND t.tgname='cm_targets_immutable'
       AND p.proname='target_immutable' AND t.tgtype=27 AND t.tgenabled='O' AND t.tgnargs=0
       OR t.tgrelid='credential_maintenance.authorizations'::regclass AND t.tgname='cm_authorizations_immutable'
       AND p.proname='authorization_immutable' AND t.tgtype=27 AND t.tgenabled='O' AND t.tgnargs=0))
   OR (SELECT count(*) FROM pg_catalog.pg_trigger t WHERE NOT t.tgisinternal AND t.tgrelid IN
     ('credential_maintenance.targets'::regclass,'credential_maintenance.authorizations'::regclass))<>2 THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_manifest_invalid';
 END IF;
 -- Verified forced-RLS owner policies above make these unfiltered proofs.
 IF EXISTS (SELECT 1 FROM credential_maintenance.targets) OR EXISTS (SELECT 1 FROM credential_maintenance.authorizations) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='maintenance_not_empty';
 END IF;
END $body$;
"""

# A schema-owner-controlled, nonsecret seal of the freshly created static bodies.
# Operational function owners cannot update this schema comment. It supplements
# (never substitutes for) exact signature, ownership, ACL and dependency checks.
SEAL = r"""
DO $seal$ DECLARE fingerprint jsonb; relations jsonb;
BEGIN
 SELECT jsonb_object_agg(p.oid::regprocedure::text,encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')) INTO fingerprint
   FROM pg_catalog.pg_proc p WHERE p.pronamespace='credential_maintenance'::regnamespace;
 SELECT jsonb_object_agg(c.relname,jsonb_build_object(
   'columns',(SELECT jsonb_agg(jsonb_build_array(a.attname,a.atttypid,a.atttypmod,a.attnotnull,a.attcollation,a.attidentity,a.attgenerated,a.attisdropped) ORDER BY a.attnum)
     FROM pg_catalog.pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0),
   'constraints',(SELECT jsonb_agg(jsonb_build_array(x.conname,pg_get_constraintdef(x.oid)) ORDER BY x.conname) FROM pg_catalog.pg_constraint x WHERE x.conrelid=c.oid),
   'indexes',(SELECT jsonb_agg(pg_get_indexdef(i.indexrelid) ORDER BY i.indexrelid::regclass::text) FROM pg_catalog.pg_index i WHERE i.indrelid=c.oid))) INTO relations
 FROM pg_catalog.pg_class c WHERE c.relnamespace='credential_maintenance'::regnamespace AND c.relkind='r';
 EXECUTE format('COMMENT ON SCHEMA credential_maintenance IS %L',jsonb_build_object('functions',fingerprint,'relations',relations)::text);
END $seal$;
"""


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(FUNCTIONS)
    op.execute(INERT_ASSERTION)
    op.execute(SCRUB)
    op.execute(SEAL)
    op.execute("SELECT credential_maintenance.assert_inert()")


def downgrade() -> None:
    op.execute("LOCK TABLE credential_maintenance.targets IN ACCESS EXCLUSIVE MODE NOWAIT")
    op.execute("LOCK TABLE credential_maintenance.authorizations IN ACCESS EXCLUSIVE MODE NOWAIT")
    op.execute("SELECT credential_maintenance.assert_inert()")
    # RESTRICT is also the final explicit foreign-dependency barrier. Transactional
    # DDL rolls back all prior drops if any unknown dependency prevents a drop.
    op.execute("DROP TRIGGER cm_authorizations_immutable ON credential_maintenance.authorizations RESTRICT")
    op.execute("DROP TRIGGER cm_targets_immutable ON credential_maintenance.targets RESTRICT")
    for signature in (
        "append_safe_audit(uuid,text)", "register_authorization(credential_maintenance.authorizations,uuid)",
        "lock_registration(credential_maintenance.authorizations)",
        "lock_authorization(uuid,text)", "lock_binding(credential_maintenance.authorizations)",
        "invalidate_wb_source()", "invalidate_avito_source()", "authorization_immutable()",
        "target_immutable()", "assert_inert()",
    ):
        op.execute(f"DROP FUNCTION credential_maintenance.{signature} RESTRICT")
    op.execute("DROP TABLE credential_maintenance.authorizations RESTRICT")
    op.execute("DROP TABLE credential_maintenance.targets RESTRICT")
    op.execute("DROP SCHEMA credential_maintenance RESTRICT")
