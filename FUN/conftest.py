import pytest
import covers
import cover_hosts

@pytest.fixture(autouse=True)
def isolated_host_circuits(monkeypatch):
    cover_hosts._blocked.clear()
    covers._last_upload_at = 0.0
    monkeypatch.setattr(covers, "MIN_UPLOAD_INTERVAL", 0)
    yield
    cover_hosts._blocked.clear()
