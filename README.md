## Storage

SQLite is the default and keeps a separate database per workspace. Set `DATABASE_URL`
in the environment or Streamlit secrets to use persistent Postgres instead. A configured
Postgres backend never falls back to SQLite when unavailable. Install the packages in
`requirements.txt` in the deployment environment; the application role must own its
schema tables or have `BYPASSRLS`. Tables have row-level security enabled without public
policies, so Supabase's public REST roles cannot read them.

Original files and float32 embeddings live in the database. Uploads are limited to
25 MB per file and 80 MB per workspace, including embeddings. The Postgres upload
soft limit is `DB_SOFT_LIMIT_MB` (400 MB by default). Inactive workspaces are removed
after `WORKSPACE_RETENTION_DAYS` (30 days by default). Existing 24-hour chat, tool,
upload and global API limits still apply. New workspaces clone the demo template
without per-visitor embedding or OCR calls.

Anyone holding a workspace link can open that workspace. Keep the link private.
Run `python tools/db_check.py` manually to check a configured backend with temporary
workspaces; the checker cleans up its own workspaces and never prints credentials.
