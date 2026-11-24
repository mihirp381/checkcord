from checkcord.proxy.sanitizer import sanitize_payload


def test_sanitize_openai_format():
    payload = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant.\nToday is Sunday, July 4, 2026.\n"
                    "Operating System: macOS 14.5\nWorking directory: /Users/alex/project"
                ),
            },
            {
                "role": "user",
                "content": "Write a python script. Current time is 10:00 AM.",  # Not redacted
            },
        ],
    }

    sanitized = sanitize_payload(payload)

    # Verify system block was sanitized
    system_msg = sanitized["messages"][0]["content"]
    assert "[TIME_REDACTED]" in system_msg
    assert "[OS_REDACTED]" in system_msg
    assert "[PATH_REDACTED]" in system_msg
    assert "macOS" not in system_msg
    assert "alex" not in system_msg

    # Verify user message remains unchanged (Fidelity Guarantee)
    user_msg = sanitized["messages"][1]["content"]
    assert "Current time is 10:00 AM." in user_msg
    assert "[TIME_REDACTED]" not in user_msg


def test_sanitize_anthropic_format():
    payload = {
        "model": "claude-3-5-sonnet",
        "system": (
            "System instructions.\nLocal time: 2026-07-04 09:36:12\nOS: Ubuntu 22.04\ncwd: /var/www"
        ),
        "messages": [{"role": "user", "content": "What is the date today?"}],
    }

    sanitized = sanitize_payload(payload)

    system_prompt = sanitized["system"]
    assert "[TIME_REDACTED]" in system_prompt
    assert "[OS_REDACTED]" in system_prompt
    assert "[PATH_REDACTED]" in system_prompt
    assert "Ubuntu" not in system_prompt
    assert "/var/www" not in system_prompt

    # User message untouched
    assert sanitized["messages"][0]["content"] == "What is the date today?"


def test_sanitize_anthropic_list_system():
    payload = {
        "model": "claude-3-5-sonnet",
        "system": [
            {"type": "text", "text": "Base instructions.\nToday is Sunday."},
            {"type": "text", "text": "Platform: Windows 11\nWorkspace: C:\\Users\\Dev\\App"},
        ],
        "messages": [],
    }

    sanitized = sanitize_payload(payload)

    assert isinstance(sanitized["system"], list)
    assert "[TIME_REDACTED]" in sanitized["system"][0]["text"]
    assert "[OS_REDACTED]" in sanitized["system"][1]["text"]
    assert "[PATH_REDACTED]" in sanitized["system"][1]["text"]


def test_sanitize_openai_list_content():
    payload = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "System text here.\nDate: 2026-07-04"},
                    {"type": "text", "text": "Cwd: /tmp/test-run"},
                ],
            }
        ],
    }

    sanitized = sanitize_payload(payload)
    system_content = sanitized["messages"][0]["content"]
    assert isinstance(system_content, list)
    assert "[TIME_REDACTED]" in system_content[0]["text"]
    assert "[PATH_REDACTED]" in system_content[1]["text"]


def test_sanitize_fail_open_edge_cases():
    # Test that non-dict or malformed payloads do not crash
    assert sanitize_payload(None) is None
    assert sanitize_payload([]) == []

    # Test that unexpected fields remain untouched
    payload = {"other_field": 123, "messages": [{"role": "user", "content": "OS: macOS"}]}
    sanitized = sanitize_payload(payload)
    assert sanitized["other_field"] == 123
    assert sanitized["messages"][0]["content"] == "OS: macOS"


def test_sanitizer_fidelity_edge_cases():
    """Verify that general instructions containing keywords are not over-matched and redacted."""
    payload = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Instructions:\n"
                    "1. You must validate the expiration date of the token.\n"
                    "2. Implement a cross-platform compatibility layer.\n"
                    "3. Make sure the database uses UTC timezone for dates.\n"
                    "4. Today is not the day to fail.\n"
                    "5. The project works inside a local workspace.\n"
                    "6. Clean the working directory before building."
                ),
            }
        ],
    }

    sanitized = sanitize_payload(payload)
    system_content = sanitized["messages"][0]["content"]

    # Assert that none of the generic instruction text was corrupted or redacted
    assert "validate the expiration date of the token" in system_content
    assert "Implement a cross-platform compatibility layer" in system_content
    assert "Today is not the day to fail" in system_content
    assert "local workspace" in system_content
    assert "Clean the working directory before building" in system_content

    assert "[TIME_REDACTED]" not in system_content
    assert "[OS_REDACTED]" not in system_content
    assert "[PATH_REDACTED]" not in system_content
