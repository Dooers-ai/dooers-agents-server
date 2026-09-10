from unittest.mock import MagicMock, patch

from dooers.agents.server.storage import gcs


def _client_with_blob(blob):
    client = MagicMock()
    client.bucket.return_value.blob.return_value = blob
    return client


def test_delete_blob_returns_true_when_present():
    blob = MagicMock()
    blob.exists.return_value = True
    with patch("google.cloud.storage.Client", return_value=_client_with_blob(blob)):
        assert gcs.delete_blob("b", "k") is True
    blob.delete.assert_called_once()


def test_read_json_absent_returns_empty_and_zero():
    blob = MagicMock()
    blob.exists.return_value = False
    with patch("google.cloud.storage.Client", return_value=_client_with_blob(blob)):
        data, gen = gcs.read_json_with_generation("b", "k")
    assert data == {} and gen == 0


def test_write_json_precondition_failure_returns_false():
    from google.api_core.exceptions import PreconditionFailed

    blob = MagicMock()
    blob.upload_from_string.side_effect = PreconditionFailed("gen mismatch")
    with patch("google.cloud.storage.Client", return_value=_client_with_blob(blob)):
        assert gcs.write_json_if_generation("b", "k", {"x": 1}, 5) is False
