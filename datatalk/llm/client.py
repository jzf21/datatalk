"""OpenAI client wrapper.

M1 provides connectivity checks and embeddings. The agentic tool-calling loop
used for report generation is added in later milestones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a circular import: context -> clients -> llm.client
    from datatalk.context import TenantContext


def ping(ctx: "TenantContext") -> str:
    """Make a tiny chat call to confirm the API key and model work.

    Returns the model's reply text. Raises on auth / network errors so the
    caller can surface the failure.
    """
    resp = ctx.openai.chat.completions.create(
        model=ctx.settings.openai_model,
        messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        # Generous cap: reasoning models (e.g. gpt-oss) spend tokens thinking
        # before the visible answer, so a tiny cap yields empty content.
        max_tokens=256,
        temperature=0,
    )
    return (resp.choices[0].message.content or "").strip()


def embed(texts: list[str], ctx: "TenantContext") -> list[list[float]]:
    """Embed a batch of texts for few-shot memory retrieval."""
    resp = ctx.openai.embeddings.create(
        model=ctx.settings.openai_embed_model, input=texts
    )
    return [item.embedding for item in resp.data]
