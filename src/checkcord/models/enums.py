# For all the "types" of entities/objects/metadata in the system, we use Enums
# When used it provides a strict number of options to choose from
from enum import StrEnum


class SourcePlatform(StrEnum):
    """The platform where the event originated."""

    SLACK = "slack"
    MS_TEAMS = "ms_teams"
    GITHUB = "github"
    JIRA = "jira"
    CONFLUENCE = "confluence"
    NOTION = "notion"
    LINEAR = "linear"
    GOOGLE_DRIVE = "google_drive"
    ZOOM = "zoom"
    DATADOG = "datadog"
    SENTRY = "sentry"
    AGENT_SESSION = "agent_session"
    CUSTOM = "custom"


class SourceType(StrEnum):
    """The specific type of the event from the source."""

    MESSAGE = "message"
    THREAD_REPLY = "thread_reply"
    PR_OPENED = "pr_opened"
    PR_COMMENT = "pr_comment"
    PR_REVIEW = "pr_review"
    COMMIT = "commit"
    TASK = "task"
    TICKET_CREATED = "ticket_created"
    TICKET_UPDATE = "ticket_update"
    TICKET_COMMENT = "ticket_comment"
    WIKI_PAGE = "wiki_page"
    MEETING_TRANSCRIPT = "meeting_transcript"
    INCIDENT = "incident"
    CI_PIPELINE = "ci_pipeline"
    AGENT_LOG = "agent_log"
    CUSTOM_PAYLOAD = "custom_payload"


class EventAction(StrEnum):
    """The action that occurred."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


class SignalStatus(StrEnum):
    """How far a person has confirmed an event; scoring weights confirmed, denied and expired."""

    UNPROCESSED = "unprocessed"
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    EXPIRED = "expired"


class Severity(StrEnum):
    """PR guardrail enforcement severity for a CanonicalContext."""

    HARD_BLOCK = "hard_block"
    SOFT_WARN = "soft_warn"


class AccessVisibility(StrEnum):
    """Broad visibility classification."""

    PUBLIC = "public"
    PRIVATE = "private"
    RESTRICTED = "restricted"


class AclType(StrEnum):
    """The type of Access Control List applying to this event."""

    CHANNEL_MEMBERS = "channel_members"
    REPO_COLLABORATORS = "repo_collaborators"
    PROJECT_ROLES = "project_roles"
    SPACE_VIEWERS = "space_viewers"
    ATTENDEES = "attendees"
    WORKSPACE_ADMINS = "workspace_admins"
    CUSTOM = "custom"
    OPEN = "open"


class ASTEdgeStatus(StrEnum):
    """The status of an AST context edge."""

    ACTIVE = "active"
    STALE = "stale"
    DEPRECATED = "deprecated"


class ConstraintType(StrEnum):
    """Type of business/technical constraint extracted."""

    REGULATORY = "regulatory"
    BUDGET = "budget"
    TECHNICAL = "technical"
    BUSINESS = "business"


class RiskLevel(StrEnum):
    """Risk severity level extracted."""

    HIGH = "high"
    MED = "med"
    LOW = "low"


class AuditStatus(StrEnum):
    """Where the verifier is with an extraction."""

    PENDING_AUDIT = "pending_audit"
    VERIFIED = "verified"
    REJECTED = "rejected"
