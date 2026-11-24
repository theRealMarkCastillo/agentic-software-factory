"""Example sealed contract. Replace with real acceptance tests for your service.

Contracts talk to the system under test over HTTP only, at SUT_BASE_URL, and
compare against golden fixtures. Name each test after the behavior it protects:
the test id is the only part of this file an agent ever sees.
"""

import json
import os
from pathlib import Path

import httpx
import pytest

BASE_URL = os.environ.get("SUT_BASE_URL", "http://sut:8080")
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="session")
def golden():
    return json.loads((FIXTURES / "golden_example.json").read_text())


@pytest.fixture(scope="session")
def client(golden):
    headers = {"X-Api-Key": golden["auth"]["api_key"]}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=5.0) as session:
        yield session


def test_health_endpoint_reports_ok(client, golden):
    expected = golden["health"]
    response = client.get(expected["path"])
    assert response.status_code == expected["status_code"]
    assert response.json() == expected["body"]
