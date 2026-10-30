"""Perform scheduled read-only database keepalive queries."""

import logging
import os
import time


RETRY_DELAYS = (5, 15, 30)


def main():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is not set")
        return 1

    logging.disable(logging.CRITICAL)
    try:
        import psycopg

        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                with psycopg.connect(
                    database_url,
                    connect_timeout=20,
                    options="-c statement_timeout=15000",
                ) as connection:
                    connection.read_only = True
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT count(*) FROM public.workspaces")
                        workspace_count = int(cursor.fetchone()[0])
                        cursor.execute("SELECT COALESCE(max(version), 0) FROM public.schema_version")
                        schema_version = int(cursor.fetchone()[0])
                print("ok", workspace_count, schema_version)
                return 0
            except Exception:
                if attempt == len(RETRY_DELAYS):
                    raise
                time.sleep(RETRY_DELAYS[attempt])
    except Exception as error:
        print(type(error).__name__, "Database keepalive failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
