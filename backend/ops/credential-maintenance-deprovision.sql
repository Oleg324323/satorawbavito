-- INERT SOURCE ARTIFACT. Explicit empty-only teardown, no operational execution.
-- Supply the identical database and five exact role OID/name inputs plus complete
-- runtime-role pair JSON documented in credential-maintenance-provision.sql.
-- The shared transaction validates exact active owners/views/policies/ACLs/source
-- triggers, fixed WB→Avito→targets→authorizations NOWAIT locks, and unfiltered
-- emptiness of BOTH metadata tables before removing any capability.
-- Any retained target/authorization, partial activation, foreign dependency,
-- privilege mismatch or contention aborts. No wait/retry, role deletion, cascade,
-- DROP OWNED, metadata/source/ciphertext deletion, suspension or history cleanup.
\set ON_ERROR_STOP on
\set cm_action deprovision
\ir credential-maintenance-provision.sql
