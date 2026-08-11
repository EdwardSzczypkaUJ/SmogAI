from __future__ import annotations

import httpx
import pytest

from smog_ai.errors import ExternalAPIError, ExternalAPIStatusError
from smog_ai.http_client import ResilientHttpClient


def test_http_406_is_not_retried(app_config, monkeypatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(406, request=request, text="Not acceptable")

    monkeypatch.setattr("smog_ai.http_client.time.sleep", lambda _: None)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ExternalAPIStatusError, match="status=406") as error:
        ResilientHttpClient(app_config.api, client=client).get_json("https://example.test/gios")
    assert error.value.status_code == 406
    assert error.value.url == "https://example.test/gios"
    assert calls == 1


def test_http_503_is_retried(app_config, monkeypatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request, text="temporary")
        return httpx.Response(200, request=request, json={"ok": True})

    monkeypatch.setattr("smog_ai.http_client.time.sleep", lambda _: None)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert ResilientHttpClient(app_config.api, client=client).get_json("https://example.test/api") == {"ok": True}
    assert calls == 2


def test_request_headers_override_accept(app_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "application/ld+json"
        return httpx.Response(200, request=request, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"Accept": "application/json"})
    result = ResilientHttpClient(app_config.api, client=client).get_json(
        "https://example.test/gios",
        headers={"Accept": "application/ld+json"},
    )
    assert result == {"ok": True}
