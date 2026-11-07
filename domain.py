"""Writes shared by the HTTP API and the broker consumer."""
from uuid import uuid4
from psycopg.types.json import Jsonb

def add_event(connection, event_type, payload, correlation_id=None):
    """Write in the caller's transaction so state and event commit together (ADR-0021)."""
    connection.execute("INSERT INTO outbox_events(id,event_type,correlation_id,payload) VALUES (%s,%s,%s,%s)",
                       (uuid4(), event_type, correlation_id or uuid4(), Jsonb(payload)))

def audit(connection, moderator_id, action, target_id, detail=None):
    connection.execute("INSERT INTO audit_log(id,moderator_id,action,target_id,detail) VALUES (%s,%s,%s,%s,%s)",
                       (uuid4(), moderator_id, action, target_id, detail))

def grant_badge(connection, runner_id, correlation_id=None):
    """Idempotent: a redelivered PhoneVerified neither duplicates the event nor undoes a rejection."""
    granted = connection.execute("INSERT INTO verifications(runner_id,status) VALUES (%s,'verified') ON CONFLICT DO NOTHING RETURNING runner_id",
                                 (runner_id,)).fetchone()
    if granted:
        add_event(connection, "RunnerVerified", {"runnerId": str(runner_id)}, correlation_id)
    return bool(granted)
