import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.interface import EventBus
from checkcord.core.config import get_settings
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.reranker import RerankCandidate, Reranker, build_reranker
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__)


class ClusteringWorker:
    """Finds records similar to each pending event and passes them to linkage."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine,
        # 0.70 measured precision 0.857, recall 0.682 on the golden set with
        # voyage-code-3 (scripts/eval_clustering.py); 0.85 missed almost every
        # true duplicate pair.
        similarity_threshold: float = 0.70,
        group_name: str = "clustering_workers",
        consumer_name: str = "clustering-worker-1",
        reranker: Reranker | None = None,
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.similarity_threshold = similarity_threshold
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.stop_event = asyncio.Event()
        self.settings = get_settings()
        # An injected reranker wins; otherwise built from settings, which gives
        # None when reranking is off.
        self.reranker: Reranker | None = reranker or build_reranker(self.settings)

    async def process_message(self, event: CanonicalEvent) -> None:
        """Processes an event, runs the vector similarity search, and publishes matches."""
        log = logger.bind(
            tenant_id=str(event.tenant_id),
            event_id=str(event.event_id),
        )

        embedding = event.metadata.get("embedding")
        if not embedding or not isinstance(embedding, list):
            log.warning("clustering.skipping_event_no_embedding")
            return

        log.info("clustering.processing_event")

        try:
            # Search PostgreSQL for similar contexts under tenant isolation
            async with tenant_context(self.engine, event.tenant_id, include_held=True) as conn:
                store = PostgresEventStore(conn)
                # A held event is compared only with records held under its connection,
                # a live one only with live records.
                stored, held = await store.get_event_hold(event.tenant_id, event.event_id)
                if not stored:
                    log.warning("clustering.skipping_event_not_stored")
                    return
                # Match only org-global records and records scoped to this event's
                # repo, so an insight from one repo never merges into another repo's
                # record. Sources are not needed here, so skip that query.
                repo_identifier = event.code_metadata.repository if event.code_metadata else None
                # The cap bounds what gets reranked; with reranking off the fetch
                # stays unbounded.
                if self.reranker is not None:
                    similar_rows = await store.find_similar_contexts(
                        tenant_id=event.tenant_id,
                        embedding=embedding,
                        threshold=self.similarity_threshold,
                        repo_identifier=repo_identifier,
                        include_sources=False,
                        limit=self.settings.rerank_candidate_pool_size,
                        held_connection=held,
                    )
                else:
                    similar_rows = await store.find_similar_contexts(
                        tenant_id=event.tenant_id,
                        embedding=embedding,
                        threshold=self.similarity_threshold,
                        repo_identifier=repo_identifier,
                        include_sources=False,
                        held_connection=held,
                    )

            # Rerank changes order only. A failure keeps the cosine-distance
            # order rather than failing the event.
            if self.reranker is not None and similar_rows:
                try:
                    reranked = await self.reranker.rerank(
                        query=event.content_clean,
                        candidates=[
                            RerankCandidate(id=str(row["id"]), text=row["summary"])
                            for row in similar_rows
                        ],
                    )
                    rank_of = {rc.id: i for i, rc in enumerate(reranked)}
                    similar_rows = sorted(
                        similar_rows,
                        key=lambda row: rank_of.get(str(row["id"]), len(rank_of)),
                    )
                except Exception as e:
                    log.warning("clustering.rerank_failed_falling_back", error=str(e))

            matches = [
                {
                    "context_id": str(row["id"]),
                    "summary": row["summary"],
                    "confidence_score": float(row["confidence_score"]),
                    "similarity": float(row["similarity"]),
                }
                for row in similar_rows
            ]

            # Inject matches into event metadata
            updated_event = event.model_copy(
                update={"metadata": {**event.metadata, "clustering_matches": matches}}
            )

            await self.bus.publish("events.linkage.pending", updated_event)
            log.info("clustering.matches_published", match_count=len(matches))

        except Exception as e:
            log.exception("clustering.processing_error", error=str(e))
            raise

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Background task running the Redis subscription loop."""
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
            )
        except asyncio.CancelledError:
            logger.info("clustering.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("clustering.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        """Starts the subscription and blocks until stopped."""
        logger.info("clustering.starting", topics=topics, group=self.group_name)

        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        await self.stop_event.wait()
        logger.info("clustering.shutdown_initiated")

        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("clustering.shutdown_complete")

    def stop(self) -> None:
        """Triggers graceful shutdown."""
        self.stop_event.set()
