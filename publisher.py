"""Outbox publisher: unsent or unrouted events stay unpublished and are retried (ADR-0021)."""
import json
import logging
import os
import time
import httpx
from common import db, migrate
logging.basicConfig(level=logging.INFO)
log=logging.getLogger(__name__)

def envelope(row):
    return {"eventId":str(row["id"]),"eventType":row["event_type"],"version":1,"occurredAt":row["occurred_at"].isoformat(),
      "correlationId":str(row["correlation_id"]),"payload":row["payload"]}

def publish(row):
    response=httpx.post(os.environ["BROKER_HTTP_URL"]+"/api/exchanges/%2F/cra.events/publish",
      auth=(os.environ["BROKER_USER"],os.environ["BROKER_PASSWORD"]),timeout=5,
      json={"properties":{"delivery_mode":2,"content_type":"application/json"},"routing_key":row["event_type"],
            "payload":json.dumps(envelope(row)),"payload_encoding":"string"})
    response.raise_for_status()
    if not response.json()["routed"]:
        raise RuntimeError("broker did not route "+row["event_type"])

def publish_batch():
    with db() as connection:
        rows=connection.execute("SELECT * FROM outbox_events WHERE published_at IS NULL ORDER BY occurred_at LIMIT 20 FOR UPDATE SKIP LOCKED").fetchall()
        for row in rows:
            publish(row)
            connection.execute("UPDATE outbox_events SET published_at=now() WHERE id=%s",(row["id"],))
            connection.commit()

def main():
    migrate()
    while True:
        try:
            publish_batch()
        except Exception as error:
            log.warning("Outbox retry: %s",error)
        time.sleep(2)

if __name__=="__main__":
    main()
