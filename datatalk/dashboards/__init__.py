"""Live dashboards: re-running a saved dashboard's queries, with filters.

Deliberately a top-level package rather than part of :mod:`datatalk.agent`,
because the point of a refresh is that it is *not* an agent run: no prompts, no
model, no ``ctx.openai``. It re-executes SQL that was authored once and
re-materializes the same authoring document the generation pipeline built, so a
refresh is deterministic and costs a few warehouse round trips instead of a
pipeline. The one place a model is involved -- rewriting a captured query into a
filterable template -- lives in :mod:`datatalk.agent.templatize`, on the
configuration path, and never runs during a refresh.
"""
