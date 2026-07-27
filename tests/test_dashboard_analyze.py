"""analyze_dashboard runs off a materialized dashboard (fake client, no API)."""

from types import SimpleNamespace

import datatalk.agent.analyze as analyze_mod
from datatalk.agent.blocks import Document, Row, Stat, materialize
from datatalk.agent.executor import QueryResult
from tests.conftest import make_ctx


class _FakeCompletions:
    def __init__(self, text):
        self.text = text
        self.seen_user = None
        self.seen_system = None

    def create(self, **kwargs):
        self.seen_system = kwargs["messages"][0]["content"]
        self.seen_user = kwargs["messages"][-1]["content"]
        msg = SimpleNamespace(content=self.text)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _FakeOpenAI:
    def __init__(self, text):
        self.completions = _FakeCompletions(text)
        self.chat = SimpleNamespace(completions=self.completions)


def test_analyze_dashboard_returns_markdown():
    fake = _FakeOpenAI("## Findings\nRevenue is up.")
    ctx = make_ctx(openai=fake)

    ds = QueryResult(columns=["metric", "current", "prior"],
                     rows=[["revenue", 120, 100]], row_count=1,
                     truncated=False, sql="x")
    doc = materialize(
        Document(blocks=[Row(children=[
            Stat(dataset_id="q1", value_col="current", label="Revenue",
                 delta_col="prior", unit="$"),
        ])]),
        {"q1": ds},
    )

    out = analyze_mod.analyze_dashboard(doc, ctx=ctx, focus="growth")
    assert out == "## Findings\nRevenue is up."
    # The real numbers reach the model; the focus is included.
    assert "Revenue: 120" in fake.completions.seen_user
    assert "growth" in fake.completions.seen_user


def test_analyze_dashboard_gets_the_workspace_context():
    """The critique agent judges numbers against the workspace's own definitions.

    Without them it reviews figures it does not understand -- e.g. calling a
    churn number 'healthy growth' because it cannot know churn is down-is-good
    here or that test orders are excluded.
    """
    from datatalk.context import ContextFile, ContextModel

    fake = _FakeOpenAI("ok")
    model = ContextModel(files=(
        ContextFile(id=1, path="overview.md", summary="", body_md="We sell widgets."),
        ContextFile(id=2, path="ontology/orders.md", summary="",
                    body_md="Exclude test orders.",
                    covers=(("main", "shop.orders"),)),
    ))
    ctx = make_ctx(openai=fake, context_model=model)

    doc = Document(blocks=[])
    queries = [{"source": "main", "sql": "SELECT count(*) FROM shop.orders"}]
    analyze_mod.analyze_dashboard(doc, ctx=ctx, queries=queries)

    assert "We sell widgets." in fake.completions.seen_system
    assert "Exclude test orders." in fake.completions.seen_system


def test_analyze_dashboard_formats_cleanly_without_context():
    fake = _FakeOpenAI("ok")
    ctx = make_ctx(openai=fake)
    analyze_mod.analyze_dashboard(Document(blocks=[]), ctx=ctx)
    assert "{context_block}" not in fake.completions.seen_system


def test_analyze_input_is_clipped_and_capped(monkeypatch):
    """An external paste can be arbitrarily large; the critique must bound both
    its input (clip marker included) and its completion (max_tokens)."""

    class _CapturingCompletions(_FakeCompletions):
        def __init__(self, text):
            super().__init__(text)
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return super().create(**kwargs)

    fake = _FakeOpenAI("ok")
    fake.completions = _CapturingCompletions("ok")
    fake.chat = SimpleNamespace(completions=fake.completions)
    from tests.conftest import make_settings

    ctx = make_ctx(
        openai=fake, settings=make_settings(OPENAI_ANALYZE_MAX_TOKENS=512)
    )

    analyze_mod.analyze_report("x" * 100_000, ctx=ctx)

    assert fake.completions.kwargs["max_tokens"] == 512
    assert "truncated at" in fake.completions.seen_user
    assert len(fake.completions.seen_user) < 30_000


def test_analyze_omits_max_tokens_when_unset():
    fake = _FakeOpenAI("ok")

    captured = {}
    real = fake.completions.create

    def create(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    fake.completions.create = create
    fake.chat = SimpleNamespace(completions=fake.completions)
    ctx = make_ctx(openai=fake)

    analyze_mod.analyze_report("short report", ctx=ctx)

    assert "max_tokens" not in captured
