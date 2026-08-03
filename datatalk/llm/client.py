"""OpenAI client wrapper.

M1 provides connectivity checks and embeddings. The agentic tool-calling loop
used for report generation is added in later milestones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from datatalk import observability as obs

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
    """Embed a batch of texts for few-shot memory retrieval.

    Instrumented by hand: the Langfuse OpenAI integration patches the chat and
    responses endpoints, not ``embeddings``, so without this the memory
    retrieval that shapes every prompt would be invisible in the trace.
    """
    with obs.observe(
        "embed-texts",
        as_type=obs.EMBEDDING,
        model=ctx.settings.openai_embed_model,
        input=texts,
    ) as span:
        resp = ctx.openai.embeddings.create(
            model=ctx.settings.openai_embed_model, input=texts
        )
        usage = getattr(resp, "usage", None)
        span.update(
            # The vectors themselves are noise in a trace; what a reader needs
            # is that the call happened, on which model, for how many texts.
            output={"vectors": len(resp.data), "dimensions": len(resp.data[0].embedding) if resp.data else 0},
            usage_details=(
                {"input": usage.prompt_tokens, "total": usage.total_tokens}
                if usage is not None
                else None
            ),
        )
        return [item.embedding for item in resp.data]
