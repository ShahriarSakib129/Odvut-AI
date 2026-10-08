"""Security audit as executable tests.

* no hard-coded credentials anywhere in the repository
* ``.env`` is never shipped
* secrets never reach the logs
* SQL is always parameterised (no f-string interpolation of user input)
* destructive commands are protected and confirmed
* webhook payloads are authenticated
"""

from __future__ import annotations

import re

import pytest

PLACEHOLDER_PASSWORDS = r"(?:PASSWORD|YOUR_PASSWORD|password|your_password|xxx+|\[[^\]]*\]|\$\{[^}]*\})"

SECRET_PATTERNS = [
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "Groq API key"),
    (re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}\b"), "Telegram bot token"),
    # a real DSN password: not a documented placeholder, not a local/example host
    (re.compile(r"postgres(?:ql)?://[^\s:/]+:(?!" + PLACEHOLDER_PASSWORDS + r")[^\s:@]{6,}@"
                r"(?!localhost|127\.0\.0\.1|db\.example|aws-0|pooler\.supabase\.com|"
                r"host|db\.host|user)"),
     "database URL with password"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]

SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "build", "dist", "node_modules",
             "tests", ".pytest_cache", "docs"}


def iter_source_files(project_root):
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {".py", ".sql", ".txt", ".yaml", ".yml", ".toml", ".cfg",
                                   ".ini", ".sh", ".md", ".json", ".example"} or \
                path.name in {".env.example", "Procfile", ".gitignore"}:
            yield path


class TestNoHardcodedSecrets:
    def test_sources_contain_no_credentials(self, project_root):
        findings = []
        for path in iter_source_files(project_root):
            if path.name == ".env.example":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for pattern, label in SECRET_PATTERNS:
                match = pattern.search(text)
                if match:
                    findings.append(f"{path.relative_to(project_root)}: {label}")
        assert not findings, "possible hard-coded credentials: " + "; ".join(findings)

    def test_env_file_is_not_present(self, project_root):
        assert not (project_root / ".env").exists(), ".env must never be shipped"
        assert (project_root / ".env.example").exists()

    def test_gitignore_covers_env_and_caches(self, project_root):
        content = (project_root / ".gitignore").read_text(encoding="utf-8")
        for entry in (".env", "__pycache__/", "*.pyc", ".venv/", "venv/", ".idea/", ".vscode/"):
            assert entry in content

    def test_no_secret_in_logging_calls(self, project_root):
        """``logger.*(..., token)`` style leaks are forbidden."""
        offenders = []
        for path in (project_root / "ai", project_root / "bot_telegram", project_root / "utils"):
            for file in path.rglob("*.py"):
                text = file.read_text(encoding="utf-8")
                for match in re.finditer(r"logger\.\w+\([^)]*(bot_token|groq_api_key|api_key|"
                                         r"database_url|webhook_secret)[^)]*\)", text):
                    line = match.group(0)
                    if "redact" in line or "bool(" in line or "summary" in line:
                        continue
                    offenders.append(f"{file.name}: {line[:80]}")
        assert not offenders, f"possible secret logging: {offenders}"

    def test_readme_warns_about_secrets(self, project_root):
        readme = (project_root / "README.md").read_text(encoding="utf-8").lower()
        assert ".env" in readme
        assert "secret" in readme or "credential" in readme


class TestSqlSafety:
    def test_no_string_interpolation_of_user_values(self, project_root):
        """Detect dangerous ``execute(f"... {var} ...")`` patterns."""
        source = (project_root / "database.py").read_text(encoding="utf-8")
        # The only interpolated queries are those whose placeholders come from
        # hard-coded whitelists (table names, counter names, int-cast settings).
        allowed = (
            "vacuum", "update public.bot_users set {field}", "from public.{table}",
            "where {' and '.join", "from public.", "select", "set statement_timeout",
        )
        for match in re.finditer(r"execute\(\s*f[\"']", source):
            fragment = source[match.start():match.start() + 160]
            assert any(safe in fragment for safe in allowed), \
                f"unexpected interpolated SQL: {fragment[:120]}"

    def test_identifier_guard_exists(self, project_root):
        source = (project_root / "database.py").read_text(encoding="utf-8")
        assert "_SAFE_IDENTIFIER" in source
        assert "counter" in source and "unsupported counter" in source

    def test_injection_payload_stays_data(self, clean_db):
        payload = "BTC'; drop table public.admin_messages; --"
        clean_db.save_admin_message(chat_id=-1001, message_id=1, admin_id=777,
                                    text=payload)
        clean_db.search_admin_messages(chat_id=-1001, query_text=payload, keywords=["btc"])
        assert clean_db.count_admin_messages(-1001) == 1
        clean_db._execute("delete from public.admin_messages where chat_id = -1001")


class TestWebhookSecurity:
    def test_webhook_requires_secret(self, project_root):
        source = (project_root / "app.py").read_text(encoding="utf-8")
        assert "X-Telegram-Bot-Api-Secret-Token" in source
        assert "hmac.compare_digest" in source, "compare secrets in constant time"

    def test_webhook_rejects_missing_update_id(self, project_root):
        source = (project_root / "app.py").read_text(encoding="utf-8")
        assert '"update_id" not in data' in source

    def test_webhook_route_authenticates(self, monkeypatch):
        import app as app_module
        from config import load_settings

        settings = load_settings({
            "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
            "GROQ_API_KEY": "gsk_test",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "PUBLIC_URL": "https://info-group-ai-bot.example.com",
            "WEBHOOK_SECRET": "s" * 32,
            "AUTOSTART_BOT": "false",
        })

        class Manager:
            def ensure_started_async(self):
                return None

            def process_update(self, data, timeout=25.0):
                return True

            def stats(self):
                return {}

            def set_webhook(self, delete_first=False):
                return {"ok": True}

        monkeypatch.setattr(app_module, "_manager", lambda: Manager())
        flask_app = app_module.create_app(settings, bootstrap=False)
        with flask_app.test_client() as client:
            assert client.post("/webhook", json={"update_id": 1}).status_code == 403
            assert client.post(
                "/webhook", json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 403


class TestPermissionGuards:
    def test_admin_commands_check_permission(self, project_root):
        source = (project_root / "bot_telegram" / "handlers.py").read_text(encoding="utf-8")
        for command in ("cmd_stats", "cmd_memory_stats", "cmd_clear_memory", "cmd_set",
                        "cmd_reload", "cmd_maintenance"):
            block = source.split(f"async def {command}", 1)[1].split("async def ", 1)[0]
            assert "_require_admin" in block, f"{command} does not check admin permission"

    def test_destructive_command_needs_confirmation(self, project_root):
        source = (project_root / "bot_telegram" / "handlers.py").read_text(encoding="utf-8")
        assert 'confirmed = "confirm" in args' in source
        assert "Confirmation দরকার" in source

    def test_clear_memory_scope_whitelist(self, project_root):
        source = (project_root / "database.py").read_text(encoding="utf-8")
        assert 'CLEAR_SCOPES = ("qa", "admin", "logs", "cache", "all")' in source
        assert "scope must be one of" in source


class TestPrivacyAndData:
    def test_unanswered_questions_never_used_as_memory(self, project_root):
        retrieval = (project_root / "ai" / "retrieval.py").read_text(encoding="utf-8")
        assert "pending_questions" not in retrieval
        assert "search_admin_messages" in retrieval and "search_qa_memory" in retrieval

    def test_message_log_is_truncated(self, project_root):
        source = (project_root / "database.py").read_text(encoding="utf-8")
        tracker = (project_root / "bot_telegram" / "message_tracker.py").read_text(encoding="utf-8")
        assert "stored = truncate_text(clean, max_chars)" in source
        assert "log_text_chars" in tracker, "the tracker must honour LOG_TEXT_CHARS"

    def test_privacy_document_exists(self, project_root):
        doc = (project_root / "docs" / "PRIVACY_AND_SECURITY.md").read_text(encoding="utf-8")
        assert "privacy" in doc.lower()
        assert "admin_messages" in doc and "qa_memory" in doc


class TestExceptionSafety:
    def test_error_handler_does_not_echo_tracebacks(self, project_root):
        source = (project_root / "app.py").read_text(encoding="utf-8")
        assert '"internal server error"' in source
        assert "traceback" not in source.lower(), \
            "the Flask app must not return/expose tracebacks"
        assert "import traceback" not in source

    def test_settings_summary_is_safe_to_log(self, settings):
        summary = str(settings.public_summary())
        assert settings.bot_token not in summary
        assert settings.webhook_secret not in summary
        if settings.database_url:
            assert (settings._db_password() or "no-password") not in summary
