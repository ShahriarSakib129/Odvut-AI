"""Import integrity + repository structure checks.

Requirement #38 asks for a cross-check that import paths, module names, table
names, environment variable names and configuration agree with each other.
"""

from __future__ import annotations

import importlib
import sys

import pytest

MODULES = [
    "config",
    "database",
    "services",
    "bot",
    "app",
    "utils",
    "utils.text",
    "utils.helpers",
    "utils.logger",
    "ai",
    "ai.embeddings",
    "ai.retrieval",
    "ai.prompts",
    "ai.groq_client",
    "ai.caching",
    "ai.responder",
    "bot_telegram",
    "bot_telegram.handlers",
    "bot_telegram.message_tracker",
    "bot_telegram.permissions",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name):
    assert importlib.import_module(module_name) is not None


def test_python_telegram_bot_is_not_shadowed():
    import telegram

    assert hasattr(telegram, "__version__"), "the 'telegram' package must be python-telegram-bot"
    assert "site-packages" in telegram.__file__ or "dist-packages" in telegram.__file__


def test_database_module_is_the_data_layer():
    import database

    assert database.__file__.endswith("database.py")
    assert callable(database.save_qa_memory)


def test_required_files_exist(project_root):
    for relative in (
        "app.py", "bot.py", "config.py", "database.py", "services.py",
        "requirements.txt", "Procfile", "render.yaml", ".env.example",
        ".gitignore", "README.md", "LICENSE", "pytest.ini",
        "database/schema.sql",
        "ai/groq_client.py", "ai/prompts.py", "ai/retrieval.py",
        "bot_telegram/handlers.py", "bot_telegram/message_tracker.py",
        "bot_telegram/permissions.py",
        "utils/logger.py", "utils/helpers.py", "utils/text.py",
        "scripts/apply_schema.py", "scripts/check_config.py",
        "scripts/set_webhook.py", "scripts/set_commands.py",
        "scripts/maintenance.py", "scripts/export_memory.py",
        "docs/ARCHITECTURE.md", "docs/DEPLOYMENT.md",
        "docs/TROUBLESHOOTING.md", "docs/PRIVACY_AND_SECURITY.md",
        "docs/SETUP_CHECKLIST.md",
    ):
        assert (project_root / relative).exists(), f"missing file: {relative}"


def test_gitignore_protects_secrets(project_root):
    content = (project_root / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "__pycache__/", "*.pyc", ".venv/", "venv/", ".idea/", ".vscode/"):
        assert pattern in content, f".gitignore must contain {pattern}"
    assert ".env.example" in content and "!" in content, "the example file must stay tracked"


def test_env_example_has_no_secrets(project_root):
    content = (project_root / ".env.example").read_text(encoding="utf-8")
    assert "GROQ_MODEL=openai/gpt-oss-120b" in content
    for key in ("BOT_TOKEN", "GROQ_API_KEY", "DATABASE_URL", "TARGET_ADMIN_ID", "GROUP_ID",
                "PUBLIC_URL", "WEBHOOK_SECRET", "ADMIN_ID"):
        assert f"{key}=" in content, f"{key} missing in .env.example"
    for line in content.splitlines():
        if "gsk_" in line or "AAH" in line or "postgresql://user:password@host" in line:
            pytest.fail(f"looks like a real secret in .env.example: {line}")


def test_schema_tables_match_data_layer(project_root):
    sql = (project_root / "database" / "schema.sql").read_text(encoding="utf-8").lower()
    for table in ("admin_messages", "qa_memory", "bot_users", "bot_settings", "message_logs",
                  "pending_questions", "conversation_sessions", "response_cache",
                  "ai_usage_log"):
        assert table in sql
    source = (project_root / "database.py").read_text(encoding="utf-8")
    for table in ("admin_messages", "qa_memory", "bot_users", "bot_settings", "message_logs",
                  "pending_questions", "conversation_sessions", "response_cache",
                  "ai_usage_log"):
        assert f"public.{table}" in source, f"database.py never touches {table}"


def test_configuration_keys_are_documented(project_root):
    """Every env var used in code must appear in .env.example or the README."""
    import config

    documented = (project_root / ".env.example").read_text(encoding="utf-8")
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    missing = []
    for key in config.iter_config_keys():
        if f"{key}=" not in documented and key not in readme:
            missing.append(key)
    assert not missing, f"undocumented configuration keys: {missing}"


def test_no_placeholder_todos_in_python(project_root):
    import re

    offenders = []
    pattern = re.compile(r"#\s*(TODO|FIXME|XXX|implement later)", re.IGNORECASE)
    for path in project_root.rglob("*.py"):
        if any(part in {".venv", "venv", "__pycache__", "tests"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            offenders.append(path.name)
    assert not offenders, f"unfinished work markers found in: {offenders}"


def test_sys_path_contains_project_root(project_root):
    assert str(project_root) in sys.path
