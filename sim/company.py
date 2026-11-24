"""Marrowfield Systems: the invented company the simulation runs on."""

from __future__ import annotations

from dataclasses import dataclass, field

DOMAIN = "marrowfield.example"
TEAM_ID = "T0MARROW01"
TEAM_NAME = "Marrowfield Systems"
ORG = "marrowfield"
ADMIN_GROUP = "checkcord-admins"
JIRA_PROJECTS = ("PAY", "OPS")


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    role: str
    slack_id: str
    github_login: str
    channels: tuple[str, ...]
    repos: tuple[str, ...]
    admin: bool = False
    slack_email: str | None = None
    style: str = "okta"
    github_email_public: bool = False

    @property
    def email(self) -> str:
        return f"{self.key}@{DOMAIN}"

    @property
    def profile_email(self) -> str:
        return self.slack_email or self.email

    @property
    def github_email(self) -> str | None:
        return self.email if self.github_email_public else None


@dataclass(frozen=True)
class Channel:
    name: str
    id: str
    private: bool
    members: tuple[str, ...] = ()


@dataclass
class Repo:
    full_name: str
    private: bool
    collaborators: tuple[str, ...]
    files: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.full_name.split("/")[1]


PEOPLE: tuple[Person, ...] = (
    Person(
        "ingrid",
        "Ingrid Vale",
        "CTO",
        "U0INGRID01",
        "ingrid-vale",
        ("eng", "payments-private", "security-private", "incidents", "eng-platform"),
        ("payments-api", "web-app", "handbook"),
        admin=True,
    ),
    Person("priya", "Priya Nair", "IT admin", "U0PRIYA001", "priya-nair", ("eng",), (), admin=True),
    Person(
        "tomas",
        "Tomas Brandt",
        "backend lead",
        "U0TOMAS001",
        "tomas-b",
        ("eng", "payments-private", "incidents", "eng-platform"),
        ("payments-api",),
        style="entra",
        github_email_public=True,
    ),
    Person(
        "aiko",
        "Aiko Sato",
        "backend dev",
        "U0AIKO0001",
        "aiko-sato",
        ("eng", "payments-private"),
        ("payments-api",),
        slack_email="Aiko@Marrowfield.example",
        github_email_public=True,
    ),
    Person(
        "dev",
        "Dev Malhotra",
        "frontend dev",
        "U0DEV00001",
        "devm",
        ("eng", "random"),
        ("web-app",),
        style="entra",
        github_email_public=True,
    ),
    Person(
        "rosa",
        "Rosa Imani",
        "security engineer",
        "U0ROSA0001",
        "rosa-imani",
        ("eng", "security-private"),
        ("payments-api", "web-app"),
        github_email_public=True,
    ),
    Person(
        "omar",
        "Omar Haddad",
        "SRE",
        "U0OMAR0001",
        "omar-h",
        ("eng", "incidents", "random", "eng-platform"),
        ("payments-api",),
        style="entra",
        github_email_public=True,
    ),
    Person(
        "lena",
        "Lena Fischer",
        "contractor",
        "U0LENA0001",
        "lena-fischer",
        ("eng",),
        ("web-app",),
        slack_email="lena@fischer-consulting.example",
        style="entra",
    ),
    Person(
        "kofi",
        "Kofi Mensah",
        "QA contractor",
        "U0KOFI0001",
        "kofi-mensah",
        ("eng",),
        ("web-app",),
        github_email_public=True,
    ),
)

BY_KEY = {p.key: p for p in PEOPLE}


def person(key: str) -> Person:
    return BY_KEY[key]


def _members(channel: str) -> tuple[str, ...]:
    return tuple(p.key for p in PEOPLE if channel in p.channels)


CHANNELS: tuple[Channel, ...] = (
    Channel("eng", "C0ENG00001", False, _members("eng")),
    Channel("random", "C0RANDOM01", False, _members("random")),
    Channel("incidents", "C0INCIDENT", False, _members("incidents")),
    Channel("payments-private", "C0PAYPRIV1", True, _members("payments-private")),
    Channel("security-private", "C0SECPRIV1", True, _members("security-private")),
    Channel("eng-platform", "C0ENGPLAT1", False, _members("eng-platform")),
)

CHANNEL_BY_NAME = {c.name: c for c in CHANNELS}


def channel(name: str) -> Channel:
    return CHANNEL_BY_NAME[name]


PAYMENTS_FILES = {
    "README.md": "# payments-api\n\nCharges, refunds and merchant webhooks for Marrowfield.\n",
    "payments_api/__init__.py": "",
    "payments_api/charges.py": '''"""Charges and refunds."""

from decimal import Decimal

from payments_api.ledger import LedgerClient, post_entry

TAX_RATE = Decimal("0.0825")


def quote_total(subtotal_cents: int) -> int:
    """Total in integer cents, tax included."""
    tax = (Decimal(subtotal_cents) * TAX_RATE).quantize(Decimal("1"))
    return subtotal_cents + int(tax)


def create_charge(ledger: LedgerClient, merchant_id: str, subtotal_cents: int, key: str) -> dict:
    if not key:
        raise ValueError("Idempotency-Key is required")
    total = quote_total(subtotal_cents)
    entry = post_entry(ledger, merchant_id, total, reference=key)
    return {"merchant_id": merchant_id, "amount_cents": total, "entry": entry}


def refund_charge(ledger: LedgerClient, charge_id: str, amount_cents: int) -> dict:
    entry = post_entry(ledger, charge_id, -amount_cents, reference=f"refund:{charge_id}")
    return {"charge_id": charge_id, "refunded_cents": amount_cents, "entry": entry}
''',
    "payments_api/webhooks.py": '''"""Merchant webhooks."""

import hashlib
import hmac
import time

MAX_RETRIES = 3


def verify_signature(secret: bytes, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_webhook(secret: bytes, body: bytes, signature: str) -> int:
    if not verify_signature(secret, body, signature):
        return 401
    return 202


def retry_delivery(send, payload: dict) -> bool:
    for attempt in range(MAX_RETRIES):
        if send(payload):
            return True
        time.sleep(2**attempt)
    return False
''',
    "payments_api/ledger.py": '''"""The ledger table and the only writer to it."""

from dataclasses import dataclass, field


@dataclass
class LedgerClient:
    rows: list[dict] = field(default_factory=list)

    def insert(self, row: dict) -> int:
        self.rows.append(row)
        return len(self.rows)


def post_entry(ledger: LedgerClient, account: str, amount_cents: int, reference: str) -> int:
    pending_row = {"account": account, "amount_cents": amount_cents, "reference": reference}
    return ledger.insert(pending_row)
''',
    "payments_api/logging_filters.py": '''"""Log filters."""


def mask_card(card: str) -> str:
    digits = "".join(ch for ch in card if ch.isdigit())
    return "*" * max(len(digits) - 4, 0) + digits[-4:]
''',
    "tests/test_charges.py": """from payments_api.charges import quote_total


def test_quote_total_is_integer_cents():
    assert quote_total(1000) == 1083
""",
}

WEB_FILES = {
    "README.md": "# web-app\n\nThe merchant checkout.\n",
    "src/checkout.ts": """import { createCharge } from "./api/client";
import { formatMoney } from "./format/money";

export async function checkout(merchantId: string, subtotalCents: number): Promise<string> {
  const key = crypto.randomUUID();
  const charge = await createCharge(merchantId, subtotalCents, key);
  return formatMoney(charge.amountCents);
}
""",
    "src/api/client.ts": """export interface Charge {
  merchantId: string;
  amountCents: number;
}

export async function createCharge(
  merchantId: string,
  subtotalCents: number,
  idempotencyKey: string,
): Promise<Charge> {
  const res = await fetch("/v1/charges", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey, "Content-Type": "application/json" },
    body: JSON.stringify({ merchantId, subtotalCents }),
  });
  return (await res.json()) as Charge;
}
""",
    "src/format/money.ts": """export function formatMoney(cents: number): string {
  return `${(cents / 100).toFixed(2)} EUR`;
}
""",
}

HANDBOOK_FILES = {
    "README.md": "# Marrowfield handbook\n\nHow we work.\n",
    "engineering/on-call.md": "# On-call\n\nEach team keeps a weekly rotation.\n",
    "engineering/reviews.md": "# Reviews\n\nEvery change gets one approving review.\n",
}


def _collaborators(repo: str) -> tuple[str, ...]:
    return tuple(p.github_login for p in PEOPLE if repo in p.repos)


REPOS: tuple[Repo, ...] = (
    Repo(f"{ORG}/payments-api", True, _collaborators("payments-api"), dict(PAYMENTS_FILES)),
    Repo(f"{ORG}/web-app", True, _collaborators("web-app"), dict(WEB_FILES)),
    Repo(f"{ORG}/handbook", False, _collaborators("handbook"), dict(HANDBOOK_FILES)),
)

REPO_BY_NAME = {r.name: r for r in REPOS}
PAYMENTS = f"{ORG}/payments-api"
WEB_APP = f"{ORG}/web-app"
HANDBOOK = f"{ORG}/handbook"

VENDOR_EVAL = Channel("vendor-eval", "C0VENDOR01", True, ("tomas", "aiko", "lena"))

BURST_DECISIONS: tuple[str, ...] = (
    "Decision: payout batches are capped at 10,000 entries per file.",
    "Decision: merchant API keys rotate every 90 days, enforced by key_service.",
    "Decision: the fraud score threshold for manual review is 0.85.",
    "Decision: refunds older than 180 days go through the support queue, not refund_charge.",
    "Decision: every ledger entry carries the request id in its reference field.",
    "Decision: webhook payloads are signed with HMAC-SHA256 only; no MD5 anywhere.",
    "Decision: the checkout page loads no third-party scripts except the card iframe.",
    "Decision: settlement reports are generated at 05:00 UTC, never earlier.",
    "Decision: currency conversion rates are cached for at most 15 minutes.",
    "Decision: merchant onboarding requires a verified bank account before the first payout.",
    "Decision: the admin back office is only reachable over the VPN.",
    "Decision: dispute notifications are emailed within one hour of receipt.",
    "Decision: test card numbers are rejected in production by the card validator.",
    "Decision: invoice PDFs are rendered by the invoice-service, not by payments-api.",
    "Decision: feature flags for payments live in the flags table, not in env vars.",
    "Decision: chargeback fees are booked to account 4900 in the ledger.",
    "Decision: the public status page is updated within ten minutes of an incident.",
    "Decision: Redis keys for idempotency expire after 24 hours.",
    "Decision: SEPA mandates are stored for 14 months after the last debit.",
    "Decision: every database migration ships with a written rollback step.",
)

BURST_CHATTER: tuple[str, ...] = (
    "morning all",
    "coffee machine on 3 is broken again",
    "anyone up for lunch at 12:30?",
    "lol",
    "brb",
    "nice work on the demo yesterday",
    "who has the projector adapter",
    "thanks!",
    "ok",
    "can someone review my PR when they have a sec",
    "the train was late again",
    "happy friday",
    "is the wifi slow for anyone else",
    "I'll be out tomorrow afternoon",
    "+1",
    "sounds good",
    "the standup moved to 10:15 today",
    "does anyone know where the whiteboard markers went",
    "great talk at the meetup",
    "my laptop is updating, give me five",
    "cake in the kitchen",
    "who is on the rota for the plants",
    "haha",
    "back",
    "can we push the sync by 15 minutes",
    "the new monitors look great",
    "reminder: team photo at 3",
    "thanks for the help earlier",
    "on it",
    "the elevator is out, take the stairs",
    "nice",
    "who broke the build? jk it was me",
    "parking lot is full today",
    "anyone have a USB-C charger",
    "I'm heading out, see you tomorrow",
    "welcome back!",
    "the playlist today is excellent",
    "ok will do",
    "pizza tonight?",
    "good morning",
)
