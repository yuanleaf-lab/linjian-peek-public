"""Explicit PostgreSQL migration runner; never called during server startup."""
import os
import re
from pathlib import Path

from postgres_proactive_store import _default_connection_factory

MIGRATIONS = Path(__file__).resolve().parent / 'migrations' / 'postgres'


def migrate(database_url, connection_factory=None, migration_dir=MIGRATIONS):
    if not database_url or not database_url.startswith(('postgresql://', 'postgres://')):
        raise ValueError('postgres_database_url_required')
    files = sorted(Path(migration_dir).glob('*.sql'))
    versions = []
    for path in files:
        match = re.fullmatch(r'(\d+)_[-a-zA-Z0-9]+\.sql', path.name)
        if not match:
            raise ValueError('invalid_migration_filename')
        version = int(match.group(1))
        if version in versions:
            raise ValueError('duplicate_migration_version')
        versions.append(version)
    factory = connection_factory or _default_connection_factory
    applied = []
    for version, path in zip(versions, files):
        # One transaction per version. Advisory lock prevents two runners racing.
        with factory(database_url) as conn, conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (715081193,))
            cur.execute("SELECT to_regclass('schema_migrations') AS table_name")
            exists = cur.fetchone()['table_name'] is not None
            if exists:
                cur.execute('SELECT version FROM schema_migrations ORDER BY version')
                existing = {row['version'] for row in cur.fetchall()}
                if existing - set(versions):
                    raise RuntimeError('postgres_schema_newer_than_code')
            else:
                existing = set()
            if version in existing:
                continue
            sql = path.read_text(encoding='utf-8')
            if re.search(r'\b(BEGIN|COMMIT|ROLLBACK)\s*;', sql, re.IGNORECASE):
                raise ValueError('migration_contains_transaction_control')
            cur.execute(sql)
            cur.execute('INSERT INTO schema_migrations(version) VALUES(%s)', (version,))
            applied.append(version)
    return applied


if __name__ == '__main__':
    print('Applied migrations:', migrate(os.environ.get('DATABASE_URL', '')))
