"""Q&A agent test: runs run_sql and returns a materialized Document."""

import json
from types import SimpleNamespace

import datatalk.agent.qa as qa_mod
import datatalk.agent.sqlloop as sqlloop_mod
from datatalk.agent.blocks import Document, Table
from datatalk.agent.executor import QueryResult


def _fn_call(call_id, sql):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="run_sql", arguments=json.dumps({"sql": sql})),
    )


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _response(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0

    def create(self, **kwargs):
        resp = self._scripted[self.calls]
        self.calls += 1
        return resp


class FakeOpenAI:
    def __init__(self, scripted):
        self.chat = SimpleNamespace(completions=FakeCompletions(scripted))


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
    fake = FakeOpenAI(scripted)
    monkeypatch.setattr(qa_mod, "get_openai", lambda: fake)

    def fake_run_sql(sql, settings=None):
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
