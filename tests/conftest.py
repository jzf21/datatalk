"""Shared test fixtures.

The ``FakeOpenAI`` scripted client used to be copy-pasted into four agent test
files, and each of those files monkeypatched ``get_openai`` on every agent
module it touched. Now that agents resolve their clients through a
``TenantContext``, one fake context replaces all of that.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from datatalk.config import Settings
from datatalk.context import TenantContext

# --- scripted OpenAI double ---------------------------------------------------


def fn_call(call_id: str, sql: str):
    """A tool call asking to run ``sql``."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="run_sql", arguments=json.dumps({"sql": sql})),
    )


def message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def response(msg):
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class FakeCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0

    def create(self, **kwargs):
        resp = self._scripted[self.calls]
        self.calls += 1
        return resp


class FakeEmbeddings:
    """Keyword -> direction embedder, so cosine assertions stay readable."""

    VOCAB = ["sla", "revenue", "bug", "account", "sprint"]

    def create(self, model=None, input=None):
        vectors = []
        for text in input or []:
            v = np.zeros(len(self.VOCAB), dtype=np.float32)
            for i, word in enumerate(self.VOCAB):
                if word in text.lower():
                    v[i] += 1.0
            if not v.any():
                v[0] = 0.01
            vectors.append(SimpleNamespace(embedding=v.tolist()))
        return SimpleNamespace(data=vectors)


class FakeOpenAI:
    def __init__(self, scripted=()):
        self.chat = SimpleNamespace(completions=FakeCompletions(scripted))
        self.embeddings = FakeEmbeddings()


# --- contexts -----------------------------------------------------------------


def make_settings(**overrides) -> Settings:
    base = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_MODEL": "test-model",
        "CLICKHOUSE_HOST": "clickhouse.test",
        "CLICKHOUSE_DATABASE": "testdb",
    }
    base.update(overrides)
    return Settings(**base)


def make_ctx(*, openai=None, clickhouse=None, settings=None, **kw) -> TenantContext:
    """A tenant context wired to fakes -- the single agent-test injection point."""
    return TenantContext.for_test(
        openai=openai if openai is not None else FakeOpenAI(),
        clickhouse=clickhouse,
        settings=settings or make_settings(),
        **kw,
    )


@pytest.fixture
def fake_openai():
    return FakeOpenAI()


@pytest.fixture
def ctx(fake_openai) -> TenantContext:
    """Default context with an embedding-capable fake OpenAI and no ClickHouse."""
    return make_ctx(openai=fake_openai)


@pytest.fixture(autouse=True)
def _clear_client_registries():
    """Stop cached clients leaking between tests."""
    from datatalk import clients

    clients.close_all()
    yield
    clients.close_all()


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    from datatalk.db import introspect

    introspect._SCHEMA_CACHE.clear()
    yield
    introspect._SCHEMA_CACHE.clear()
