"""Q&A agent test: runs run_sql and returns a materialized Document."""

import json

import datatalk.agent.qa as qa_mod
import datatalk.agent.sqlloop as sqlloop_mod
from datatalk.agent.blocks import Document, Table
from datatalk.agent.executor import QueryResult
from tests.conftest import FakeOpenAI, make_ctx
from tests.conftest import fn_call as _fn_call
from tests.conftest import message as _message
from tests.conftest import response as _response



def test_qa_runs_sql_and_returns_document(monkeypatch):
    answer_json = json.dumps(
        {
            "blocks": [
                {"type": "paragraph", "text": "Here is the breakdown."},
                {"type": "table", "dataset_id": "q1", "columns": ["account", "issues"]},
            ]
        }
    )
    scripted = [
        _response(_message(tool_calls=[_fn_call("c1", "SELECT account, count() FROM jira.issues GROUP BY account")])),
        _response(_message(content=answer_json)),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))

    def fake_run_sql(sql, *, ctx=None):
        return QueryResult(
            columns=["account", "issues"],
            rows=[["acme", 5], ["globex", 9]],
            row_count=2,
            truncated=False,
            sql=sql,
        )

    monkeypatch.setattr(sqlloop_mod, "run_sql", fake_run_sql)

    events = []
    result = qa_mod.answer_question(
        "Break issues down by account",
        ctx=ctx,
        report_document=Document(blocks=[]),
        prior_queries=[],
        conversation=[],
        schema_context="TABLE jira.issues",
        on_event=lambda k, d: events.append((k, d)),
    )

    table = next(b for b in result.answer_document.blocks if isinstance(b, Table))
    assert table.rows == [["acme", 5], ["globex", 9]]  # real, materialized data
    assert len(result.queries) == 1
    assert result.queries[0]["dataset_id"] == "q1"
    assert "sql" in [k for k, _ in events]
