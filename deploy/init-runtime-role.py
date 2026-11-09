"""VPS-only role grants after migration; no credentials printed or persisted."""
import os
import psycopg
from psycopg import sql

password = os.environ['POSTGRES_PASSWORD']
if len(password) < 32 or not all(char in '0123456789abcdefABCDEF' for char in password):
    raise RuntimeError('Use a random hexadecimal application DB password of at least 32 characters')
with psycopg.connect(os.environ['DATABASE_ADMIN_DSN']) as connection:
    exists = connection.execute("SELECT rolname FROM pg_roles WHERE rolname = 'satorna'").fetchone()
    action = 'ALTER' if exists else 'CREATE'
    connection.execute(sql.SQL(action + ' ROLE satorna LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Literal(password)))
    connection.execute('GRANT CONNECT ON DATABASE satorna TO satorna')
    connection.execute('GRANT USAGE ON SCHEMA public TO satorna')
    connection.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO satorna')
    connection.execute('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO satorna')
    connection.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO satorna')
    connection.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO satorna')
print('Application database role ready: non-superuser, no RLS bypass.')
