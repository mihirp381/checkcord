import asyncio
import re
import uuid

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.interface import EventBus
from checkcord.llm.client import LLMClient
from checkcord.models.common import AgentMetadata
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.storage.models import Skill as DBSkill
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__)


def parse_skills_from_markdown(text: str) -> list[dict[str, str]]:
    """Parse discrete skills out of a Markdown response containing YAML frontmatter blocks."""
    # Split on '---' boundaries at start of line
    parts = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    skills = []
    i = 1
    while i < len(parts) - 1:
        fm = parts[i].strip()
        body = parts[i + 1].strip()

        # Simple key-value parser for simple YAML frontmatter
        fm_data = {}
        for line in fm.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                fm_data[key.strip().lower()] = val.strip()

        name = fm_data.get("name", "Untitled Skill")
        description = fm_data.get("description", "No description provided")

        # Keep original yaml representation
        full_instructions = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"

        skills.append(
            {
                "name": name,
                "description": description,
                "instructions": full_instructions,
            }
        )
        i += 2
    return skills


class SkillSynthesisWorker:
    """Makes skills from enriched agent events with a model and stores them."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine,
        llm_client: LLMClient,
        group_name: str = "synthesis_workers",
        consumer_name: str = "synthesizer-1",
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.llm_client = llm_client
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.stop_event = asyncio.Event()

    async def process_message(self, event: CanonicalEvent) -> None:
        """Process an enriched agent trajectory event to synthesize skills."""
        log = logger.bind(
            tenant_id=str(event.tenant_id),
            event_id=str(event.event_id),
            session_id=event.source_id,
        )

        # Enforce filtering: only process agent session logs
        if not (
            event.source_platform == SourcePlatform.AGENT_SESSION
            and event.source_type == SourceType.AGENT_LOG
        ):
            log.debug("skill_synthesizer.ignored_non_agent_event")
            return

        # Check if successfully completed
        metadata = event.metadata or {}
        status = metadata.get("status")
        success = metadata.get("success")

        # If status or success is explicitly set and indicates failure, skip
        is_success = True
        if status is not None and status != "success":
            is_success = False
        if success is not None and not success:
            is_success = False

        if not is_success:
            log.info("skill_synthesizer.skipping_failed_trajectory")
            return

        log.info("skill_synthesizer.processing_trajectory")

        try:
            raw_output = await self.llm_client.synthesize_skills(event.content_clean)

            parsed_skills = parse_skills_from_markdown(raw_output)
            if not parsed_skills:
                log.info("skill_synthesizer.no_skills_synthesized")
                return

            # Persist to Postgres database under tenant context
            synthesized_skill_names = []
            async with tenant_context(self.engine, event.tenant_id) as conn:
                store = PostgresEventStore(conn)

                for skill_data in parsed_skills:
                    skill_id = uuid.uuid4()
                    row = {
                        "id": skill_id,
                        "tenant_id": event.tenant_id,
                        "name": skill_data["name"],
                        "description": skill_data["description"],
                        "instructions": skill_data["instructions"],
                    }
                    stmt = pg_insert(DBSkill.__table__).values(row)
                    await conn.execute(stmt)

                    synthesized_skill_names.append(skill_data["name"])
                    log.info(
                        "skill_synthesizer.skill_saved",
                        skill_id=str(skill_id),
                        skill_name=skill_data["name"],
                    )

                # Update the CanonicalEvent's agent_metadata in database
                session_id = event.source_id
                prompt = metadata.get("prompt")
                prompt_val = (
                    str(prompt)
                    if prompt is not None
                    else (event.agent_metadata.prompt if event.agent_metadata else None)
                )
                reasoning = metadata.get("reasoning_chain") or (
                    event.agent_metadata.reasoning_chain if event.agent_metadata else []
                )
                tool_calls = metadata.get("tool_calls") or (
                    event.agent_metadata.tool_calls if event.agent_metadata else []
                )
                files_read = metadata.get("files_read") or (
                    event.agent_metadata.files_read if event.agent_metadata else []
                )
                tokens = metadata.get("tokens_used") or (
                    event.agent_metadata.tokens_used if event.agent_metadata else 0
                )

                updated_agent_meta = AgentMetadata(
                    session_id=session_id,
                    prompt=prompt_val,
                    reasoning_chain=list(reasoning),
                    tool_calls=list(tool_calls),
                    files_read=list(files_read),
                    tokens_used=int(tokens),
                    synthesized_skills=synthesized_skill_names,
                )

                # Update Postgres event store
                await store.update_agent_metadata(
                    tenant_id=event.tenant_id,
                    event_id=event.event_id,
                    agent_metadata=updated_agent_meta,
                )
                log.debug("skill_synthesizer.event_agent_metadata_updated")

        except Exception as e:
            log.exception("skill_synthesizer.processing_error", error=str(e))
            raise

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Background task that runs the infinite subscription loop."""
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
            )
        except asyncio.CancelledError:
            logger.info("skill_synthesizer.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("skill_synthesizer.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        """Start consuming from the given topics and block until stopped."""
        logger.info("skill_synthesizer.starting", topics=topics, group=self.group_name)

        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        await self.stop_event.wait()
        logger.info("skill_synthesizer.shutdown_initiated")

        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("skill_synthesizer.shutdown_complete")

    def stop(self) -> None:
        """Trigger a graceful shutdown of the worker."""
        self.stop_event.set()
