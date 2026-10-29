import re

from .config import StorageError
from .db_schema import TABLES


class Row(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def postgres_sql(sql):
    result, quote, index = [], None, 0
    while index < len(sql):
        char = sql[index]
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                if index + 1 < len(sql) and sql[index + 1] == char:
                    result.extend((char, char))
                    index += 2
                    continue
                quote = None
        result.append("%%" if char == "%" else "%s" if char == "?" and quote is None else char)
        index += 1
    return "".join(result)


def split_list(value):
    parts, start, quote, depth, index = [], 0, None, 0, 0
    while index < len(value):
        char = value[index]
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                if index + 1 < len(value) and value[index + 1] == char:
                    index += 2
                    continue
                quote = None
        elif quote is None:
            depth += char == "("
            depth -= char == ")"
            if char == "," and depth == 0:
                parts.append(value[start:index].strip())
                start = index + 1
        index += 1
    return parts + [value[start:].strip()]


def parameter_count(sql):
    count, quote, index = 0, None, 0
    while index < len(sql):
        char = sql[index]
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                if index + 1 < len(sql) and sql[index + 1] == char:
                    index += 2
                    continue
                quote = None
        elif char == "?" and quote is None:
            count += 1
        index += 1
    return count


def values_for(columns, expressions, params):
    result, offset = {}, 0
    for name, expression in zip(columns, expressions):
        count = parameter_count(expression)
        if expression == "?":
            result[name] = params[offset]
        elif expression.upper() == "NULL":
            result[name] = None
        elif re.fullmatch(r"-?\d+", expression):
            result[name] = int(expression)
        elif expression.startswith("'") and expression.endswith("'"):
            result[name] = expression[1:-1].replace("''", "'")
        offset += count
    return result


def scoped_sql(sql, params, workspace_id, dialect):
    """Apply scope to every table, including joins and nested source queries."""
    sql, params = sql.strip().rstrip(";"), tuple(params or ())
    operation = sql.split(None, 1)[0].upper() if sql else ""
    if operation == "SELECT":
        names = list(dict.fromkeys(name.lower() for name in re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z_0-9]*)", sql, re.I)))
        scoped = [name for name in names if name in TABLES or name in {"workspaces", "usage_events"}]
        if any(name not in (*TABLES, "workspaces", "usage_events", "schema_version", "sqlite_master") for name in names):
            raise StorageError()
        schema = "public" if dialect == "postgres" else "main"
        ctes = [f"{name} AS (SELECT * FROM {schema}.{name} WHERE {'id' if name == 'workspaces' else 'workspace_id'} = ?)" for name in scoped]
        if ctes:
            sql = "WITH " + ", ".join(ctes) + " " + sql
            params = (workspace_id,) * len(ctes) + params
        return sql, params, operation, None, {}
    if operation == "INSERT":
        match = re.fullmatch(r"INSERT\s+INTO\s+(\w+)\s*\((.*?)\)\s*VALUES\s*\((.*)\)(.*?)", sql, re.I | re.S)
        if match is None or match.group(1).lower() not in TABLES:
            raise StorageError()
        table, columns = match.group(1).lower(), [name.lower() for name in split_list(match.group(2))]
        beginning = re.search(r"\bVALUES\s*\(", sql, re.I).end()
        quote, depth, end = None, 1, beginning
        while end < len(sql) and depth:
            char = sql[end]
            if char == "'":
                if quote and end + 1 < len(sql) and sql[end + 1] == "'":
                    end += 2
                    continue
                quote = None if quote else char
            elif quote is None:
                depth += char == "("
                depth -= char == ")"
            end += 1
        expressions, suffix = split_list(sql[beginning:end - 1]), sql[end:]
        if any(re.fullmatch(r"[a-z_][a-z_0-9]*", name) is None for name in columns) or re.search(r"\bSELECT\b", sql, re.I):
            raise StorageError()
        values = values_for(columns, expressions, params)
        if "workspace_id" in columns:
            if values.get("workspace_id") != workspace_id:
                raise ValueError("This item is not available in the current workspace.")
        else:
            columns.insert(0, "workspace_id")
            expressions.insert(0, "?")
            params = (workspace_id,) + params
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(expressions)})" + suffix
        if not re.search(r"\bRETURNING\b", suffix, re.I):
            sql += " RETURNING id"
        return sql, params, operation, table, values
    if operation in {"UPDATE", "DELETE"}:
        match = re.match(r"(?:UPDATE|DELETE\s+FROM)\s+(\w+)\b", sql, re.I)
        if match is None or match.group(1).lower() not in TABLES:
            raise StorageError()
        table = match.group(1).lower()
        if re.search(r"\bSELECT\b", sql, re.I):
            raise StorageError()
        where = re.search(r"\bWHERE\b", sql, re.I)
        suffix = re.search(r"\bRETURNING\b", sql, re.I)
        tail = sql[suffix.start():] if suffix else ""
        body = sql[:suffix.start()].rstrip() if suffix else sql
        prefix, condition = (body[:where.start()], body[where.end():]) if where else (body + " ", "1 = 1")
        before = parameter_count(prefix)
        sql = prefix + "WHERE workspace_id = ? AND (" + condition + ") " + tail
        params = params[:before] + (workspace_id,) + params[before:]
        values = {}
        if operation == "UPDATE":
            assignments = split_list(re.split(r"\bSET\b", prefix, flags=re.I)[1])
            names, expressions = zip(*(item.split("=", 1) for item in assignments))
            names = [name.strip().lower() for name in names]
            if any(re.fullmatch(r"[a-z_][a-z_0-9]*", name) is None for name in names):
                raise StorageError()
            if "workspace_id" in names:
                raise ValueError("Workspace ownership cannot be changed.")
            values = values_for([name.strip() for name in names], [item.strip() for item in expressions], params[:before])
        return sql, params, operation, table, values
    if operation == "PRAGMA" and dialect == "sqlite" and re.fullmatch(r"PRAGMA\s+(?:table_info\(\w+\)|foreign_key_check|foreign_keys)", sql, re.I):
        return sql, params, operation, None, {}
    raise StorageError()
