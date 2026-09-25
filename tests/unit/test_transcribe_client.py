from pathlib import Path

import pytest

from src.transcribe_client import TranscribeClient


class TestTranscribeClient:
    def test_init(self):
        client = TranscribeClient(base_url="http://localhost:8000")
        assert client.base_url == "http://localhost:8000"
        assert client.session_token is None

    def test_init_strips_slash(self):
        client = TranscribeClient(base_url="http://localhost:8000/")
        assert client.base_url == "http://localhost:8000"

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with TranscribeClient(base_url="http://localhost:8000") as client:
            assert client._session is not None
        assert client._session.closed