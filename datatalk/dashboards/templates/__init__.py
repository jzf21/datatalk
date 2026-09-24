"""Curated report templates: hand-written, tested SQL behind dashboard controls.

For sources whose schema DataTalk owns (a synced Jira source), the reports
people want are a known set with precise definitions, and generating their SQL
per request only adds ways to be subtly wrong. A template is instantiated into
an ordinary saved dashboard; see :mod:`.base` and :mod:`.instantiate`.
"""

from datatalk.dashboards.templates.registry import get, templates_for

__all__ = ["get", "templates_for"]
