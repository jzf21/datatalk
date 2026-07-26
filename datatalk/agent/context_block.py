"""The context block for the agents that have no tools.

The Reporter and the Dashboard author never touch a warehouse and never call a
tool, so the file *tree* that reaches the Planner, Analyst and Q&A is useless to
them -- they could not act on a path. They get ``overview.md``'s body instead:
short by construction, and the place the workspace's metric vocabulary, units
and naming live, which is exactly what shows up in prose, chart titles and KPI
labels.

Deliberately not the ontology or playbook bodies. Those are what ``read_context``
is for; putting them here would recreate the every-prompt bloat the two-tier
design exists to avoid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from datatalk.llm.prompts import CONTEXT_BLOCK_TEMPLATE

if TYPE_CHECKING:
    from datatalk.context import TenantContext

# The overview is meant to be a couple of paragraphs. This is a backstop against
# an editor pasting a novel into it, not the primary limit (the API caps the
# stored body at memory.datacontext.MAX_BODY_CHARS).
_MAX_OVERVIEW_CHARS = 3000


def build_context_block(ctx: "TenantContext") -> str:
    """``overview.md``'s body, fenced -- or ``""`` when there is none."""
    overview = ctx.context_model.get("overview.md")
    if overview is None or not overview.body_md.strip():
        return ""
    return CONTEXT_BLOCK_TEMPLATE.format(
        overview=overview.body_md[:_MAX_OVERVIEW_CHARS]
    )
