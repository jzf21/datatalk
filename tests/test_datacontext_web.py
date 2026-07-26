"""Context-model endpoints: the CRUD contract and the isolation guarantees.

Deliberately small. It covers what a mistake here would cost -- a cross-org read,
a traversal path reaching the database, a lost edit -- and not the generation
agent, which needs a scripted fake and belongs with a fuller test pass.
"""

from tests.conftest import signup


def test_context_crud_roundtrip(api_client, db):
    org_id = signup(api_client)

    # Empty to start.
    r = api_client.get(f"/api/orgs/{org_id}/context")
    assert r.status_code == 200, r.text
    assert r.json() == {"doc": None, "files": [], "tree_preview": ""}

    # Create.
    r = api_client.post(
        f"/api/orgs/{org_id}/context/file",
        json={
            "path": "ontology/orders.md",
            "summary": "Order lifecycle",
            "body_md": "One row per order.",
            "covers": [{"source": "main", "table": "analytics.orders"}],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["origin"] == "human"
    assert r.json()["human_owned"] is True

    # Duplicate path.
    r = api_client.post(
        f"/api/orgs/{org_id}/context/file",
        json={"path": "ontology/orders.md"},
    )
    assert r.status_code == 409 and r.json()["detail"] == "context_file_exists"

    # Bad path shape is rejected by the model, not the database.
    r = api_client.post(
        f"/api/orgs/{org_id}/context/file", json={"path": "../etc/passwd"}
    )
    assert r.status_code == 422, r.text

    # Read one.
    r = api_client.get(
        f"/api/orgs/{org_id}/context/file", params={"path": "ontology/orders.md"}
    )
    assert r.status_code == 200 and r.json()["body_md"] == "One row per order."

    # Update.
    r = api_client.put(
        f"/api/orgs/{org_id}/context/file",
        params={"path": "ontology/orders.md"},
        json={"body_md": "One row per order. Exclude void.", "summary": "Orders"},
    )
    assert r.status_code == 200 and "Exclude void" in r.json()["body_md"]

    # Missing file.
    r = api_client.put(
        f"/api/orgs/{org_id}/context/file",
        params={"path": "ontology/nope.md"},
        json={"body_md": "x"},
    )
    assert r.status_code == 404 and r.json()["detail"] == "context_file_not_found"

    # Listing + export.
    r = api_client.get(f"/api/orgs/{org_id}/context")
    assert [f["path"] for f in r.json()["files"]] == ["ontology/orders.md"]

    r = api_client.get(f"/api/orgs/{org_id}/context/export")
    md = r.json()["files"][0]["markdown"]
    assert md.startswith("---\nsummary: Orders") and "Exclude void" in md
    assert "table: analytics.orders" in md

    # Delete.
    r = api_client.delete(
        f"/api/orgs/{org_id}/context/file", params={"path": "ontology/orders.md"}
    )
    assert r.status_code == 204
    assert api_client.get(f"/api/orgs/{org_id}/context").json()["files"] == []


def test_cross_org_context_is_404_not_403(api_client):
    signup(api_client)
    other = "00000000-0000-0000-0000-0000000000ff"
    r = api_client.get(f"/api/orgs/{other}/context")
    assert r.status_code == 404 and r.json()["detail"] == "org_not_found"


def test_generate_requires_a_connection(api_client):
    org_id = signup(api_client)
    r = api_client.post(f"/api/orgs/{org_id}/context/generate", json={})
    assert r.status_code == 409 and r.json()["detail"] == "no_connection"


def test_markdown_roundtrip():
    from datatalk.memory.datacontext import (
        SavedContextFile,
        from_markdown,
        to_markdown,
    )

    original = SavedContextFile(
        id=1,
        path="ontology/customers.md",
        summary="Customer identity and segments",
        body_md="## What it is\n\nA buyer.\n",
        generated_body_md=None,
        covers=[{"source": "main", "table": "crm.accounts"}],
        evidence=[],
        origin="human",
        generated_at="",
        edited_at="",
        created_at="",
        updated_at="",
    )
    back = from_markdown(original.path, to_markdown(original))
    assert back.summary == original.summary
    assert back.body_md.strip() == original.body_md.strip()
    assert back.covers == original.covers
