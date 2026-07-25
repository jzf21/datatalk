"""Memory store tests.

The `store` fixture (tests/conftest.py) supplies an org-scoped store bound to a
rolled-back Postgres transaction and a deterministic keyword embedder, so
cosine rankings stay predictable.
"""

from datatalk.agent.blocks import Document, Heading, Paragraph, Table





def test_add_and_list_suggestions(store):
    s = store
    s.add_suggestion("For SLA metrics use the jira.sla table")
    s.add_suggestion("Revenue lives in billing.invoices")
    assert len(s.all_suggestions()) == 2


def test_retrieval_ranks_relevant_first(store):
    s = store
    s.add_suggestion("For SLA metrics use the jira.sla table")
    s.add_suggestion("Revenue lives in billing.invoices")
    s.add_suggestion("Bug counts come from jira.bugs")

    top = s.retrieve_suggestion_texts("report on SLA breaches", k=1)
    assert top == ["For SLA metrics use the jira.sla table"]


def test_delete_suggestion(store):
    s = store
    added = s.add_suggestion("account grouping should use account_id")
    s.delete_suggestion(added.id)
    assert s.all_suggestions() == []


def _sample_doc():
    from datatalk.agent.executor import QueryResult

    ds = QueryResult(
        columns=["month", "issues"],
        rows=[["2026-01", 42]],
        row_count=1,
        truncated=False,
        sql="SELECT ...",
    )
    from datatalk.agent.blocks import materialize

    return materialize(
        Document(
            blocks=[
                Heading(text="Report", level=1),
                Paragraph(text="Summary."),
                Table(dataset_id="q1", columns=["month", "issues"]),
            ]
        ),
        {"q1": ds},
    )


def test_save_and_get_report(store):
    s = store
    doc = _sample_doc()
    queries = [{"dataset_id": "q1", "sql": "SELECT ...", "row_count": 1, "columns": ["month", "issues"]}]
    saved = s.save_report("count issues", doc, queries)

    fetched = s.get_report(saved.id)
    assert fetched is not None
    # Document + queries round-trip.
    assert fetched.document.to_dict() == doc.to_dict()
    assert fetched.queries == queries
    # markdown is derived from the document (Analyze/memory keep working).
    assert "# Report" in fetched.markdown
    assert "2026-01 | 42" in fetched.markdown
    assert s.get_report(9999) is None


def test_qa_turns_crud(store):
    s = store
    saved = s.save_report("a report", _sample_doc(), [])
    assert s.list_qa_turns(saved.id) == []

    answer = Document(blocks=[Paragraph(text="Because of X.")])
    q = [{"dataset_id": "q1", "sql": "SELECT 1", "row_count": 1, "columns": ["x"]}]
    turn = s.add_qa_turn(saved.id, "Why?", answer, q)

    turns = s.list_qa_turns(saved.id)
    assert len(turns) == 1
    assert turns[0].id == turn.id
    assert turns[0].question == "Why?"
    assert turns[0].answer_document.to_dict() == answer.to_dict()
    assert turns[0].queries == q


