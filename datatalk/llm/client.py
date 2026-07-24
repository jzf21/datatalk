"""OpenAI client wrapper.

M1 provides connectivity checks and embeddings. The agentic tool-calling loop
used for report generation is added in later milestones.
"""

from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from datatalk.config import Settings, get_settings


@lru_cache(maxsize=1)
def get_openai() -> OpenAI:
    """Return a cached OpenAI client configured from settings.

    Honors ``OPENAI_BASE_URL`` so any OpenAI-compatible provider (e.g. Nebius
    Token Factory) can be used with the same code.
    """
    settings = get_settings()
    kwargs: dict = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return OpenAI(**kwargs)


def ping(settings: Settings | None = None) -> str:
    """Make a tiny chat call to confirm the API key and model work.

    Returns the model's reply text. Raises on auth / network errors so the
    caller can surface the failure.
    """
    settings = settings or get_settings()
    client = get_openai()
    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        # Generous cap: reasoning models (e.g. gpt-oss) spend tokens thinking
        # before the visible answer, so a tiny cap yields empty content.
        max_tokens=256,
        temperature=0,
    )
    return (resp.choices[0].message.content or "").strip()


def embed(texts: list[str], settings: Settings | None = None) -> list[list[float]]:
    """Embed a batch of texts for few-shot memory retrieval."""
    settings = settings or get_settings()
    client = get_openai()
    resp = client.embeddings.create(model=settings.openai_embed_model, input=texts)
    return [item.embedding for item in resp.data]
