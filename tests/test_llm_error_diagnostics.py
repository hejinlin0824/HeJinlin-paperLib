"""Regression tests for actionable model API errors and bounded retries."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import translate


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = body

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://model.invalid/v1/chat/completions")
            response = httpx.Response(self.status_code, text=self.text, request=request)
            raise httpx.HTTPStatusError("bad response", request=request, response=response)

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def _info():
    return {
        "provider": "custom",
        "base_url": "https://model.invalid/v1",
        "api_key": "test-key",
        "model": "test-model",
        "temperature": 0,
    }


def test_timeout_error_keeps_exception_type_even_when_str_is_empty(monkeypatch):
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            calls["count"] += 1
            request = httpx.Request("POST", "https://model.invalid/v1/chat/completions")
            raise httpx.ConnectTimeout("", request=request)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(translate.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(translate.asyncio, "sleep", no_sleep)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(translate.chat(_info(), [{"role": "user", "content": "hello"}],
                                   timeout_seconds=7, retries=2))
    assert calls["count"] == 2
    assert "ConnectTimeout" in str(error.value)
    assert "timeout=7s" in str(error.value)


def test_permanent_401_is_not_retried_and_includes_http_body(monkeypatch):
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            calls["count"] += 1
            return _FakeResponse(401, '{"error":"invalid api key"}')

    monkeypatch.setattr(translate.httpx, "AsyncClient", FakeClient)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(translate.chat(_info(), [{"role": "user", "content": "hello"}],
                                   retries=3))
    assert calls["count"] == 1
    assert "HTTP 401" in str(error.value)
    assert "invalid api key" in str(error.value)


def test_successful_openai_compatible_response_is_unchanged(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return _FakeResponse(200, "")

    monkeypatch.setattr(translate.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(translate.chat(_info(), [{"role": "user", "content": "hello"}]))
    assert result == "ok"
