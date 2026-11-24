"""Deterministic keyword stand-in for LLMClient, for tests that need no real model."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from checkcord.llm.client import LLMClient
from checkcord.llm.outcomes import Outcome

_NOISE_KEYWORDS = {
    "hello",
    "hi",
    "hey",
    "lol",
    "coffee",
    "lunch",
    "banter",
    "chatter",
    "weather",
    "bot",
    "build",
    "deploying",
    "alert",
    "merged",
}


_SETTLED = ("decision", "decided", "must", "require", "need", "agreed", "risk", "never")


class KeywordLLMClient(LLMClient):
    async def synthesize_skills(self, trajectory_content: str) -> str:
        lowered = trajectory_content.lower()
        if "login" in lowered or "auth" in lowered:
            return (
                "---\n"
                "name: Mock Auth Skill\n"
                "description: Synthesized guide for authentication\n"
                "---\n"
                "# Authentication Setup\n"
                "To set up authentication, verify bearer tokens in the request headers."
            )
        return (
            "---\n"
            "name: Mock Generic Skill\n"
            "description: A generic synthesized skill\n"
            "---\n"
            "# Generic Instructions\n"
            "This is a generic synthesized skill from the mock trajectory."
        )

    async def judge_linkage(self, record_summary: str, event_text: str) -> str:
        lowered = event_text.lower()
        return "same" if "match" in lowered or "same" in lowered else "different"

    def discussion_text(self, messages: list[dict[str, Any]]) -> str:
        return "\n".join(m.get("content") or "" for m in messages)

    async def judge_settled(self, discussion_text: str, statement: str) -> str:
        return "OPEN" if "again next quarter" in discussion_text.lower() else "SETTLED"

    async def classify_chat_message(self, text: str) -> bool:
        lowered = text.lower()
        return not (set(lowered.split()) & _NOISE_KEYWORDS or lowered.startswith("["))

    async def assign_discourse_tree(
        self,
        active_trees: list[dict[str, Any]],
        message_block: list[dict[str, Any]],
    ) -> dict[str, str]:
        combined_text = " ".join(m.get("content", "").lower() for m in message_block)

        for tree in active_trees:
            summary = tree.get("summary", "").lower()
            if any(w in summary for w in combined_text.split() if len(w) > 4):
                return {
                    "tree_id": tree["tree_id"],
                    "domain_tag": tree.get("domain_tag", "HYBRID"),
                    "summary": tree.get("summary", "Updated active discourse tree"),
                }

        if any(k in combined_text for k in ["problem", "why", "need", "requirement", "goal"]):
            domain = "PROBLEM_SPACE"
        elif any(k in combined_text for k in ["fix", "how", "code", "deploy", "build"]):
            domain = "SOLUTION_SPACE"
        else:
            domain = "HYBRID"

        first = message_block[0].get("content", "business context")[:30]
        return {
            "tree_id": str(uuid4()),
            "domain_tag": domain,
            "summary": f"Discussion regarding {first}",
        }

    async def extract_outcomes(self, messages: list[dict[str, Any]]) -> list[Outcome]:
        settled = [
            n
            for n, m in enumerate(messages, 1)
            if any(k in (m.get("content") or "").lower() for k in _SETTLED)
        ]
        if not settled:
            return []
        text = max((messages[n - 1].get("content") or "" for n in settled), key=len)
        kind = "risk" if "risk" in text.lower() else "decision"
        return [Outcome(kind, text[:400], (), tuple(settled), False)]
