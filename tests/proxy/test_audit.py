import os

from checkcord.proxy.audit import AuditLogger, generate_json_diff


def test_generate_json_diff():
    original = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}
    mutated = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello, world!"}]}

    diff = generate_json_diff(original, mutated)
    assert "Original Payload" in diff
    assert "Mutated Payload" in diff
    assert '-      "content": "Hello"' in diff
    assert '+      "content": "Hello, world!"' in diff

    # Matching dictionaries should return empty string
    assert generate_json_diff(original, original) == ""


def test_audit_logger_writes_diff(tmp_path):
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        logger = AuditLogger(relative_log_dir=os.path.join(".context", "logs"))

        orig = {"model": "gpt-4", "messages": [{"role": "user", "content": "A"}]}
        mutated = {"model": "gpt-4", "messages": [{"role": "user", "content": "B"}]}

        logger.log_diff("/v1/chat/completions", orig, mutated)

        log_file = os.path.abspath(os.path.join(".context", "logs", "prompt_diffs.log"))
        assert os.path.exists(log_file)

        with open(log_file, encoding="utf-8") as f:
            content = f.read()

        assert "TIMESTAMP:" in content
        assert "PATH:      /v1/chat/completions" in content
        assert "MODEL:     gpt-4" in content
        assert '-      "content": "A"' in content
        assert '+      "content": "B"' in content

    finally:
        # Clean up any logging handlers to prevent holding file locks
        if logger._logger:
            for handler in list(logger._logger.handlers):
                handler.close()
                logger._logger.removeHandler(handler)
        os.chdir(old_cwd)


def test_audit_logger_rotation(tmp_path):
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Use a very small rotation limit for testing purposes
        logger = AuditLogger(relative_log_dir=os.path.join(".context", "logs"))

        # Override handler to use small maxBytes (1000 bytes)
        logger._setup_logger()
        assert logger._logger is not None

        # Replace the handlers with a custom 1KB rotating handler
        for handler in list(logger._logger.handlers):
            handler.close()
            logger._logger.removeHandler(handler)

        import logging
        from logging.handlers import RotatingFileHandler

        log_file = os.path.abspath(os.path.join(".context", "logs", "prompt_diffs.log"))
        handler = RotatingFileHandler(log_file, maxBytes=1000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger._logger.addHandler(handler)

        orig = {"model": "gpt-4", "messages": [{"role": "user", "content": "A"}]}
        mutated = {"model": "gpt-4", "messages": [{"role": "user", "content": "B" * 200}]}

        # Log multiple times to exceed 1000 bytes
        for _ in range(10):
            logger.log_diff("/v1/chat/completions", orig, mutated)

        # Verify rotation happened
        log_file_rotated = log_file + ".1"
        assert os.path.exists(log_file)
        assert os.path.exists(log_file_rotated)

    finally:
        if logger._logger:
            for handler in list(logger._logger.handlers):
                handler.close()
                logger._logger.removeHandler(handler)
        os.chdir(old_cwd)
