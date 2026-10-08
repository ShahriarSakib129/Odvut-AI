"""Flask endpoints, webhook security and update ingestion tests."""

from __future__ import annotations

import json

import pytest

import app as app_module
from config import load_settings
from tests.fakes import FakeDB

SECRET = "testsecret_testsecret_testsecret_test"


@pytest.fixture()
def settings():
    return load_settings({
        "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
        "GROQ_API_KEY": "gsk_test",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "TARGET_ADMIN_ID": "777000111",
        "GROUP_ID": "-1001234567890",
        "PUBLIC_URL": "https://info-group-ai-bot.example.com",
        "WEBHOOK_SECRET": SECRET,
        "ENVIRONMENT": "test",
        # /health is expected to kick off the lazy bot startup
        "AUTOSTART_BOT": "true",
    })


class FakeManager:
    def __init__(self, started: bool = True, process_result: bool = True):
        self.started = started
        self.process_result = process_result
        self.updates: list[dict] = []
        self.ensured = 0
        self.start_error = ""

    def ensure_started_async(self):
        self.ensured += 1

    def process_update(self, data, timeout=25.0):
        self.updates.append(data)
        return self.process_result

    def stats(self):
        return {"mode": "webhook", "started": self.started, "updates_received": len(self.updates)}

    def set_webhook(self, delete_first=False):
        return {"ok": True, "url": "https://info-group-ai-bot.example.com/webhook",
                "description": "Webhook was set"}


@pytest.fixture()
def client(settings, monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(app_module, "_manager", lambda: manager)
    flask_app = app_module.create_app(settings, bootstrap=False)
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        test_client.manager = manager  # type: ignore[attr-defined]
        yield test_client


def test_index(client):
    response = client.get("/")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["service"] == "INFO GROUP AI BOT"
    assert payload["status"] == "ok"


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["service"] == "INFO GROUP AI BOT"
    assert "uptime_seconds" in payload
    assert payload["telegram"]["started"] is True
    assert client.manager.ensured >= 1  # /health kicks off lazy startup


def test_version_endpoint(client):
    assert client.get("/version").get_json()["version"] == app_module.VERSION


def test_unknown_route_returns_json_404(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.get_json()["error"] == "not found"


def test_security_headers(client):
    headers = client.get("/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


class TestWebhookSecurity:
    def test_missing_secret_rejected(self, client):
        response = client.post("/webhook", json={"update_id": 1})
        assert response.status_code == 403
        assert client.manager.updates == []

    def test_wrong_secret_rejected(self, client):
        response = client.post("/webhook", json={"update_id": 1},
                               headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        assert response.status_code == 403

    def test_correct_secret_accepted(self, client):
        response = client.post("/webhook", json={"update_id": 11},
                               headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        assert response.status_code == 200
        assert response.get_json()["processed"] is True
        assert client.manager.updates[0]["update_id"] == 11

    def test_query_token_accepted(self, client):
        response = client.post(f"/webhook?token={SECRET}", json={"update_id": 12})
        assert response.status_code == 200

    def test_alias_route_works(self, client):
        response = client.post("/telegram/webhook", json={"update_id": 13},
                               headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        assert response.status_code == 200

    def test_malformed_payload_rejected(self, client):
        response = client.post("/webhook", json={"not": "an update"},
                               headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        assert response.status_code == 400

    def test_non_json_payload_rejected(self, client):
        response = client.post("/webhook", data="hello",
                               headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        assert response.status_code == 400

    def test_get_on_webhook_is_405(self, client):
        assert client.get("/webhook").status_code == 405

    def test_internal_error_returns_200_so_telegram_stops_retrying(self, settings, monkeypatch):
        class Exploding(FakeManager):
            def process_update(self, data, timeout=25.0):
                raise RuntimeError("boom")

        monkeypatch.setattr(app_module, "_manager", lambda: Exploding())
        flask_app = app_module.create_app(settings, bootstrap=False)
        with flask_app.test_client() as test_client:
            response = test_client.post("/webhook", json={"update_id": 99},
                                        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
            assert response.status_code == 200
            assert response.get_json()["processed"] is False


    def test_still_starting_returns_503_so_telegram_retries(self, settings, monkeypatch):
        import bot as bot_module

        class NotReady(FakeManager):
            def process_update(self, data, timeout=25.0):
                raise bot_module.ApplicationNotReady("still booting")

        monkeypatch.setattr(app_module, "_manager", lambda: NotReady())
        flask_app = app_module.create_app(settings, bootstrap=False)
        with flask_app.test_client() as test_client:
            response = test_client.post("/webhook", json={"update_id": 100},
                                        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
            assert response.status_code == 503
            assert response.get_json()["reason"] == "bot starting"


class TestSetWebhookEndpoint:
    def test_requires_token(self, client):
        assert client.get("/set_webhook").status_code == 403

    def test_with_token(self, client):
        response = client.get(f"/set_webhook?token={SECRET}")
        assert response.status_code == 200
        assert response.get_json()["ok"] is True

    def test_unconfigured_public_url(self, monkeypatch):
        settings = load_settings({
            "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "WEBHOOK_SECRET": SECRET,
            "WEBHOOK_REQUIRE_SECRET": "true",
        })
        monkeypatch.setattr(app_module, "_manager", lambda: FakeManager())
        flask_app = app_module.create_app(settings, bootstrap=False)
        with flask_app.test_client() as test_client:
            response = test_client.get(f"/set_webhook?token={SECRET}")
            assert response.status_code == 400


def test_misconfigured_app_refuses_webhook(monkeypatch):
    settings = load_settings({"BOT_TOKEN": "", "DATABASE_URL": ""})
    monkeypatch.setattr(app_module, "_manager", lambda: FakeManager())
    flask_app = app_module.create_app(settings, bootstrap=False)
    with flask_app.test_client() as test_client:
        response = test_client.post("/webhook", json={"update_id": 5})
        assert response.status_code == 503
        assert "problems" in response.get_json()


def test_health_reports_problems(monkeypatch):
    settings = load_settings({"BOT_TOKEN": "", "DATABASE_URL": ""})
    monkeypatch.setattr(app_module, "_manager", lambda: FakeManager())
    flask_app = app_module.create_app(settings, bootstrap=False)
    with flask_app.test_client() as test_client:
        payload = test_client.get("/health").get_json()
        assert payload["status"] == "degraded"
        assert len(payload["problems"]) >= 1


def test_unprotected_webhook_when_explicitly_allowed(monkeypatch):
    settings = load_settings({
        "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "WEBHOOK_REQUIRE_SECRET": "false",
        "PUBLIC_URL": "https://x.example.com",
    })
    manager = FakeManager()
    monkeypatch.setattr(app_module, "_manager", lambda: manager)
    flask_app = app_module.create_app(settings, bootstrap=False)
    with flask_app.test_client() as test_client:
        assert test_client.post("/webhook", json={"update_id": 7}).status_code == 200


def test_health_reports_degraded_when_the_bot_cannot_start(settings, monkeypatch):
    """A broken bot token must be visible in /health (still HTTP 200)."""
    class BrokenBot(FakeManager):
        def stats(self):
            return {"mode": "webhook", "started": False, "starting": False,
                    "start_error": "The token ***REDACTED*** was rejected",
                    "start_attempts": 3, "retry_in_seconds": 12}

    monkeypatch.setattr(app_module, "_manager", lambda: BrokenBot())
    flask_app = app_module.create_app(settings, bootstrap=False)
    with flask_app.test_client() as client:
        response = client.get("/health")
        assert response.status_code == 200, "UptimeRobot must not flap on cold starts"
        payload = response.get_json()
        assert payload["status"] == "degraded"
        assert payload["telegram"]["start_error"]
        assert "***REDACTED***" in payload["telegram"]["start_error"]
