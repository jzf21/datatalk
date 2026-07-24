"""Block-document model tests: round-trip, materialize, flatten."""

from datatalk.agent.blocks import (
    Chart,
    Document,
    Heading,
    Paragraph,
    Table,
    document_to_text,
    materialize,
    parse_json_object,
)
from datatalk.agent.executor import QueryResult


def _dataset():
    return QueryResult(
        columns=["month", "issues", "resolved"],
        rows=[["2026-01", 10, 8], ["2026-02", 20, 15]],
        row_count=2,
        truncated=False,
        sql="SELECT ...",
    )


def test_round_trip_to_from_dict():
    doc = Document(
        blocks=[
            Heading(text="Overview", level=1),
            Paragraph(text="Some **bold** text."),
            Table(dataset_id="q1", columns=["month", "issues"]),
            Chart(
                chart_type="bar",
                title="Issues by month",
                dataset_id="q1",
                x_col="month",
                series_cols=["issues"],
            ),
        ]
    )
    restored = Document.from_dict(doc.to_dict())
    assert restored.to_dict() == doc.to_dict()
    assert isinstance(restored.blocks[0], Heading)
    assert isinstance(restored.blocks[2], Table)
    assert restored.blocks[2].dataset_id == "q1"


def test_materialize_table_and_chart():
    doc = Document(
        blocks=[
            Table(dataset_id="q1", columns=["month", "issues"]),
            Chart(
                chart_type="line",
                title="Trend",
                dataset_id="q1",
                x_col="month",
                series_cols=["issues", "resolved"],
            ),
        ]
    )
    out = materialize(doc, {"q1": _dataset()})

    table = out.blocks[0]
    assert isinstance(table, Table)
    # Materialized values equal the captured dataset values (no fabrication).
    assert table.columns == ["month", "issues"]
    assert table.rows == [["2026-01", 10], ["2026-02", 20]]

    chart = out.blocks[1]
    assert isinstance(chart, Chart)
    assert chart.x == {"label": "month", "values": ["2026-01", "2026-02"]}
    assert chart.series == [
        {"name": "issues", "values": [10, 20]},
        {"name": "resolved", "values": [8, 15]},
    ]


def test_materialize_all_columns_when_omitted():
    doc = Document(blocks=[Table(dataset_id="q1")])
    out = materialize(doc, {"q1": _dataset()})
    assert out.blocks[0].columns == ["month", "issues", "resolved"]
    assert out.blocks[0].rows == [["2026-01", 10, 8], ["2026-02", 20, 15]]


def test_materialize_missing_dataset_degrades_to_paragraph():
    doc = Document(blocks=[Table(dataset_id="nope", columns=["x"])])
    out = materialize(doc, {"q1": _dataset()})
    assert isinstance(out.blocks[0], Paragraph)
    assert "unavailable" in out.blocks[0].text


def test_materialize_bad_column_degrades_to_paragraph():
    doc = Document(
        blocks=[
            Chart(
                chart_type="bar",
                dataset_id="q1",
                x_col="month",
                series_cols=["not_a_column"],
            )
        ]
    )
    out = materialize(doc, {"q1": _dataset()})
    assert isinstance(out.blocks[0], Paragraph)
    assert "not_a_column" in out.blocks[0].text


def test_materialize_bad_chart_type_degrades():
    doc = Document(
        blocks=[Chart(chart_type="donut", dataset_id="q1", x_col="month", series_cols=["issues"])]
    )
    out = materialize(doc, {"q1": _dataset()})
    assert isinstance(out.blocks[0], Paragraph)


def test_document_to_text_flattens_materialized_doc():
    doc = materialize(
        Document(
            blocks=[
                Heading(text="Report", level=1),
                Paragraph(text="Summary line."),
                Table(dataset_id="q1", columns=["month", "issues"]),
                Chart(
                    chart_type="bar",
                    title="Issues",
                    dataset_id="q1",
                    x_col="month",
                    series_cols=["issues"],
                ),
            ]
        ),
        {"q1": _dataset()},
    )
    text = document_to_text(doc)
    assert "# Report" in text
    assert "Summary line." in text
    assert "month | issues" in text
    assert "2026-01 | 10" in text
    assert "Issues (bar)" in text


def test_parse_json_object_handles_code_fences():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('prefix {"b": 2} suffix') == {"b": 2}
    assert parse_json_object("not json") == {}
