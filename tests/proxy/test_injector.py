from checkcord.proxy.injector import inject_payload


def test_inject_openai_format():
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "Keep instructions simple."},
            {"role": "user", "content": "Hello!"},
        ],
    }
    context = "<checkcord_context>\nRule 1\n</checkcord_context>"

    injected = inject_payload(payload, context, is_anthropic=False)
    system_content = injected["messages"][0]["content"]
    assert system_content.startswith(context)
    assert "Keep instructions simple." in system_content
    # User message unchanged
    assert injected["messages"][1]["content"] == "Hello!"


def test_inject_openai_no_system_message():
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello!"}]}
    context = "<checkcord_context>\nRule 1\n</checkcord_context>"

    injected = inject_payload(payload, context, is_anthropic=False)
    # A system message should be prepended at index 0
    assert len(injected["messages"]) == 2
    assert injected["messages"][0]["role"] == "system"
    assert injected["messages"][0]["content"] == context
    assert injected["messages"][1]["role"] == "user"


def test_inject_openai_list_system_content():
    payload = {
        "model": "gpt-4",
        "messages": [{"role": "system", "content": [{"type": "text", "text": "Base block"}]}],
    }
    context = "<checkcord_context>\nRule 1\n</checkcord_context>"

    injected = inject_payload(payload, context, is_anthropic=False)
    system_content = injected["messages"][0]["content"]
    assert isinstance(system_content, list)
    assert len(system_content) == 2
    assert system_content[0]["text"] == context
    assert system_content[1]["text"] == "Base block"


def test_inject_anthropic_format_string():
    payload = {
        "model": "claude-3",
        "system": "System instructions.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    context = "<checkcord_context>\nRule 1\n</checkcord_context>"

    injected = inject_payload(payload, context, is_anthropic=True)
    assert injected["system"] == f"{context}\n\nSystem instructions."
    assert injected["messages"][0]["content"] == "hi"


def test_inject_anthropic_format_list():
    payload = {
        "model": "claude-3",
        "system": [{"type": "text", "text": "System block."}],
        "messages": [],
    }
    context = "<checkcord_context>\nRule 1\n</checkcord_context>"

    injected = inject_payload(payload, context, is_anthropic=True)
    assert isinstance(injected["system"], list)
    assert len(injected["system"]) == 2
    assert injected["system"][0]["text"] == context
    assert injected["system"][1]["text"] == "System block."


def test_inject_anthropic_no_system_key():
    payload = {"model": "claude-3", "messages": [{"role": "user", "content": "hi"}]}
    context = "<checkcord_context>\nRule 1\n</checkcord_context>"

    injected = inject_payload(payload, context, is_anthropic=True)
    assert injected["system"] == context
    assert injected["messages"][0]["content"] == "hi"


def test_inject_collision_warning_openai():
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "Keep instructions simple."},
            {"role": "user", "content": "Hello!"},
        ],
    }
    context = "<checkcord_context>\nRule 1\n</checkcord_context>"
    collision_warning = "<conflict_warning>\nWARNING: test_collision\n</conflict_warning>"

    injected = inject_payload(
        payload, context, collision_warning=collision_warning, is_anthropic=False
    )
    system_content = injected["messages"][0]["content"]
    assert system_content.startswith(f"{context}\n\n{collision_warning}")
    assert "Keep instructions simple." in system_content
    # User message unchanged
    assert injected["messages"][1]["content"] == "Hello!"


def test_inject_collision_warning_anthropic():
    payload = {
        "model": "claude-3",
        "system": "System instructions.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    context = "<checkcord_context>\nRule 1\n</checkcord_context>"
    collision_warning = "<conflict_warning>\nWARNING: test_collision\n</conflict_warning>"

    injected = inject_payload(
        payload, context, collision_warning=collision_warning, is_anthropic=True
    )
    assert injected["system"] == f"{context}\n\n{collision_warning}\n\nSystem instructions."
    assert injected["messages"][0]["content"] == "hi"
