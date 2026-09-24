"""Report templates over a synced Jira source (see ``integrations/jira/schema.py``)."""

from datatalk.dashboards.templates.jira.backlog import BACKLOG
from datatalk.dashboards.templates.jira.flow import FLOW
from datatalk.dashboards.templates.jira.people import PEOPLE
from datatalk.dashboards.templates.jira.sprint import SPRINT_REPORT, VELOCITY

TEMPLATES = (SPRINT_REPORT, VELOCITY, FLOW, BACKLOG, PEOPLE)
