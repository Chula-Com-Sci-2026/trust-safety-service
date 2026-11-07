from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from common import issue
from app import app

@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client

@pytest.fixture
def subject():
    return str(uuid4())

@pytest.fixture
def admin(monkeypatch):
    moderator=str(uuid4())
    monkeypatch.setenv("MODERATOR_IDS",moderator)
    monkeypatch.delenv("IDENTITY_LOCK_URL",raising=False)
    return moderator

def auth(subject):
    return {"Authorization":"Bearer "+issue(subject)}
