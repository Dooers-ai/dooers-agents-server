"""V4 URLs in managed storage are signed with the runtime token, not a JSON key."""

import sys
import types

from dooers.agents.server.storage import gcs


def test_iam_signed_url_uses_runtime_token_and_email(monkeypatch):
    blob_box: dict[str, object] = {}

    class _Blob:
        def generate_signed_url(self, **kwargs):
            blob_box["kwargs"] = kwargs
            return "https://signed.example/obj"

    class _Bucket:
        def blob(self, name):
            blob_box["name"] = name
            return _Blob()

    class _Client:
        def __init__(self, credentials=None):
            blob_box["credentials"] = credentials

        def bucket(self, name):
            blob_box["bucket"] = name
            return _Bucket()

    creds = types.SimpleNamespace(
        token="runtime-token",
        valid=True,
        service_account_email="tenant@dooers-agents.iam.gserviceaccount.com",
    )
    auth = types.ModuleType("google.auth")
    auth.default = lambda scopes=None: (creds, None)
    transport = types.ModuleType("google.auth.transport")
    requests_mod = types.ModuleType("google.auth.transport.requests")
    storage = types.ModuleType("google.cloud.storage")
    storage.Client = _Client
    cloud = types.ModuleType("google.cloud")
    cloud.storage = storage
    google = types.ModuleType("google")
    google.auth = auth
    google.cloud = cloud

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)

    url = gcs.generate_signed_get_url_via_iam("dooers-agents-bucket", "agents/org/file.pdf", expiration_minutes=15)

    assert url == "https://signed.example/obj"
    assert blob_box["credentials"] is creds
    assert blob_box["bucket"] == "dooers-agents-bucket"
    assert blob_box["name"] == "agents/org/file.pdf"
    kwargs = blob_box["kwargs"]
    assert kwargs["service_account_email"] == creds.service_account_email
    assert kwargs["access_token"] == "runtime-token"
    assert kwargs["version"] == "v4"
    assert kwargs["method"] == "GET"
