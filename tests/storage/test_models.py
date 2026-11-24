from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from checkcord.models.enums import ASTEdgeStatus
from checkcord.storage.models import (
    ASTContextEdge,
    ASTNode,
    Base,
    ChatChannel,
    ChatGroup,
    ChatUser,
    EnterpriseUser,
    GithubInstallation,
    MSTeamsInstallation,
    MsTeamsMeetingInstallation,
    SlackInstallation,
    ZoomInstallation,
)


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


def test_create_tables_and_models_sqlite() -> None:
    """Verify SQLAlchemy schema, types, and model mappings using an in-memory SQLite database."""
    engine = create_engine("sqlite:///:memory:")
    # CanonicalContext.summary_tsv is a Postgres GENERATED column whose
    # expression calls to_tsvector(), audit_log_entries uses a text array with a
    # GIN index, audience_containers a UUID array and connection_scopes and
    # connection_audiences text arrays;
    # SQLite has none of these.
    postgres_only = {
        "canonical_contexts",
        "audit_log_entries",
        "audience_containers",
        "connection_scopes",
        "connection_audiences",
    }
    tables_to_create = [
        table for name, table in Base.metadata.tables.items() if name not in postgres_only
    ]
    Base.metadata.create_all(engine, tables=tables_to_create)

    # Verify we can perform CRUD operations on all defined models
    with Session(engine) as session:
        tenant_id = uuid4()

        # 1. SlackInstallation
        slack_inst = SlackInstallation(
            team_id="T123456",
            tenant_id=tenant_id,
            bot_token="xoxb-test-token",
            installation_info={"ok": True},
        )
        session.add(slack_inst)

        # 2. MSTeamsInstallation
        ms_inst = MSTeamsInstallation(
            ms_tenant_id="ms-tenant-789",
            tenant_id=tenant_id,
            auth_payload={"client_id": "foo"},
        )
        session.add(ms_inst)

        # 2b. GithubInstallation
        gh_inst = GithubInstallation(
            repo_identifier="org/repo",
            tenant_id=tenant_id,
            personal_access_token="ghp_testtoken",
            webhook_secret="shhh_secret",
        )
        session.add(gh_inst)

        # ZoomInstallation
        zoom_inst = ZoomInstallation(
            zoom_account_id="zoom-acc-123",
            tenant_id=tenant_id,
            webhook_secret="zoom_webhook_secret_token",
        )
        session.add(zoom_inst)

        # MsTeamsMeetingInstallation
        teams_meet_inst = MsTeamsMeetingInstallation(
            ms_tenant_id="ms-tenant-meet-456",
            tenant_id=tenant_id,
            webhook_secret="teams_meet_webhook_secret",
        )
        session.add(teams_meet_inst)

        # ASTNode
        ast_node = ASTNode(
            tenant_id=tenant_id,
            repo_identifier="github.com/org/repo",
            file_path="src/main.py",
            symbol_name="my_function",
            node_type="function",
            merkle_hash="hash123",
        )
        session.add(ast_node)
        session.flush()

        # ASTContextEdge (mock canonical context id since we didn't save one here)
        edge = ASTContextEdge(
            ast_node_id=ast_node.id,
            canonical_id=uuid4(),
            tenant_id=tenant_id,
            status=ASTEdgeStatus.ACTIVE,
        )
        session.add(edge)

        # EnterpriseUser
        ent_user = EnterpriseUser(
            tenant_id=tenant_id,
            primary_email="alice@example.com",
            display_name="Alice Admin",
            active=True,
            scim_metadata={"dept": "engineering"},
        )
        session.add(ent_user)
        session.flush()

        # 3. ChatUser
        chat_user = ChatUser(
            tenant_id=tenant_id,
            platform="slack",
            native_id="U98765",
            display_name="Alice",
            email="alice@example.com",
            internal_id=ent_user.id,
            metadata_={"role": "admin"},
        )
        session.add(chat_user)

        # 4. ChatChannel
        chat_channel = ChatChannel(
            tenant_id=tenant_id,
            platform="slack",
            native_id="C98765",
            display_name="general",
            metadata_={"topic": "anything"},
        )
        session.add(chat_channel)

        # 5. ChatGroup
        chat_group = ChatGroup(
            tenant_id=tenant_id,
            platform="slack",
            native_id="G98765",
            display_name="team-a",
            metadata_={"purpose": "work"},
        )
        session.add(chat_group)

        session.commit()

        # Verify SlackInstallation
        db_slack = session.query(SlackInstallation).filter_by(team_id="T123456").first()
        assert db_slack is not None
        assert db_slack.bot_token == "xoxb-test-token"
        assert db_slack.installation_info == {"ok": True}

        # Verify MSTeamsInstallation
        db_ms = session.query(MSTeamsInstallation).filter_by(ms_tenant_id="ms-tenant-789").first()
        assert db_ms is not None
        assert db_ms.auth_payload == {"client_id": "foo"}

        # Verify GithubInstallation
        db_gh = session.query(GithubInstallation).filter_by(repo_identifier="org/repo").first()
        assert db_gh is not None
        assert db_gh.personal_access_token == "ghp_testtoken"
        assert db_gh.webhook_secret == "shhh_secret"

        # Verify EnterpriseUser
        db_ent = session.query(EnterpriseUser).filter_by(primary_email="alice@example.com").first()
        assert db_ent is not None
        assert db_ent.display_name == "Alice Admin"
        assert db_ent.scim_metadata == {"dept": "engineering"}
        assert db_ent.active is True

        # Verify ChatUser
        db_user = session.query(ChatUser).filter_by(native_id="U98765").first()
        assert db_user is not None
        assert db_user.display_name == "Alice"
        assert db_user.metadata_ == {"role": "admin"}
        assert db_user.internal_id == db_ent.id

        # Verify ChatChannel
        db_channel = session.query(ChatChannel).filter_by(native_id="C98765").first()
        assert db_channel is not None
        assert db_channel.display_name == "general"
        assert db_channel.metadata_ == {"topic": "anything"}

        # Verify ChatGroup
        db_group = session.query(ChatGroup).filter_by(native_id="G98765").first()
        assert db_group is not None
        assert db_group.display_name == "team-a"
        assert db_group.metadata_ == {"purpose": "work"}

        # Verify ZoomInstallation
        db_zoom = session.query(ZoomInstallation).filter_by(zoom_account_id="zoom-acc-123").first()
        assert db_zoom is not None
        assert db_zoom.webhook_secret == "zoom_webhook_secret_token"

        # Verify MsTeamsMeetingInstallation
        db_teams_meet = (
            session.query(MsTeamsMeetingInstallation)
            .filter_by(ms_tenant_id="ms-tenant-meet-456")
            .first()
        )
        assert db_teams_meet is not None
        assert db_teams_meet.webhook_secret == "teams_meet_webhook_secret"

        # Verify ASTNode and ASTContextEdge
        db_node = session.query(ASTNode).filter_by(symbol_name="my_function").first()
        assert db_node is not None
        assert db_node.repo_identifier == "github.com/org/repo"
        assert db_node.node_type == "function"
        assert db_node.merkle_hash == "hash123"

        db_edge = session.query(ASTContextEdge).filter_by(ast_node_id=db_node.id).first()
        assert db_edge is not None
        assert db_edge.status == ASTEdgeStatus.ACTIVE
