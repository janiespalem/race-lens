import io
import json
import urllib.request
from types import SimpleNamespace

import pytest

from racelens.positions import track


@pytest.mark.parametrize("failure", [None, TimeoutError("TLS handshake timed out")])
def test_circuit_corners_use_bounded_request_and_allow_missing_annotations(monkeypatch, failure):
    session = SimpleNamespace(session_info={"Meeting": {
        "Circuit": {"Key": 61, "ShortName": "Singapore"},
    }})

    def open_url(url, *, timeout):
        assert url == "https://api.multiviewer.app/api/v1/circuits/61/2026"
        assert 0 < timeout <= 10
        if failure is not None:
            raise failure
        return io.BytesIO(json.dumps({"corners": [
            {"number": 1, "trackPosition": {"x": 120, "y": -50}},
        ]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    assert track._circuit_corners(session, 2026) == (
        [] if failure else [{"number": 1, "x": 120.0, "y": -50.0}]
    )


@pytest.mark.parametrize("data", [None, [], {"corners": None}, {"corners": [{}]},
                                       {"corners": [{"number": 1, "trackPosition":
                                                      {"x": float("nan"), "y": 2}}]}])
def test_invalid_optional_corners_do_not_block_geometry(monkeypatch, data):
    session = SimpleNamespace(session_info={"Meeting": {"Circuit": {"Key": 61}}})
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(data).encode()),
    )
    assert track._circuit_corners(session, 2026) == []
