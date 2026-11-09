-- INERT SOURCE ARTIFACT. Run only after independent acceptance and owner approval.
-- psql -X, ON_ERROR_STOP. Explicit dedicated database; direct schema-owner login.
-- Required variables, with NO role/identity/policy defaults:
-- cm_database, cm_schema_owner, cm_schema_owner_oid, cm_helper_owner,
-- cm_helper_owner_oid, cm_invalidator_owner, cm_invalidator_owner_oid,
-- cm_registrar, cm_registrar_oid, cm_runner, cm_runner_oid,
-- cm_runtime_roles: JSON array of exact {"oid":positive_integer,"name":"role"}.
-- All named roles must already exist. This artifact NEVER creates/removes roles.
-- No proof, source value, path, key or secret is an input. Runtime inventory must
-- be independently established by the owner; a SQL array cannot prove completeness.
-- Deprovision includes this file with cm_action=deprovision to reuse one exact
-- catalog/grant/lock implementation. Do not invoke parts of this transaction.
\set ON_ERROR_STOP on
\if :{?cm_action}
\else
\set cm_action provision
\endif
BEGIN;
CREATE TEMP TABLE cm_input (action text,database_name text) ON COMMIT DROP;
INSERT INTO cm_input VALUES (:'cm_action',:'cm_database');
CREATE TEMP TABLE cm_roles (purpose text PRIMARY KEY,role_id oid UNIQUE,role_name name UNIQUE) ON COMMIT DROP;
INSERT INTO cm_roles VALUES
 ('schema',:'cm_schema_owner_oid'::oid,:'cm_schema_owner'::name),
 ('helper',:'cm_helper_owner_oid'::oid,:'cm_helper_owner'::name),
 ('invalidator',:'cm_invalidator_owner_oid'::oid,:'cm_invalidator_owner'::name),
 ('registrar',:'cm_registrar_oid'::oid,:'cm_registrar'::name),
 ('runner',:'cm_runner_oid'::oid,:'cm_runner'::name);
CREATE TEMP TABLE cm_runtime (role_id oid PRIMARY KEY,role_name name UNIQUE) ON COMMIT DROP;
INSERT INTO cm_runtime SELECT (x->>'oid')::oid,(x->>'name')::name
 FROM jsonb_array_elements(:'cm_runtime_roles'::jsonb) x;
-- Function manifest and final recipient of each function; invariants stay invoker
-- under schema ownership. The two operational owners are independent capabilities.
CREATE TEMP TABLE cm_functions (signature text PRIMARY KEY,purpose text NOT NULL,definer boolean NOT NULL) ON COMMIT DROP;
INSERT INTO cm_functions VALUES
 ('credential_maintenance.target_immutable()','schema',false),
 ('credential_maintenance.authorization_immutable()','schema',false),
 ('credential_maintenance.assert_inert()','schema',false),
 ('credential_maintenance.lock_binding(credential_maintenance.authorizations)','helper',true),
 ('credential_maintenance.lock_authorization(uuid,text)','helper',true),
 ('credential_maintenance.lock_registration(credential_maintenance.authorizations)','helper',true),
 ('credential_maintenance.register_authorization(credential_maintenance.authorizations,uuid)','helper',true),
 ('credential_maintenance.append_safe_audit(uuid,text)','helper',true),
 ('credential_maintenance.invalidate_wb_source()','invalidator',true),
 ('credential_maintenance.invalidate_avito_source()','invalidator',true);
CREATE TEMP TABLE cm_views (view_name text PRIMARY KEY,definition text NOT NULL) ON COMMIT DROP;
INSERT INTO cm_views VALUES
 ('authorized_metadata',$view$
 SELECT a.* FROM credential_maintenance.authorizations a JOIN pg_catalog.pg_roles r
 ON r.oid=a.recipient_role_oid AND r.rolname=a.recipient_role_name AND r.rolname=session_user
 WHERE r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication)
 AND a.revoked_at IS NULL AND a.not_before<=clock_timestamp() AND a.expires_at>clock_timestamp()
 $view$),
 ('authorized_wb_source',$view$
 SELECT a.authorization_id,s.wb_token AS token FROM credential_maintenance.authorized_metadata a
 JOIN public.lk_user_wb_tokens s ON a.provider='wb' AND s.token_id=a.source_id
 AND s.user_id COLLATE "C"=a.source_user_id AND s.organization_id=a.organization_id
 JOIN public.lk_users u ON u.user_id COLLATE "C"=a.source_user_id AND u.organization_id=a.organization_id AND u.is_active
 JOIN public.marketplace_accounts m ON m.marketplace_account_id=a.marketplace_account_id
 AND m.organization_id=a.organization_id AND m.marketplace COLLATE "C"=a.provider AND m.status='connected'
 AND m.external_account_id COLLATE "C"=a.expected_external_account_id
 AND m.credential_ref COLLATE "C" IS NOT DISTINCT FROM a.expected_credential_ref
 WHERE a.organization_id::text=current_setting('app.organization_id',true)
 $view$),
 ('authorized_avito_source',$view$
 SELECT a.authorization_id,s.client_id,s.client_secret FROM credential_maintenance.authorized_metadata a
 JOIN public.lk_user_avito_credentials s ON a.provider='avito' AND s.credentials_id=a.source_id
 AND s.user_id COLLATE "C"=a.source_user_id AND s.organization_id=a.organization_id
 JOIN public.lk_users u ON u.user_id COLLATE "C"=a.source_user_id AND u.organization_id=a.organization_id AND u.is_active
 JOIN public.marketplace_accounts m ON m.marketplace_account_id=a.marketplace_account_id
 AND m.organization_id=a.organization_id AND m.marketplace COLLATE "C"=a.provider AND m.status='connected'
 AND m.external_account_id COLLATE "C"=a.expected_external_account_id
 AND m.credential_ref COLLATE "C" IS NOT DISTINCT FROM a.expected_credential_ref
 WHERE a.organization_id::text=current_setting('app.organization_id',true)
 $view$),
 ('authorized_history',$view$
 SELECT a.authorization_id,c.credential_id,c.organization_id,c.marketplace_account_id,c.provider,c.credential_kind,
 c.generation,c.payload_schema_version,c.expires_at,c.revoked_at,c.revocation_reason_code,c.created_at,c.updated_at
 FROM credential_maintenance.authorized_metadata a
 JOIN public.marketplace_accounts m ON m.marketplace_account_id=a.marketplace_account_id
 AND m.organization_id=a.organization_id AND m.marketplace COLLATE "C"=a.provider AND m.status='connected'
 AND m.external_account_id COLLATE "C"=a.expected_external_account_id
 AND m.credential_ref COLLATE "C" IS NOT DISTINCT FROM a.expected_credential_ref
 LEFT JOIN public.marketplace_account_credentials c ON c.organization_id=a.organization_id
 AND c.marketplace_account_id=a.marketplace_account_id AND c.provider COLLATE "C"=a.provider
 AND c.credential_kind COLLATE "C"=a.credential_kind
 WHERE a.organization_id::text=current_setting('app.organization_id',true)
 $view$);
CREATE TEMP TABLE cm_triggers (relation text,trigger_name text PRIMARY KEY,function_name text,type_bits integer,columns text[]) ON COMMIT DROP;
INSERT INTO cm_triggers VALUES
 ('public.lk_user_wb_tokens','cm_wb_source_update','invalidate_wb_source',17,ARRAY['token_id','user_id','organization_id','wb_token']),
 ('public.lk_user_wb_tokens','cm_wb_source_delete','invalidate_wb_source',9,ARRAY[]::text[]),
 ('public.lk_user_avito_credentials','cm_avito_source_update','invalidate_avito_source',17,ARRAY['credentials_id','user_id','organization_id','client_id','client_secret']),
 ('public.lk_user_avito_credentials','cm_avito_source_delete','invalidate_avito_source',9,ARRAY[]::text[]);

-- Each row is one justified privilege. NULL columns means table/object privilege;
-- UPDATE(column) is actual DML, required by PostgreSQL row-lock statements.
CREATE TEMP TABLE cm_grants (purpose text,kind text,object_name text,privilege text,columns text[]) ON COMMIT DROP;
INSERT INTO cm_grants VALUES
 ('helper','SCHEMA','credential_maintenance','USAGE',NULL),
 ('invalidator','SCHEMA','credential_maintenance','USAGE',NULL),
 ('registrar','SCHEMA','credential_maintenance','USAGE',NULL),
 ('runner','SCHEMA','credential_maintenance','USAGE',NULL),
 ('helper','SCHEMA','public','USAGE',NULL),
 ('runner','SCHEMA','public','USAGE',NULL),
 ('helper','TABLE','credential_maintenance.targets','SELECT',NULL),
 ('helper','TABLE','credential_maintenance.targets','INSERT',NULL),
 ('helper','TABLE','credential_maintenance.authorizations','SELECT',NULL),
 ('helper','TABLE','credential_maintenance.authorizations','INSERT',NULL),
 ('helper','TABLE','credential_maintenance.authorizations','UPDATE',ARRAY['authorization_id','revoked_at','revocation_reason_code']),
 ('helper','TABLE','public.lk_users','SELECT',ARRAY['user_id','organization_id','is_active']),
 ('helper','TABLE','public.lk_users','UPDATE',ARRAY['user_id']),
 ('helper','TABLE','public.marketplace_accounts','SELECT',ARRAY['marketplace_account_id','organization_id','marketplace','external_account_id','credential_ref','status']),
 ('helper','TABLE','public.marketplace_accounts','UPDATE',ARRAY['marketplace_account_id']),
 ('helper','TABLE','public.lk_user_wb_tokens','SELECT',ARRAY['token_id','user_id','organization_id','wb_token']),
 ('helper','TABLE','public.lk_user_wb_tokens','UPDATE',ARRAY['token_id']),
 ('helper','TABLE','public.lk_user_avito_credentials','SELECT',ARRAY['credentials_id','user_id','organization_id','client_id','client_secret']),
 ('helper','TABLE','public.lk_user_avito_credentials','UPDATE',ARRAY['credentials_id']),
 ('helper','TABLE','public.marketplace_account_credentials','SELECT',ARRAY['credential_id','organization_id','marketplace_account_id','provider','credential_kind','generation','payload_schema_version','expires_at','revoked_at','revocation_reason_code','created_at','updated_at']),
 ('invalidator','TABLE','credential_maintenance.authorizations','SELECT',ARRAY['authorization_id','provider','source_id','revoked_at','revocation_reason_code']),
 ('invalidator','TABLE','credential_maintenance.authorizations','UPDATE',ARRAY['revoked_at','revocation_reason_code']),
 ('runner','TABLE','public.marketplace_account_credentials','SELECT',NULL),
 ('runner','TABLE','public.marketplace_account_credentials','INSERT',NULL),
 ('helper','TABLE','public.lk_audit_events','INSERT',ARRAY['organization_id','actor_user_id','action','object_type','object_id','details']),
 ('runner','FUNCTION','credential_maintenance.lock_authorization(uuid,text)','EXECUTE',NULL),
 ('runner','FUNCTION','credential_maintenance.append_safe_audit(uuid,text)','EXECUTE',NULL),
 ('registrar','FUNCTION','credential_maintenance.lock_registration(credential_maintenance.authorizations)','EXECUTE',NULL),
 ('registrar','FUNCTION','credential_maintenance.register_authorization(credential_maintenance.authorizations,uuid)','EXECUTE',NULL);
INSERT INTO cm_grants SELECT 'runner','TABLE','credential_maintenance.'||view_name,'SELECT',NULL FROM cm_views;

DO $preflight$
DECLARE r record; reachable record; seq_name text; source oid;
BEGIN
 IF (SELECT action NOT IN ('provision','deprovision') OR database_name<>current_database() FROM cm_input)
   OR NOT EXISTS (SELECT 1 FROM cm_roles WHERE purpose='schema' AND role_name=current_user AND role_name=session_user
     AND role_id=(SELECT nspowner FROM pg_catalog.pg_namespace WHERE nspname='credential_maintenance'))
   OR NOT EXISTS (SELECT 1 FROM cm_runtime)
   OR EXISTS (SELECT 1 FROM cm_runtime x JOIN cm_roles r ON x.role_id=r.role_id)
   OR EXISTS (SELECT 1 FROM cm_roles x LEFT JOIN pg_catalog.pg_roles r ON r.oid=x.role_id AND r.rolname=x.role_name WHERE r.oid IS NULL)
   OR EXISTS (SELECT 1 FROM cm_runtime x LEFT JOIN pg_catalog.pg_roles r ON r.oid=x.role_id AND r.rolname=x.role_name WHERE r.oid IS NULL) THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_identity_invalid';
 END IF;
 FOR r IN SELECT p.*,x.purpose FROM cm_roles x JOIN pg_catalog.pg_roles p ON p.oid=x.role_id WHERE x.purpose<>'schema' LOOP
   IF r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolinherit
     OR r.rolcanlogin IS DISTINCT FROM (r.purpose IN ('runner','registrar'))
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_role_not_dedicated';
   END IF;
 END LOOP;
 -- Conservative graph: reject dangerous reachable memberships even when one
 -- server-version membership option would suppress inheritance/SET ROLE.
 FOR reachable IN WITH RECURSIVE reach(root,id) AS (
   SELECT role_id,role_id FROM cm_runtime UNION SELECT role_id,role_id FROM cm_roles WHERE purpose='runner'
   UNION SELECT q.root,m.roleid FROM reach q JOIN pg_catalog.pg_auth_members m ON m.member=q.id)
   SELECT DISTINCT q.id FROM reach q LOOP
   SELECT * INTO r FROM pg_catalog.pg_roles WHERE oid=reachable.id;
   IF r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
     OR EXISTS (SELECT 1 FROM cm_roles x WHERE x.purpose IN ('schema','helper','invalidator','registrar') AND x.role_id=r.oid)
     OR has_schema_privilege(r.oid,'credential_maintenance','CREATE')
     OR has_parameter_privilege(r.oid,'session_replication_role','SET') THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_source_bypass';
   END IF;
   FOREACH source IN ARRAY ARRAY['public.lk_user_wb_tokens'::regclass::oid,'public.lk_user_avito_credentials'::regclass::oid] LOOP
     IF has_table_privilege(r.oid,source,'TRUNCATE') OR has_table_privilege(r.oid,source,'TRIGGER')
       OR (SELECT relowner=r.oid FROM pg_catalog.pg_class WHERE oid=source) THEN
       RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_source_bypass';
     END IF;
   END LOOP;
 END LOOP;
 IF current_setting('session_replication_role')<>'origin'
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite WHERE ev_class IN
     ('public.lk_user_wb_tokens'::regclass,'public.lk_user_avito_credentials'::regclass)) THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_source_state_invalid';
 END IF;
 -- Actual serial/identity dependency, never a guessed sequence name/wildcard.
 seq_name := pg_get_serial_sequence('public.lk_audit_events','event_id');
 IF seq_name IS NULL OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
   JOIN pg_catalog.pg_attribute a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
   JOIN pg_catalog.pg_class c ON c.oid=d.objid
   WHERE d.classid='pg_class'::regclass AND d.refclassid='pg_class'::regclass
   AND d.objid=seq_name::regclass AND c.relkind='S' AND d.deptype IN ('a','i')
   AND d.refobjid='public.lk_audit_events'::regclass AND a.attname='event_id') THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_audit_schema_invalid';
 END IF;
 INSERT INTO cm_grants VALUES ('helper','SEQUENCE',seq_name,'USAGE',NULL);
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE oid IN
   ('public.marketplace_accounts'::regclass,'public.marketplace_account_credentials'::regclass)
   AND (NOT relrowsecurity OR NOT relforcerowsecurity)) THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_rls_invalid';
 END IF;
END $preflight$;

-- Fixed table order; no user/account lock acquisition in this transaction.
DO $locks$ BEGIN
 LOCK TABLE public.lk_user_wb_tokens IN ACCESS EXCLUSIVE MODE NOWAIT;
 LOCK TABLE public.lk_user_avito_credentials IN ACCESS EXCLUSIVE MODE NOWAIT;
 LOCK TABLE credential_maintenance.targets IN ACCESS EXCLUSIVE MODE NOWAIT;
 LOCK TABLE credential_maintenance.authorizations IN ACCESS EXCLUSIVE MODE NOWAIT;
EXCEPTION WHEN lock_not_available THEN
 RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_lock_busy';
END $locks$;

-- Exact RLS policy manifest. Definitions contain static SQL, roles only identifiers.
CREATE TEMP TABLE cm_policies (relation text,policy_name text PRIMARY KEY,purpose text,command text,restrictive boolean,using_sql text,check_sql text) ON COMMIT DROP;
INSERT INTO cm_policies VALUES
 ('credential_maintenance.targets','cm_helper_select','helper','SELECT',false,'true',NULL),
 ('credential_maintenance.targets','cm_helper_insert','helper','INSERT',false,NULL,'true'),
 ('credential_maintenance.authorizations','cm_helper_select_auth','helper','SELECT',false,'true',NULL),
 ('credential_maintenance.authorizations','cm_helper_insert_auth','helper','INSERT',false,NULL,'revoked_at IS NULL AND revocation_reason_code IS NULL'),
 ('credential_maintenance.authorizations','cm_helper_revoke','helper','UPDATE',false,'true',
   'revoked_at IS NOT NULL AND isfinite(revoked_at) AND revocation_reason_code = ''operator_revoked'''),
 ('credential_maintenance.authorizations','cm_invalidator_select','invalidator','SELECT',false,'true',NULL),
 ('credential_maintenance.authorizations','cm_invalidator_revoke','invalidator','UPDATE',false,'revoked_at IS NULL',
   'revoked_at IS NOT NULL AND isfinite(revoked_at) AND revocation_reason_code = ''source_changed'''),
 ('public.marketplace_account_credentials','cm_runner_select','runner','SELECT',true,$policy$
 EXISTS (SELECT 1 FROM credential_maintenance.authorized_metadata a WHERE
   a.target_credential_id=marketplace_account_credentials.credential_id
   AND a.organization_id=marketplace_account_credentials.organization_id
   AND a.marketplace_account_id=marketplace_account_credentials.marketplace_account_id
   AND a.provider=marketplace_account_credentials.provider AND a.credential_kind=marketplace_account_credentials.credential_kind)
 $policy$,NULL),
 ('public.marketplace_account_credentials','cm_runner_insert','runner','INSERT',true,NULL,$policy$
 generation=1 AND payload_schema_version=1 AND expires_at IS NULL AND revoked_at IS NULL AND revocation_reason_code IS NULL
 AND EXISTS (SELECT 1 FROM credential_maintenance.authorized_metadata a WHERE a.allow_backfill
   AND a.target_credential_id=marketplace_account_credentials.credential_id
   AND a.organization_id=marketplace_account_credentials.organization_id
   AND a.marketplace_account_id=marketplace_account_credentials.marketplace_account_id
   AND a.provider=marketplace_account_credentials.provider AND a.credential_kind=marketplace_account_credentials.credential_kind)
 $policy$);
-- Approved actual-schema adjustment: lk_audit_events has no RLS in 0005/0006.
-- The runner receives ONLY append_safe_audit(uuid,text) EXECUTE; the fixed helper
-- derives all six INSERT columns. No global audit/RLS/runtime behavior is changed.

-- Functions below are transaction-local implementation, never installed helpers.
CREATE FUNCTION pg_temp.cm_assert_empty() RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE r record; owner_id oid;
BEGIN
 SELECT role_id INTO owner_id FROM cm_roles WHERE purpose='schema';
 FOR r IN SELECT oid,relowner,relrowsecurity,relforcerowsecurity FROM pg_catalog.pg_class
 WHERE oid IN ('credential_maintenance.targets'::regclass,'credential_maintenance.authorizations'::regclass) LOOP
   IF r.relowner<>owner_id OR NOT r.relrowsecurity OR NOT r.relforcerowsecurity OR NOT EXISTS (
     SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid=r.oid AND p.polname='cm_owner_select'
     AND p.polroles=ARRAY[owner_id] AND p.polcmd='r' AND p.polpermissive
     AND pg_get_expr(p.polqual,p.polrelid)='true' AND p.polwithcheck IS NULL) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_manifest_invalid';
   END IF;
 END LOOP;
 IF EXISTS (SELECT 1 FROM credential_maintenance.targets) OR EXISTS (SELECT 1 FROM credential_maintenance.authorizations) THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_not_empty';
 END IF;
END $fn$;

CREATE FUNCTION pg_temp.cm_grant(action text,g pg_temp.cm_grants) RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE recipient name; col_sql text;
BEGIN
 SELECT role_name INTO recipient FROM cm_roles WHERE purpose=g.purpose;
 SELECT string_agg(quote_ident(x),',' ORDER BY n) INTO col_sql FROM unnest(g.columns) WITH ORDINALITY v(x,n);
 IF action='GRANT' THEN
   EXECUTE format('GRANT %s%s ON %s %s TO %I',g.privilege,CASE WHEN col_sql IS NULL THEN '' ELSE '('||col_sql||')' END,g.kind,g.object_name,recipient);
 ELSE
   EXECUTE format('REVOKE %s%s ON %s %s FROM %I RESTRICT',g.privilege,CASE WHEN col_sql IS NULL THEN '' ELSE '('||col_sql||')' END,g.kind,g.object_name,recipient);
 END IF;
END $fn$;

CREATE FUNCTION pg_temp.cm_assert_grants() RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE r record; obj record; col record; permission text; expected boolean; actual boolean;
BEGIN
 FOR r IN SELECT * FROM cm_roles WHERE purpose<>'schema' LOOP
   FOR obj IN SELECT DISTINCT object_name,kind FROM cm_grants WHERE kind IN ('TABLE','SEQUENCE','SCHEMA','FUNCTION') LOOP
     IF obj.kind='TABLE' THEN
       -- Owning a view entails its own ACL capabilities; the underlying base
       -- columns below remain constrained independently and contain no keys.
       IF r.purpose='helper' AND EXISTS (SELECT 1 FROM cm_views WHERE
         'credential_maintenance.'||view_name=obj.object_name) THEN CONTINUE; END IF;
       FOREACH permission IN ARRAY ARRAY['SELECT','INSERT','UPDATE','REFERENCES'] LOOP
         FOR col IN SELECT attname FROM pg_catalog.pg_attribute WHERE attrelid=obj.object_name::regclass AND attnum>0 AND NOT attisdropped LOOP
           SELECT EXISTS (SELECT 1 FROM cm_grants g WHERE g.purpose=r.purpose AND g.object_name=obj.object_name
             AND g.privilege=permission AND (g.columns IS NULL OR col.attname=ANY(g.columns))) INTO expected;
           actual := has_column_privilege(r.role_id,obj.object_name,col.attname,permission);
           IF actual IS DISTINCT FROM expected OR has_column_privilege(r.role_id,obj.object_name,col.attname,permission||' WITH GRANT OPTION') THEN
             RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_privilege_mismatch';
           END IF;
         END LOOP;
       END LOOP;
       IF has_table_privilege(r.role_id,obj.object_name,'DELETE,TRUNCATE,TRIGGER') THEN
         RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_privilege_mismatch';
       END IF;
     ELSIF obj.kind='SEQUENCE' THEN
       IF has_sequence_privilege(r.role_id,obj.object_name,'USAGE') IS DISTINCT FROM (r.purpose='helper')
         OR has_sequence_privilege(r.role_id,obj.object_name,'SELECT,UPDATE,USAGE WITH GRANT OPTION') THEN
         RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_privilege_mismatch';
       END IF;
     ELSIF obj.kind='SCHEMA' THEN
       -- public USAGE may pre-exist for PUBLIC; CREATE must never be inherited.
       IF has_schema_privilege(r.role_id,obj.object_name,'CREATE') THEN
         RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_privilege_mismatch';
       END IF;
     ELSE
       SELECT EXISTS (SELECT 1 FROM cm_grants g WHERE g.purpose=r.purpose AND g.object_name=obj.object_name)
         OR EXISTS (SELECT 1 FROM cm_functions f WHERE f.signature=obj.object_name AND f.purpose=r.purpose) INTO expected;
       IF has_function_privilege(r.role_id,obj.object_name,'EXECUTE') IS DISTINCT FROM expected THEN
         RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_privilege_mismatch';
       END IF;
     END IF;
   END LOOP;
 END LOOP;
END $fn$;

CREATE FUNCTION pg_temp.cm_assert_active() RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE f record; v record; t record; expected_attrs smallint[]; p record; actual_policy record; seal jsonb; actual jsonb;
BEGIN
 PERFORM pg_temp.cm_assert_empty();
 seal := obj_description('credential_maintenance'::regnamespace,'pg_namespace')::jsonb;
 SELECT jsonb_object_agg(p.oid::regprocedure::text,encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')) INTO actual
   FROM pg_catalog.pg_proc p WHERE p.pronamespace='credential_maintenance'::regnamespace;
 IF seal IS NULL OR seal->'functions' IS DISTINCT FROM actual THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_manifest_invalid';
 END IF;
 SELECT jsonb_object_agg(c.relname,jsonb_build_object(
   'columns',(SELECT jsonb_agg(jsonb_build_array(a.attname,a.atttypid,a.atttypmod,a.attnotnull,a.attcollation,a.attidentity,a.attgenerated,a.attisdropped) ORDER BY a.attnum)
     FROM pg_catalog.pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0),
   'constraints',(SELECT jsonb_agg(jsonb_build_array(x.conname,pg_get_constraintdef(x.oid)) ORDER BY x.conname) FROM pg_catalog.pg_constraint x WHERE x.conrelid=c.oid),
   'indexes',(SELECT jsonb_agg(pg_get_indexdef(i.indexrelid) ORDER BY i.indexrelid::regclass::text) FROM pg_catalog.pg_index i WHERE i.indrelid=c.oid))) INTO actual
 FROM pg_catalog.pg_class c WHERE c.relnamespace='credential_maintenance'::regnamespace AND c.relkind='r';
 IF seal->'relations' IS DISTINCT FROM actual THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_manifest_invalid';
 END IF;
 IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace='credential_maintenance'::regnamespace)<>10
   OR (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace='credential_maintenance'::regnamespace AND relkind='v')<>4
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace='credential_maintenance'::regnamespace AND relkind NOT IN ('r','i','v')) THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_manifest_invalid';
 END IF;
 IF (SELECT count(*) FROM pg_catalog.pg_policy WHERE polrelid IN
   ('credential_maintenance.targets'::regclass,'credential_maintenance.authorizations'::regclass))<>9
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d JOIN cm_roles r ON r.role_id=d.refobjid
     WHERE r.purpose<>'schema' AND d.refclassid='pg_authid'::regclass AND
     (d.dbid<>(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database()) OR NOT (
       d.classid='pg_class'::regclass AND d.objid IN (SELECT to_regclass(object_name) FROM cm_grants WHERE kind IN ('TABLE','SEQUENCE'))
       OR d.classid='pg_proc'::regclass AND d.objid IN (SELECT to_regprocedure(signature) FROM cm_functions)
       OR d.classid='pg_namespace'::regclass AND d.objid IN ('public'::regnamespace,'credential_maintenance'::regnamespace)
       OR d.classid='pg_policy'::regclass AND d.objid IN (SELECT x.oid FROM pg_catalog.pg_policy x JOIN cm_policies m
         ON x.polrelid=m.relation::regclass AND x.polname=m.policy_name))))) THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_foreign_dependency';
 END IF;
 FOR f IN SELECT m.*,r.role_id FROM cm_functions m JOIN cm_roles r USING(purpose) LOOP
   IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid=to_regprocedure(f.signature)
     AND p.proowner=f.role_id AND p.prosecdef=f.definer AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_manifest_invalid';
   END IF;
   IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc p,LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
     WHERE p.oid=to_regprocedure(f.signature) AND a.grantee<>p.proowner AND
       (a.is_grantable OR a.privilege_type<>'EXECUTE' OR NOT EXISTS (
         SELECT 1 FROM cm_grants g JOIN cm_roles r ON r.purpose=g.purpose
         WHERE g.kind='FUNCTION' AND g.object_name=f.signature AND r.role_id=a.grantee))) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_privilege_mismatch';
   END IF;
 END LOOP;
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n,LATERAL aclexplode(n.nspacl) a
   WHERE n.nspname='credential_maintenance' AND a.grantee<>n.nspowner AND
   (a.is_grantable OR a.privilege_type<>'USAGE' OR NOT EXISTS (SELECT 1 FROM cm_roles r
     WHERE r.role_id=a.grantee AND r.purpose IN ('helper','invalidator','registrar','runner'))))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c,LATERAL aclexplode(c.relacl) a
   WHERE c.relnamespace='credential_maintenance'::regnamespace AND a.grantee<>c.relowner AND
   (a.is_grantable OR NOT EXISTS (SELECT 1 FROM cm_grants g JOIN cm_roles r ON r.purpose=g.purpose
     WHERE g.kind='TABLE' AND g.object_name='credential_maintenance.'||c.relname
     AND g.columns IS NULL AND g.privilege=a.privilege_type AND r.role_id=a.grantee)))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_attribute att JOIN pg_catalog.pg_class c ON c.oid=att.attrelid,
   LATERAL aclexplode(att.attacl) a WHERE c.relnamespace='credential_maintenance'::regnamespace AND a.grantee<>c.relowner
   AND (a.is_grantable OR NOT EXISTS (SELECT 1 FROM cm_grants g JOIN cm_roles r ON r.purpose=g.purpose
     WHERE g.kind='TABLE' AND g.object_name='credential_maintenance.'||c.relname
     AND att.attname=ANY(g.columns) AND g.privilege=a.privilege_type AND r.role_id=a.grantee))) THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_privilege_mismatch';
 END IF;
 FOR v IN SELECT * FROM cm_views LOOP
   IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.oid=to_regclass('credential_maintenance.'||v.view_name)
     AND c.relkind='v' AND c.relowner=(SELECT role_id FROM cm_roles WHERE purpose='helper')
     AND c.reloptions=ARRAY['security_barrier=true']) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_manifest_invalid';
   END IF;
   -- Parse the exact static definition into a transaction-local comparison view;
   -- pg_get_viewdef canonicalizes syntax on both sides without executing source.
   EXECUTE format('CREATE TEMP VIEW cm_expected_view AS %s',v.definition);
   IF pg_get_viewdef(to_regclass('credential_maintenance.'||v.view_name),false)
     IS DISTINCT FROM pg_get_viewdef('pg_temp.cm_expected_view'::regclass,false) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_manifest_invalid';
   END IF;
   DROP VIEW pg_temp.cm_expected_view RESTRICT;
 END LOOP;
 FOR t IN SELECT * FROM cm_triggers LOOP
   SELECT coalesce(array_agg(a.attnum ORDER BY n),ARRAY[]::smallint[]) INTO expected_attrs
     FROM unnest(t.columns) WITH ORDINALITY x(column_name,n) JOIN pg_catalog.pg_attribute a
     ON a.attrelid=t.relation::regclass AND a.attname=x.column_name;
   IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger x WHERE x.tgrelid=t.relation::regclass
     AND x.tgname=t.trigger_name AND NOT x.tgisinternal AND x.tgenabled='O' AND x.tgtype=t.type_bits
     AND x.tgnargs=0 AND x.tgqual IS NULL AND x.tgconstraint=0
     AND x.tgfoid=to_regprocedure('credential_maintenance.'||t.function_name||'()')
     AND ARRAY(SELECT n FROM unnest(x.tgattr) n)=expected_attrs) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_trigger_invalid';
   END IF;
 END LOOP;
 IF (SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal AND tgrelid IN
   ('public.lk_user_wb_tokens'::regclass,'public.lk_user_avito_credentials'::regclass))<>4 THEN
   RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_source_state_invalid';
 END IF;
 FOR p IN SELECT m.*,r.role_id,r.role_name FROM cm_policies m JOIN cm_roles r USING(purpose) LOOP
   SELECT * INTO actual_policy FROM pg_catalog.pg_policy WHERE polrelid=p.relation::regclass AND polname=p.policy_name;
   IF NOT FOUND OR actual_policy.polroles<>ARRAY[p.role_id] OR actual_policy.polpermissive=p.restrictive
     OR actual_policy.polcmd<>CASE p.command WHEN 'SELECT' THEN 'r' WHEN 'INSERT' THEN 'a' ELSE 'w' END THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_policy_invalid';
   END IF;
   -- Parse static policy against an empty LIKE table to compare deparsed trees.
   EXECUTE format('CREATE TEMP TABLE cm_expected_policy (LIKE %s)',p.relation);
   EXECUTE format('CREATE POLICY cm_expected ON pg_temp.cm_expected_policy FOR %s TO %I %s %s',p.command,p.role_name,
     CASE WHEN p.using_sql IS NULL THEN '' ELSE 'USING ('||replace(p.using_sql,split_part(p.relation,'.',2)||'.','cm_expected_policy.')||')' END,
     CASE WHEN p.check_sql IS NULL THEN '' ELSE 'WITH CHECK ('||replace(p.check_sql,split_part(p.relation,'.',2)||'.','cm_expected_policy.')||')' END);
   IF replace(pg_get_expr(actual_policy.polqual,actual_policy.polrelid),split_part(p.relation,'.',2)||'.','cm_expected_policy.')
       IS DISTINCT FROM (SELECT pg_get_expr(polqual,polrelid) FROM pg_catalog.pg_policy WHERE polrelid='pg_temp.cm_expected_policy'::regclass)
     OR replace(pg_get_expr(actual_policy.polwithcheck,actual_policy.polrelid),split_part(p.relation,'.',2)||'.','cm_expected_policy.')
       IS DISTINCT FROM (SELECT pg_get_expr(polwithcheck,polrelid) FROM pg_catalog.pg_policy WHERE polrelid='pg_temp.cm_expected_policy'::regclass) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_policy_invalid';
   END IF;
   DROP TABLE pg_temp.cm_expected_policy RESTRICT;
 END LOOP;
 PERFORM pg_temp.cm_assert_grants();
END $fn$;

DO $transition$
DECLARE f record; v record; t record; g cm_grants; p record; r record; a record; recipient text; col_sql text;
BEGIN
 IF (SELECT action='provision' FROM cm_input) THEN
   PERFORM credential_maintenance.assert_inert();
   IF EXISTS (SELECT 1 FROM pg_catalog.pg_trigger WHERE NOT tgisinternal AND tgrelid IN
     ('public.lk_user_wb_tokens'::regclass,'public.lk_user_avito_credentials'::regclass))
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_policy p JOIN cm_policies m ON p.polrelid=to_regclass(m.relation) AND p.polname=m.policy_name)
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d JOIN cm_roles r ON d.refobjid=r.role_id
       WHERE r.purpose<>'schema' AND d.refclassid='pg_authid'::regclass AND d.deptype IN ('o','a','r')) THEN
     RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='maintenance_not_pristine';
   END IF;
   FOR r IN SELECT * FROM cm_roles WHERE purpose IN ('helper','invalidator') LOOP
     EXECUTE format('GRANT CREATE ON SCHEMA credential_maintenance TO %I',r.role_name);
   END LOOP;
   FOR f IN SELECT m.*,r.role_name FROM cm_functions m JOIN cm_roles r USING(purpose) WHERE m.definer LOOP
     EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f.signature,f.role_name);
     -- Ownership is final before any function is made a definer.
     EXECUTE format('ALTER FUNCTION %s SECURITY DEFINER',f.signature);
   END LOOP;
   FOR p IN SELECT m.*,r.role_name FROM cm_policies m JOIN cm_roles r USING(purpose) LOOP
     EXECUTE format('CREATE POLICY %I ON %s AS %s FOR %s TO %I %s %s',p.policy_name,p.relation,
       CASE WHEN p.restrictive THEN 'RESTRICTIVE' ELSE 'PERMISSIVE' END,p.command,p.role_name,
       CASE WHEN p.using_sql IS NULL THEN '' ELSE 'USING ('||p.using_sql||')' END,
       CASE WHEN p.check_sql IS NULL THEN '' ELSE 'WITH CHECK ('||p.check_sql||')' END);
   END LOOP;
   -- Owner needs underlying column grants before CREATE VIEW under its identity.
   FOR g IN SELECT * FROM cm_grants WHERE purpose IN ('helper','invalidator') LOOP PERFORM pg_temp.cm_grant('GRANT',g); END LOOP;
   FOR v IN SELECT * FROM cm_views ORDER BY CASE view_name WHEN 'authorized_metadata' THEN 0 ELSE 1 END,view_name LOOP
     EXECUTE format('SET LOCAL ROLE %I',(SELECT role_name FROM cm_roles WHERE purpose='helper'));
     EXECUTE format('CREATE VIEW credential_maintenance.%I WITH (security_barrier=true) AS %s',v.view_name,v.definition);
     RESET ROLE;
   END LOOP;
   -- Scrub creator default ACLs on exactly these new views and all transferred
   -- private functions, including grant options, before caller grants exist.
   FOR v IN SELECT c.oid,c.relowner,c.relname,c.relacl FROM pg_catalog.pg_class c
     JOIN cm_views m ON c.relname=m.view_name WHERE c.relnamespace='credential_maintenance'::regnamespace LOOP
     FOR a IN SELECT DISTINCT grantee FROM aclexplode(v.relacl) WHERE grantee<>v.relowner LOOP
       recipient := CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
       EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE credential_maintenance.%I FROM %s RESTRICT',v.relname,recipient);
     END LOOP;
     FOR p IN SELECT attname,attacl FROM pg_catalog.pg_attribute WHERE attrelid=v.oid AND attnum>0 AND attacl IS NOT NULL LOOP
       FOR a IN SELECT DISTINCT grantee FROM aclexplode(p.attacl) WHERE grantee<>v.relowner LOOP
         recipient := CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
         EXECUTE format('REVOKE ALL PRIVILEGES (%I) ON TABLE credential_maintenance.%I FROM %s RESTRICT',p.attname,v.relname,recipient);
       END LOOP;
     END LOOP;
   END LOOP;
   FOR f IN SELECT p.* FROM pg_catalog.pg_proc p JOIN cm_functions m ON p.oid=to_regprocedure(m.signature) LOOP
     FOR a IN SELECT DISTINCT grantee FROM aclexplode(coalesce(f.proacl,acldefault('f',f.proowner))) WHERE grantee<>f.proowner LOOP
       recipient := CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
       EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %s RESTRICT',f.oid::regprocedure,recipient);
     END LOOP;
   END LOOP;
   FOR t IN SELECT * FROM cm_triggers LOOP
     SELECT string_agg(quote_ident(x),',' ORDER BY n) INTO col_sql FROM unnest(t.columns) WITH ORDINALITY v(x,n);
     EXECUTE format('CREATE TRIGGER %I AFTER %s ON %s FOR EACH ROW EXECUTE FUNCTION credential_maintenance.%I()',
       t.trigger_name,CASE WHEN t.type_bits=9 THEN 'DELETE' ELSE 'UPDATE OF '||col_sql END,t.relation,t.function_name);
   END LOOP;
   FOR r IN SELECT * FROM cm_roles WHERE purpose IN ('helper','invalidator') LOOP
     EXECUTE format('REVOKE CREATE ON SCHEMA credential_maintenance FROM %I RESTRICT',r.role_name);
   END LOOP;
   -- Only now expose reviewed runner and registrar entry points.
   FOR g IN SELECT * FROM cm_grants WHERE purpose IN ('runner','registrar') LOOP PERFORM pg_temp.cm_grant('GRANT',g); END LOOP;
   PERFORM pg_temp.cm_assert_active();
 ELSE
   PERFORM pg_temp.cm_assert_active();
   FOR g IN SELECT * FROM cm_grants WHERE purpose IN ('runner','registrar') LOOP PERFORM pg_temp.cm_grant('REVOKE',g); END LOOP;
   FOR t IN SELECT * FROM cm_triggers LOOP EXECUTE format('DROP TRIGGER %I ON %s RESTRICT',t.trigger_name,t.relation); END LOOP;
   FOR p IN SELECT * FROM cm_policies LOOP EXECUTE format('DROP POLICY %I ON %s',p.policy_name,p.relation); END LOOP;
   FOR v IN SELECT * FROM cm_views ORDER BY CASE view_name WHEN 'authorized_metadata' THEN 1 ELSE 0 END,view_name LOOP
     EXECUTE format('DROP VIEW credential_maintenance.%I RESTRICT',v.view_name);
   END LOOP;
   FOR f IN SELECT * FROM cm_functions WHERE definer LOOP
     EXECUTE format('ALTER FUNCTION %s SECURITY INVOKER',f.signature);
     EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f.signature,(SELECT role_name FROM cm_roles WHERE purpose='schema'));
   END LOOP;
   FOR g IN SELECT * FROM cm_grants WHERE purpose IN ('helper','invalidator') LOOP PERFORM pg_temp.cm_grant('REVOKE',g); END LOOP;
   PERFORM credential_maintenance.assert_inert();
 END IF;
END $transition$;
COMMIT;
