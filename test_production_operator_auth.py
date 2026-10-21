"""Production operator authentication configuration regression tests.

These tests are local and deterministic: they do not contact Google Sheets,
start discovery, or send outreach.
"""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def client():
    return TestClient(server.app)


HOSTED_ENV_NO_KEY = {
    "K_SERVICE": "dripp-leads-api",
    "K_REVISION": "dripp-leads-api-test",
    "CLOUD_RUN_JOB": "",
    "CLOUD_RUN_EXECUTION": "",
    "OPERATOR_KEY": "",
    "OPERATOR_API_KEY": "",
}


def test_hosted_private_api_fails_closed_when_operator_key_is_missing(client):
    with patch.dict(os.environ, HOSTED_ENV_NO_KEY, clear=False):
        response = client.get("/api/system/config")

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Operator authentication required")


def test_hosted_login_refuses_to_create_session_without_configured_operator_key(client):
    with patch.dict(os.environ, HOSTED_ENV_NO_KEY, clear=False):
        response = client.post(
            "/api/operator/login",
            json={"operator_key": "arbitrary-client-supplied-value"},
        )

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"].lower()


def test_hosted_private_api_rejects_wrong_key_when_key_is_configured(client):
    with patch.dict(
        os.environ,
        {
            "K_SERVICE": "dripp-leads-api",
            "K_REVISION": "dripp-leads-api-test",
            "OPERATOR_KEY": "test-only-known-key",
            "OPERATOR_API_KEY": "",
        },
        clear=False,
    ):
        response = client.get("/api/system/config")

    assert response.status_code == 401


def test_local_development_open_mode_is_preserved(client):
    with patch.dict(
        os.environ,
        {
            "K_SERVICE": "",
            "K_REVISION": "",
            "CLOUD_RUN_JOB": "",
            "CLOUD_RUN_EXECUTION": "",
            "OPERATOR_KEY": "",
            "OPERATOR_API_KEY": "",
        },
        clear=False,
    ):
        response = client.get("/api/system/config")

    assert response.status_code == 200
    assert response.json().get("status") == "ok"
