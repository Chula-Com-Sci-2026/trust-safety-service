"""Manual acknowledgement after commit; granting the badge is idempotent, so redelivery is safe."""
import json
import logging
import os
import time
from uuid import UUID
import pika
import psycopg
from common import db, migrate
from domain import grant_badge
logging.basicConfig(level=logging.INFO)
log=logging.getLogger(__name__)

def store(event):
    if event["version"]!=1 or event["eventType"]!="PhoneVerified":
        raise ValueError("Unsupported event schema")
    runner=UUID(event["payload"]["runnerId"])
    correlation=UUID(event["correlationId"])
    with db() as connection:
        grant_badge(connection,runner,correlation)

def main():
    migrate()
    while True:
        connection=None
        try:
            connection=pika.BlockingConnection(pika.URLParameters(os.environ["AMQP_URL"]))
            channel=connection.channel()
            channel.exchange_declare(exchange="cra.events",exchange_type="topic",durable=True)
            # A dedicated dead-letter exchange: cra.dead is a fanout, so sharing it would mix in Notification's failures.
            channel.exchange_declare(exchange="trust-safety.dead",exchange_type="fanout",durable=True)
            channel.queue_declare(queue="trust-safety.dead",durable=True)
            channel.queue_bind(queue="trust-safety.dead",exchange="trust-safety.dead")
            channel.queue_declare(queue="trust-safety.phone-verified.v1",durable=True,arguments={"x-dead-letter-exchange":"trust-safety.dead"})
            channel.queue_bind(queue="trust-safety.phone-verified.v1",exchange="cra.events",routing_key="PhoneVerified")
            channel.basic_qos(prefetch_count=10)
            def consume(ch,method,properties,body):
                try:
                    store(json.loads(body))
                except (ValueError,KeyError,TypeError):
                    log.exception("Invalid event; dead-lettering")
                    ch.basic_nack(method.delivery_tag,requeue=False)
                else:
                    ch.basic_ack(method.delivery_tag)
            channel.basic_consume(queue="trust-safety.phone-verified.v1",on_message_callback=consume)
            log.info("Trust & Safety consumer ready")
            channel.start_consuming()
        except (pika.exceptions.AMQPError,psycopg.Error):
            log.exception("Consumer disconnected; retrying in 3 seconds")
            try:
                if connection:
                    connection.close()
            except Exception:
                pass
            time.sleep(3)

if __name__=="__main__":
    main()
