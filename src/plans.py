from datetime import datetime, timezone
import json

from .db import database_session


def _title(title):
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Plan title must not be empty.")
    return " ".join(title.split())[:120]


def _plan(connection, plan_id):
    row = connection.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise ValueError("Study plan does not exist.")
    return row


def _record(row):
    plan = dict(row)
    plan["body"] = json.loads(plan["body_json"])
    plan["pinned"] = bool(plan["pinned"])
    return plan


def create_plan(title, body, days):
    title = _title(title)
    if type(days) is not int or days < 1:
        raise ValueError("Choose a positive number of study days.")
    if not isinstance(body, dict) or not isinstance(body.get("markdown"), str) or not body["markdown"].strip():
        raise ValueError("Study plan must contain text.")
    body_json = json.dumps(body, ensure_ascii=False)
    with database_session(write=True) as connection:
        return connection.execute(
            'INSERT INTO plans(title, body_json, days, created_at, pinned) VALUES (?, ?, ?, ?, 0) RETURNING id',
            (title, body_json, days, datetime.now(timezone.utc).isoformat()),
        ).fetchone()["id"]


def list_plans():
    with database_session() as connection:
        return [_record(row) for row in connection.execute(
            "SELECT * FROM plans ORDER BY pinned DESC, created_at DESC, id DESC"
        )]


def get_plan(plan_id):
    with database_session() as connection:
        return _record(_plan(connection, plan_id))


def rename_plan(plan_id, title):
    title = _title(title)
    with database_session(write=True) as connection:
        _plan(connection, plan_id)
        connection.execute("UPDATE plans SET title = ? WHERE id = ?", (title, plan_id))


def pin_plan(plan_id, pinned=True):
    with database_session(write=True) as connection:
        _plan(connection, plan_id)
        if pinned:
            connection.execute("UPDATE plans SET pinned = 0 WHERE pinned != 0")
        connection.execute("UPDATE plans SET pinned = ? WHERE id = ?", (int(bool(pinned)), plan_id))


def delete_plan(plan_id):
    with database_session(write=True) as connection:
        _plan(connection, plan_id)
        connection.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
