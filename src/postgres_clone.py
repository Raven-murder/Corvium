import re

from .db_schema import TABLES, table_definitions
from .db_sql import split_list

_MAP = "corvium_clone_ids"
_JSON = """
CREATE OR REPLACE FUNCTION pg_temp.corvium_clone_json(input_value jsonb, target_workspace text, entry_key text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql AS $cv$
DECLARE
    value_kind text := jsonb_typeof(input_value);
    output_value jsonb;
    item record;
    replacement_id bigint;
BEGIN
    IF entry_key = 'doc_id' AND value_kind = 'number' AND input_value::text ~ '^[0-9]+$' THEN
        SELECT new_id INTO replacement_id FROM pg_temp.corvium_clone_ids
        WHERE workspace_id = target_workspace AND table_name = 'documents'
          AND old_id::numeric = input_value::text::numeric;
        RETURN COALESCE(to_jsonb(replacement_id), input_value);
    ELSIF entry_key IN ('doc_ids', 'source_doc_ids') AND value_kind = 'array' THEN
        output_value := '[]'::jsonb;
        FOR item IN SELECT value FROM jsonb_array_elements(input_value) LOOP
            output_value := output_value || jsonb_build_array(pg_temp.corvium_clone_json(item.value, target_workspace, 'doc_id'));
        END LOOP;
        RETURN output_value;
    ELSIF value_kind = 'array' THEN
        output_value := '[]'::jsonb;
        FOR item IN SELECT value FROM jsonb_array_elements(input_value) LOOP
            output_value := output_value || jsonb_build_array(pg_temp.corvium_clone_json(item.value, target_workspace));
        END LOOP;
        RETURN output_value;
    ELSIF value_kind = 'object' THEN
        output_value := '{}'::jsonb;
        FOR item IN SELECT key, value FROM jsonb_each(input_value) LOOP
            output_value := output_value || jsonb_build_object(item.key, pg_temp.corvium_clone_json(item.value, target_workspace, item.key));
        END LOOP;
        RETURN output_value;
    END IF;
    RETURN input_value;
END
$cv$
"""
_NOTE = r"""
CREATE OR REPLACE FUNCTION pg_temp.corvium_clone_note(input_body text, target_workspace text)
RETURNS text LANGUAGE plpgsql AS $cv$
DECLARE
    parts text[];
    remaining text := input_body;
    output_value text := '';
    token text;
    position_value integer;
    replacement_id bigint;
BEGIN
    FOR parts IN SELECT regexp_matches(input_body, $pattern$(!\[[^\]\n]*\]\(diagram:([1-9][0-9]{0,18})\))$pattern$, 'g') LOOP
        token := parts[1];
        position_value := strpos(remaining, token);
        SELECT new_id INTO replacement_id FROM pg_temp.corvium_clone_ids
        WHERE workspace_id = target_workspace AND table_name = 'diagrams'
          AND old_id::numeric = parts[2]::numeric;
        output_value := output_value || substr(remaining, 1, position_value - 1);
        IF replacement_id IS NULL THEN
            output_value := output_value || token;
        ELSE
            output_value := output_value || left(token, length(token) - length(parts[2]) - 1) || replacement_id::text || ')';
        END IF;
        remaining := substr(remaining, position_value + length(token));
    END LOOP;
    RETURN output_value || remaining;
END
$cv$
"""


def clone_rows(connection, source_id, target_id):
    """Copy payloads and remap every ID on the server within the caller's transaction."""
    connection._run(f"CREATE TEMP TABLE {_MAP} (workspace_id TEXT NOT NULL, table_name TEXT NOT NULL, old_id BIGINT NOT NULL, new_id BIGINT NOT NULL, PRIMARY KEY(workspace_id, table_name, old_id)) ON COMMIT DROP")
    connection._run(_JSON)
    connection._run(_NOTE)
    definitions = table_definitions("postgres")
    parents = {"folder_id": "folders", "doc_id": "documents", "chat_id": "chats"}
    for table in TABLES:
        columns = [match[1] for declaration in split_list(definitions[table])
                   if (match := re.match(r"([a-z_][a-z_0-9]*)\s", declaration)) and match[1] not in {"id", "workspace_id"}]
        joins, values = [], []
        for column in columns:
            if column in parents:
                parent = parents[column]
                joins.append(f"LEFT JOIN pg_temp.{_MAP} ref_{column} ON ref_{column}.workspace_id = scope.target_id AND ref_{column}.table_name = '{parent}' AND ref_{column}.old_id = s.{column}")
                values.append(f"ref_{column}.new_id")
            elif table == "messages" and column == "session_id":
                values.append("COALESCE(ref_chat_id.new_id::text, s.session_id)")
            elif (table == "messages" and column == "sources") or (table == "plans" and column == "body_json"):
                values.append(f"pg_temp.corvium_clone_json(s.{column}::jsonb, scope.target_id)::text")
            elif table == "notes" and column == "body":
                values.append("pg_temp.corvium_clone_note(s.body, scope.target_id)")
            elif table == "documents" and column == "stored_path":
                values.append("NULL")
            else:
                values.append("s." + column)
        sql = f"""WITH scope AS (SELECT ?::text AS source_id, ?::text AS target_id),
            allocated AS MATERIALIZED (
                SELECT s.id AS old_id, nextval(pg_get_serial_sequence('public.{table}', 'id')) AS new_id
                FROM {table} s CROSS JOIN scope WHERE s.workspace_id = scope.source_id
            ), remembered AS (
                INSERT INTO pg_temp.{_MAP}(workspace_id, table_name, old_id, new_id)
                SELECT scope.target_id, '{table}', allocated.old_id, allocated.new_id FROM allocated CROSS JOIN scope
                RETURNING old_id, new_id
            ) INSERT INTO {table}(id, workspace_id, {', '.join(columns)})
              SELECT remembered.new_id, scope.target_id, {', '.join(values)}
              FROM {table} s JOIN remembered ON remembered.old_id = s.id CROSS JOIN scope
              {' '.join(joins)} WHERE s.workspace_id = scope.source_id"""
        connection._run(sql, (source_id, target_id))
