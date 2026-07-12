import asyncio
import os
import unittest
import uuid
from types import SimpleNamespace

from videosim.migrations import PostgresMigrator
from videosim.nats_publisher import (
    STREAM_DUPLICATE_WINDOW_SECONDS,
    STREAM_MAX_AGE_SECONDS,
    STREAM_NAME,
    STREAM_SUBJECTS,
    NatsPublisherError,
    ensure_event_stream,
    publish_outbox_once,
    validate_event_stream_config,
)
from videosim.postgres_store import PostgresControlPlaneStore


DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")
NATS_URL = os.environ.get("VIDEOSIM_TEST_NATS_URL", "")


class NatsStreamConfigurationTest(unittest.TestCase):
    def valid_config(self, **overrides):
        values = {
            "storage": "file",
            "retention": "limits",
            "subjects": list(STREAM_SUBJECTS),
            "max_age": STREAM_MAX_AGE_SECONDS,
            "duplicate_window": STREAM_DUPLICATE_WINDOW_SECONDS,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_accepts_exact_durable_configuration(self):
        validate_event_stream_config(self.valid_config())

    def test_rejects_retention_or_deduplication_drift(self):
        for override in (
            {"retention": "interest"},
            {"max_age": 60},
            {"duplicate_window": 1},
            {"subjects": ["videosim.audit.v1"]},
        ):
            with self.subTest(override=override):
                with self.assertRaises(NatsPublisherError):
                    validate_event_stream_config(self.valid_config(**override))


@unittest.skipUnless(DATABASE_URL and NATS_URL, "PostgreSQL and NATS integration URLs are not configured")
class NatsOutboxIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        PostgresMigrator(DATABASE_URL).apply()
        asyncio.run(ensure_event_stream(NATS_URL))
        cls.store = PostgresControlPlaneStore(DATABASE_URL, min_pool_size=1, max_pool_size=2)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_committed_outbox_event_publishes_to_jetstream_once(self):
        event_id = uuid.uuid4()
        event_payload = {"eventId": str(event_id), "action": "integration"}
        self.store.enqueue_outbox(event_id, "videosim.audit.v1", event_payload)

        summary = asyncio.run(
            publish_outbox_once(
                self.store,
                NATS_URL,
                publisher_id=f"publisher-{uuid.uuid4()}",
                limit=10,
                event_ids=[event_id],
            )
        )
        with self.store._pool.connection() as connection:
            first_row = connection.execute(
                "SELECT state, broker_sequence FROM outbox WHERE event_id = %s",
                (event_id,),
            ).fetchone()
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE outbox SET state = 'pending', next_attempt_at = clock_timestamp(),
                                      publisher_id = NULL, locked_at = NULL
                    WHERE event_id = %s
                    """,
                    (event_id,),
                )
        replay = asyncio.run(
            publish_outbox_once(
                self.store,
                NATS_URL,
                publisher_id=f"publisher-{uuid.uuid4()}",
                limit=10,
                event_ids=[event_id],
            )
        )
        self.store.enqueue_outbox(event_id, "videosim.audit.v1", event_payload)
        third = asyncio.run(
            publish_outbox_once(
                self.store,
                NATS_URL,
                publisher_id=f"publisher-{uuid.uuid4()}",
                limit=10,
                event_ids=[event_id],
            )
        )

        self.assertEqual(summary.claimed, 1)
        self.assertEqual(summary.published, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(replay.published, 1)
        self.assertEqual(third.claimed, 0)
        with self.store._pool.connection() as connection:
            row = connection.execute(
                "SELECT state, broker_sequence FROM outbox WHERE event_id = %s",
                (event_id,),
            ).fetchone()
        self.assertEqual(row["state"], "published")
        self.assertGreater(row["broker_sequence"], 0)
        self.assertEqual(row["broker_sequence"], first_row["broker_sequence"])

    def test_jetstream_configuration_is_durable_file_storage(self):
        async def info():
            import nats

            client = await nats.connect(NATS_URL)
            try:
                return await client.jetstream().stream_info(STREAM_NAME)
            finally:
                await client.drain()

        stream = asyncio.run(info())

        self.assertEqual(stream.config.name, STREAM_NAME)
        self.assertEqual(str(stream.config.storage).lower(), "file")
        self.assertEqual(set(stream.config.subjects), set(STREAM_SUBJECTS))
        self.assertEqual(stream.config.retention, "limits")
        self.assertEqual(stream.config.max_age, STREAM_MAX_AGE_SECONDS)
        self.assertEqual(
            stream.config.duplicate_window,
            STREAM_DUPLICATE_WINDOW_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
