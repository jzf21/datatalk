"""Every report template, by id and by the source types it runs on."""

from __future__ import annotations

from collections.abc import Iterable

from datatalk.dashboards.templates.base import ReportTemplate
from datatalk.dashboards.templates.jira import TEMPLATES as _JIRA

_ALL: dict[str, ReportTemplate] = {t.id: t for t in _JIRA}


def get(template_id: str) -> ReportTemplate | None:
    return _ALL.get(template_id)


def templates_for(source_types: Iterable[str]) -> list[ReportTemplate]:
    """Templates runnable on at least one of ``source_types``, in catalog order."""
    types = set(source_types)
    return [t for t in _ALL.values() if t.source_types & types]
