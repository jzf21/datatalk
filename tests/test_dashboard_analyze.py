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

    def create(self, **kwargs):
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
