from typing import Any


def inject_content_field(content: Any, context_str: str) -> Any:
    """Prepends context_str to the system message content (can be str or list of dicts)."""
    if isinstance(content, str):
        return f"{context_str}\n\n{content}"
    elif isinstance(content, list):
        # Insert a new text block containing context at index 0
        return [{"type": "text", "text": context_str}] + content
    return content


def inject_payload(
    payload: dict[str, Any],
    context_str: str | None = None,
    collision_warning: str | None = None,
    is_anthropic: bool = False,
) -> dict[str, Any]:
    """Injects the company context and collision warnings at the very top (index 0)
    of the system prompt.

    Supports both OpenAI and Anthropic schemas, using `is_anthropic` to resolve routing.
    Guarantees user and assistant messages are not touched.
    """
    if not isinstance(payload, dict):
        return payload

    parts = []
    if context_str:
        parts.append(context_str)
    if collision_warning:
        parts.append(collision_warning)

    if not parts:
        return payload

    injection_text = "\n\n".join(parts)
    mutated = dict(payload)

    # Anthropic Schema
    if is_anthropic:
        if "system" in mutated:
            system_val = mutated["system"]
            if isinstance(system_val, str):
                mutated["system"] = f"{injection_text}\n\n{system_val}"
            elif isinstance(system_val, list):
                # Prepend as a new block
                mutated["system"] = [{"type": "text", "text": injection_text}] + system_val
        else:
            # Inject top-level system field
            mutated["system"] = injection_text

    # OpenAI Schema (fallback if not explicitly Anthropic)
    else:
        if "messages" in mutated and isinstance(mutated["messages"], list):
            messages = list(mutated["messages"])

            # Look for the first system message
            system_index = -1
            for idx, msg in enumerate(messages):
                if isinstance(msg, dict) and msg.get("role") == "system":
                    system_index = idx
                    break

            if system_index != -1:
                # Modify existing system message
                system_msg = dict(messages[system_index])
                if "content" in system_msg:
                    system_msg["content"] = inject_content_field(
                        system_msg["content"], injection_text
                    )
                else:
                    system_msg["content"] = injection_text
                messages[system_index] = system_msg
            else:
                # Prepend a new system message at index 0
                new_system_msg = {"role": "system", "content": injection_text}
                messages.insert(0, new_system_msg)

            mutated["messages"] = messages

    return mutated
