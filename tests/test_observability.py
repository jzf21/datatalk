"""Langfuse tracing: inert by default, correctly shaped when configured.

The second half exports spans to an in-memory OpenTelemetry exporter instead of
the network, so the *structure* of a report trace -- which observations exist,
their types, and how they nest -- is asserted without a live Langfuse project.
That structure is the contract: evaluators, dashboards and saved filters in
Langfuse target observations by name and type, so a rename or a re-parent breaks
them silently at the far end where no test would ever run.
"""

from __future__ import annotations

import itertools
import json

import pytest

import datatalk.agent.report as report_mod
from datatalk import observability as obs
from datatalk.agent.executor import QueryResult
from datatalk.config import Settings
from tests.conftest import FakeOpenAI, make_ctx
from tests.conftest import fn_call as _fn_call
from tests.conftest import message as _message
from tests.conftest import response as _response


# --- off by default -----------------------------------------------------------


def test_no_credentials_means_no_tracing():
    """The whole feature must be absent, not merely quiet, without keys."""
    settings = Settings(LANGFUSE_PUBLIC_KEY="", LANGFUSE_SECRET_KEY="")
    assert settings.has_langfuse is False
    try:
        assert obs.configure(settings, force=True) is False
        assert obs.is_enabled() is False
        # No kwargs, because the plain OpenAI SDK rejects unknown ones: passing
        # `name=` without the Langfuse wrapper installed is a TypeError.
        assert obs.llm_kwargs("plan-report") == {}
        with obs.observe("anything") as span:
            assert span is obs.NULL_SPAN
            span.update(output="ignored").end()
        assert obs.start("anything") is obs.NULL_SPAN
    finally:
        obs.shutdown()


def test_tracing_enabled_flag_overrides_present_keys():
    settings = Settings(
        LANGFUSE_PUBLIC_KEY="pk-lf-x",
        LANGFUSE_SECRET_KEY="sk-lf-x",
        LANGFUSE_TRACING_ENABLED=False,
    )
    assert settings.has_langfuse is False


def test_null_span_absorbs_the_whole_surface():
    span = obs.NULL_SPAN
    assert span.update(output=1).end() is span
    assert span.start_observation(name="x") is span
    with span.start_as_current_observation(name="x") as inner:
        assert inner is span
    assert bool(span) is False


# --- masking ------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["password", "PASSWORD", "api_key", "secret_key", "authorization", "token"]
)
def test_mask_redacts_credential_keys(key):
    assert obs._mask(data={key: "hunter2"})[key] == "<redacted>"


def test_mask_reaches_nested_structures_and_keeps_data():
    data = {
        "spec": {"host": "db.example.com", "password": "hunter2"},
        "sources": [{"name": "main", "api_key": "sk-live"}],
        "rows": [["2026-01", 10]],
    }
    out = obs._mask(data=data)
    assert out["spec"]["password"] == "<redacted>"
    assert out["spec"]["host"] == "db.example.com"
    assert out["sources"][0]["api_key"] == "<redacted>"
    # Analytical values are the point of the trace; they must survive.
    assert out["rows"] == [["2026-01", 10]]


def test_health_check_spans_are_not_exported():
    """One open browser tab must not file a trace every 30 seconds."""
    from types import SimpleNamespace

    assert obs._should_export_span(SimpleNamespace(name="health-check")) is False
    assert obs._should_export_span(SimpleNamespace(name="analyst-step")) is True
    # A span object that misbehaves must not break the exporter thread.
    assert obs._should_export_span(object()) is True


def test_ping_names_its_generation_so_it_can_be_dropped(monkeypatch):
    """The filter keys on the name, so the name has to actually be sent."""
    monkeypatch.setattr(obs, "_enabled", True)
    ctx = make_ctx(openai=FakeOpenAI([_response(_message(content="OK"))]))
    from datatalk.llm import client as llm_client

    llm_client.ping(ctx)
    assert ctx.openai.chat.completions.kwargs[0]["name"] in obs._UNEXPORTED_NAMES


def test_mask_never_raises():
    class Exploding:
        def __repr__(self):  # pragma: no cover - defensive
            raise RuntimeError("boom")

    # Cyclic structure: a naive recursive walk would never return.
    cyclic: dict = {}
    cyclic["self"] = cyclic
    assert obs._mask(data=cyclic) is not None
    assert obs._mask(data=Exploding()) is not None


# --- trace structure ----------------------------------------------------------


_PUBLIC_KEY_SEQ = itertools.count()


@pytest.fixture
def exported_spans(monkeypatch):
    """Configure Langfuse against an in-memory exporter; yield a drain function."""
    pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # conftest sets this false suite-wide, and in this SDK the environment
    # variable beats the constructor argument — so it has to go, not be
    # argued with.
    monkeypatch.delenv("LANGFUSE_TRACING_ENABLED", raising=False)

    exporter = InMemorySpanExporter()
    from langfuse import Langfuse

    # Built directly rather than through configure(): configure() owns the real
    # credential path, and this needs the exporter injected. A fresh public key
    # per test because the SDK caches one client per key -- reusing a key would
    # silently hand back the previous test's client, still wired to *its*
    # exporter, and every assertion here would read an empty span list.
    key = f"pk-lf-test-{next(_PUBLIC_KEY_SEQ)}"
    client = Langfuse(
        public_key=key,
        secret_key=f"sk-lf-test-{key}",
        span_exporter=exporter,
        mask=obs._mask,
        tracing_enabled=True,
    )
    obs._configured, obs._enabled, obs._client = True, True, client
    try:
        yield lambda: (client.flush(), exporter.get_finished_spans())[1]
    finally:
        obs._configured = obs._enabled = False
        obs._client = None


def _tree(spans):
    """(name, type, parent name) per exported span."""
    by_id = {s.context.span_id: s for s in spans}
    out = []
    for s in spans:
        parent = by_id.get(s.parent.span_id) if s.parent else None
        out.append(
            (
                s.name,
                s.attributes.get("langfuse.observation.type"),
                parent.name if parent else None,
            )
        )
    return out


def test_report_run_emits_the_expected_observation_tree(exported_spans, monkeypatch):
    plan_json = json.dumps(
        {
            "sections": [
                {
                    "id": "trends",
                    "title": "Trends",
                    "goal": "Monthly issue counts",
                    "data_questions": ["How many issues per month?"],
                }
            ]
        }
    )
    reporter_json = json.dumps(
        {"blocks": [{"type": "table", "dataset_id": "q1", "columns": ["month"]}]}
    )
    scripted = [
        _response(_message(content=plan_json)),
        _response(_message(tool_calls=[_fn_call("c1", "SELECT month FROM jira.issues")])),
        _response(_message(content="Done.")),
        _response(_message(content=reporter_json)),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main")
    monkeypatch.setattr(
        "datatalk.agent.sqlloop.run_sql",
        lambda sql, *, ctx, source=None: QueryResult(
            columns=["month"], rows=[["2026-01"]], row_count=1, sql=sql, truncated=False
        ),
    )

    report_mod.generate_report("Show me trends", ctx=ctx, max_steps=2)
    tree = _tree(exported_spans())
    names = {n: (t, p) for n, t, p in tree}

    assert names["generate-report"] == ("agent", None)
    assert names["load-catalog"] == ("retriever", "generate-report")
    # Each agent is its own node, so the pipeline is a graph and not a blob.
    assert names["plan-report"][0] == "agent"
    assert names["gather-data"] == ("agent", "generate-report")
    assert names["write-report"][0] == "agent"
    # The tool call hangs off the Analyst, a sibling of the generation that
    # asked for it -- never dangling at the trace root.
    assert names["run-sql"] == ("tool", "gather-data")


def test_run_sql_observation_records_sql_and_result(exported_spans, monkeypatch):
    scripted = [
        _response(_message(content=json.dumps({"sections": []}))),
        _response(_message(tool_calls=[_fn_call("c1", "SELECT 1 AS n")])),
        _response(_message(content="Done.")),
        _response(_message(content=json.dumps({"blocks": []}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main")
    monkeypatch.setattr(
        "datatalk.agent.sqlloop.run_sql",
        lambda sql, *, ctx, source=None: QueryResult(
            columns=["n"], rows=[[1]], row_count=1, sql=sql, truncated=False
        ),
    )

    report_mod.generate_report("anything", ctx=ctx, max_steps=2)
    span = next(s for s in exported_spans() if s.name == "run-sql")
    payload = json.loads(span.attributes["langfuse.observation.input"])
    output = json.loads(span.attributes["langfuse.observation.output"])
    assert payload["sql"] == "SELECT 1 AS n"
    assert output["dataset_id"] == "q1"
    assert output["rows"] == [[1]]


def test_row_values_can_be_withheld(exported_spans, monkeypatch):
    """The opt-out keeps the shape of a result without the tenant's numbers."""
    monkeypatch.setattr(obs, "_capture_rows", False)
    scripted = [
        _response(_message(content=json.dumps({"sections": []}))),
        _response(_message(tool_calls=[_fn_call("c1", "SELECT revenue FROM sales")])),
        _response(_message(content="Done.")),
        _response(_message(content=json.dumps({"blocks": []}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main")
    monkeypatch.setattr(
        "datatalk.agent.sqlloop.run_sql",
        lambda sql, *, ctx, source=None: QueryResult(
            columns=["revenue"], rows=[[123456]], row_count=1, sql=sql, truncated=False
        ),
    )

    report_mod.generate_report("anything", ctx=ctx, max_steps=2)
    span = next(s for s in exported_spans() if s.name == "run-sql")
    output = json.loads(span.attributes["langfuse.observation.output"])
    assert output["rows"] == "<not captured>"
    # The SQL and the shape stay: they are what makes a trace diagnosable.
    assert output["columns"] == ["revenue"]
    assert output["row_count"] == 1
    assert "revenue FROM sales" in output["sql_executed"]


def test_tenant_attributes_carry_org_and_user(exported_spans, monkeypatch):
    scripted = [
        _response(_message(content=json.dumps({"sections": []}))),
        _response(_message(content="Done.")),
        _response(_message(content=json.dumps({"blocks": []}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main")

    report_mod.generate_report("anything", ctx=ctx, max_steps=1)
    root = next(s for s in exported_spans() if s.name == "generate-report")
    attrs = dict(root.attributes)
    assert attrs.get("user.id") == str(ctx.user_id)
    assert "feature:report" in attrs["langfuse.trace.tags"]
    assert attrs["langfuse.trace.metadata.org_id"] == str(ctx.org_id)
    # The email must NOT be here: a trace store is not a customer directory.
    assert ctx.user_email not in json.dumps(attrs, default=str)
