"""
backend/db.py — SQLite database driver with user and ticket persistence.
"""
import json
import os
import random
import secrets
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

DB_PATH = Path(__file__).resolve().parent / "users.db"

def get_db():
    # timeout: wait up to 30s for a lock instead of raising "database is
    # locked" immediately — matters once concurrent callers are all writing
    # transcript lines at once. WAL lets readers proceed without blocking on
    # a writer (and vice versa), which plain "rollback journal" mode doesn't.
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

TICKET_STATUSES = {"OPEN", "CLOSED"}

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                phone_number TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        existing_user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "phone_number" not in existing_user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_code TEXT UNIQUE NOT NULL,
                user_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                priority TEXT NOT NULL,
                assigned_desk TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                human_requested INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Lightweight migration: older databases created before these columns
        # existed just get them added, defaulting existing rows to OPEN/0.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
        if "status" not in existing_columns:
            conn.execute("ALTER TABLE tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'OPEN'")
        if "human_requested" not in existing_columns:
            conn.execute("ALTER TABLE tickets ADD COLUMN human_requested INTEGER NOT NULL DEFAULT 0")

        # Call transcripts, kept permanently — never cleared on session end or
        # navigation. ticket_code links a transcript line back to the ticket
        # it was recorded under (a call is always tied to the caller's most
        # recent ticket; see server.py's voice_relay).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_code TEXT NOT NULL,
                user_email TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_ticket ON transcripts(ticket_code)")

        # Network incidents — one row per incident, ongoing or resolved.
        # "Current status" for a location is just its most recent row
        # (resolved_at IS NULL means still ongoing), so this single table
        # backs both check_network_status (latest row) and
        # check_incident_history (all rows) in support_tools.py, instead of
        # keeping two separate in-memory dicts that could drift apart.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS network_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT UNIQUE NOT NULL,
                location TEXT NOT NULL,
                status TEXT NOT NULL,
                affected_services TEXT NOT NULL,
                eta TEXT,
                summary TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                resolved_at TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_location ON network_incidents(location)")

        # Customer accounts — one row per phone number, looked up by
        # restart_connection in support_tools.py. Every new signup gets a
        # placeholder row at registration; once that phone number's first
        # ticket is filed, apply_ticket_scenario_to_account derives real
        # plan/5G/location attributes FROM the ticket's actual problem (see
        # _scenario_from_ticket), so the account matches what the caller
        # reports instead of being assigned independently at random.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                phone_number TEXT PRIMARY KEY,
                user_email TEXT,
                customer_name TEXT NOT NULL,
                plan TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                line_resets INTEGER NOT NULL DEFAULT 0,
                five_g_enabled INTEGER NOT NULL DEFAULT 1,
                location TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(user_email)")

        existing_account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
        if "location" not in existing_account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN location TEXT")

        # Session tokens — issued on register/login, required on every
        # authenticated REST call and on /ws/voice, so the server derives
        # "who is this caller" from a verified token instead of trusting a
        # client-supplied email/ticket_id query param (see get_session_user).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_email TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
        """)

        conn.commit()

    _seed_accounts_if_empty()

    _seed_network_incidents_if_empty()

def _hours_ago(hours: float) -> str:
    # Timestamps are computed relative to call time (not hardcoded dates) so
    # seeded incidents stay "this morning"/"yesterday" relative to whenever
    # the demo is actually run, instead of silently going stale.
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

def _seed_network_incidents_if_empty() -> None:
    """Populates network_incidents with a realistic mock dataset the voice
    agent can actually query, the first time the table is empty (a fresh DB,
    or one created before this table existed). Never re-seeds an existing
    dataset, so real/edited rows are never clobbered on server restart."""
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM network_incidents").fetchone()["n"]
        if count > 0:
            return

        seed_rows = [
            # (incident_id, location, status, affected_services, eta, summary, started_hours_ago, resolved_hours_ago)
            ("NET-4471", "downtown", "outage", ["voice", "data"], "45 minutes",
             "Ongoing outage affecting voice and data in Downtown. Field crew dispatched to a damaged fiber line.",
             1.5, None),
            ("NET-4390", "downtown", "resolved", ["voice", "data"], None,
             "Yesterday's outage in Downtown, caused by a power failure at the local cell site. Backup power restored and full service confirmed the same evening.",
             27, 24.5),
            ("NET-4468", "riverside", "degraded", ["data"], "20 minutes",
             "Ongoing degraded data speeds in Riverside due to a capacity overload during peak hours.",
             0.75, None),
            ("NET-4355", "riverside", "resolved", ["voice"], None,
             "Brief voice service disruption two days ago from a routine tower firmware update. Resolved once the update completed.",
             50, 48),
            ("NET-4402", "harbor point", "resolved", ["voice", "data", "sms"], None,
             "Yesterday's regional outage in Harbor Point after a scheduled network upgrade ran long. Fully resolved and confirmed stable.",
             20, 16),
        ]

        for incident_id, location, status, services, eta, summary, started_hours_ago, resolved_hours_ago in seed_rows:
            conn.execute(
                """INSERT INTO network_incidents
                   (incident_id, location, status, affected_services, eta, summary, started_at, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    incident_id, location, status, json.dumps(services), eta, summary,
                    _hours_ago(started_hours_ago),
                    _hours_ago(resolved_hours_ago) if resolved_hours_ago is not None else None,
                ),
            )
        conn.commit()

def _incident_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "incident_id": row["incident_id"],
        "location": row["location"],
        "status": row["status"],
        "affected_services": json.loads(row["affected_services"]),
        "eta": row["eta"],
        "summary": row["summary"],
        "started_at": row["started_at"],
        "resolved_at": row["resolved_at"],
    }

def get_current_network_status(location: str) -> Optional[Dict[str, Any]]:
    """Returns the most recent incident on file for a location (ongoing or
    resolved), or None if nothing is on record — meaning the location is
    assumed operational."""
    key = location.strip().lower()
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM network_incidents WHERE location = ?
               ORDER BY started_at DESC LIMIT 1""",
            (key,),
        ).fetchone()
        return _incident_row_to_dict(row) if row else None

def get_incident_history(location: str) -> List[Dict[str, Any]]:
    """Returns every incident on file for a location, most recent first."""
    key = location.strip().lower()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM network_incidents WHERE location = ?
               ORDER BY started_at DESC""",
            (key,),
        ).fetchall()
        return [_incident_row_to_dict(r) for r in rows]

# Default plan pool a brand-new account (no ticket yet) is randomly given,
# just so it isn't blank before any ticket exists to derive a scenario from.
# Once a ticket is filed, _scenario_from_ticket below overrides these fields
# to actually match what the caller says their problem is.
DEFAULT_PLANS: List[str] = ["Unlimited Plus", "Basic 5GB", "Family Share 10GB"]

def _random_default_scenario() -> Dict[str, Any]:
    return {"plan": random.choice(DEFAULT_PLANS), "five_g_enabled": True}

# Known incident locations a "network/outage" ticket can be tied to — kept
# in sync with the seed rows in _seed_network_incidents_if_empty below.
_INCIDENT_LOCATIONS = ["downtown", "riverside", "harbor point"]

def _scenario_from_ticket(subject: str, description: str, category: str) -> Dict[str, Any]:
    """Derives mock account attributes (plan, 5G status, and optionally a
    network_incidents location to tie the account to) FROM a ticket's own
    content, instead of assigning them at random — so a "no 5G" ticket's
    phone number actually has 5g_enabled=False, an outage-in-Downtown
    ticket's phone number is actually tied to the Downtown incident, etc.
    A small amount of randomness is layered on top (line_resets starting
    count, plan tier when the ticket doesn't specify one) so accounts within
    the same problem type still aren't all byte-identical — useful when
    testing several tickets of the same kind side by side."""
    text = f"{subject} {description}".lower()

    scenario: Dict[str, Any] = {
        "plan": random.choice(DEFAULT_PLANS),
        "five_g_enabled": True,
        "location": None,
        "starting_line_resets": random.randint(0, 2),
    }

    # 5G / plan-tier complaints ("no 5G", "can't use my 5G", "not getting 5G")
    if "5g" in text:
        scenario["five_g_enabled"] = False
        scenario["plan"] = random.choice([
            "Basic 5GB (4G LTE only)",
            "Family Share 10GB (4G LTE only)",
        ])
    # Billing/payment-flavored tickets get a premium plan, since these are
    # usually "I'm paying for X but not getting it" complaints.
    elif "Billing" in category or "payment" in text or "bill" in text:
        scenario["plan"] = "Unlimited Plus"
        scenario["five_g_enabled"] = True

    # Outage/connectivity complaints mentioning a known incident location —
    # tie the account to that real seeded incident so check_network_status /
    # check_incident_history and this account tell a consistent story.
    for loc in _INCIDENT_LOCATIONS:
        if loc in text:
            scenario["location"] = loc
            break
    else:
        if "outage" in text or "no signal" in text or "no service" in text or "Network" in category:
            # Complaint clearly about connectivity but didn't name a known
            # location — pick one of the seeded incident areas at random so
            # check_network_status still has something real to report.
            scenario["location"] = random.choice(_INCIDENT_LOCATIONS)

    return scenario

def _seed_accounts_if_empty() -> None:
    """Populates accounts with the original hand-picked demo numbers the
    first time the table is empty — real signups get their own row at
    registration instead (see create_account_for_user), this just keeps the
    known test numbers (5551234567 etc.) available after migrating off the
    old in-memory MOCK_ACCOUNTS dict in support_tools.py."""
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        if count > 0:
            return

        seed_rows = [
            ("5551234567", "Alex Mercer", "Unlimited Plus", True),
            ("5559876543", "Jamie Lee", "Basic 5GB", True),
            ("123456789", "Morgan Reyes", "Basic 5GB (4G LTE only)", False),
        ]
        for phone_number, customer_name, plan, five_g in seed_rows:
            conn.execute(
                """INSERT INTO accounts (phone_number, customer_name, plan, status, line_resets, five_g_enabled)
                   VALUES (?, ?, ?, 'ACTIVE', 0, ?)""",
                (phone_number, customer_name, plan, 1 if five_g else 0),
            )
        conn.commit()

def _account_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "phone_number": row["phone_number"],
        "customer_name": row["customer_name"],
        "plan": row["plan"],
        "status": row["status"],
        "line_resets": row["line_resets"],
        "5g_enabled": bool(row["five_g_enabled"]),
        "location": row["location"],
    }

def create_account_for_user(phone_number: str, customer_name: str, user_email: str) -> Dict[str, Any]:
    """Creates an account row for a newly registered user, keyed by their
    phone number, with a placeholder plan/5G scenario (there's no ticket yet
    to derive one from). Raises sqlite3.IntegrityError if the phone number
    is already in use. Once this phone number's first ticket is filed,
    apply_ticket_scenario_to_account overrides these fields to actually
    match the reported problem."""
    key = phone_number.strip().replace("-", "").replace(" ", "")
    scenario = _random_default_scenario()

    with get_db() as conn:
        conn.execute(
            """INSERT INTO accounts (phone_number, user_email, customer_name, plan, status, line_resets, five_g_enabled)
               VALUES (?, ?, ?, ?, 'ACTIVE', 0, ?)""",
            (key, user_email.strip().lower(), customer_name, scenario["plan"], 1 if scenario["five_g_enabled"] else 0),
        )
        conn.commit()

        row = conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (key,)).fetchone()
        return _account_row_to_dict(row)

def apply_ticket_scenario_to_account(phone_number: str, subject: str, description: str, category: str) -> Optional[Dict[str, Any]]:
    """Updates a phone number's account row to match a newly filed ticket's
    actual problem (5G/plan status, and a tie-in to a real seeded network
    incident location where relevant) — see _scenario_from_ticket. No-op
    (returns None) if the phone number has no account on file, e.g. a guest
    checkout or a pre-migration user with no linked account."""
    key = phone_number.strip().replace("-", "").replace(" ", "")
    scenario = _scenario_from_ticket(subject, description, category)

    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE accounts SET plan = ?, five_g_enabled = ?, location = ?,
               line_resets = ? WHERE phone_number = ?""",
            (scenario["plan"], 1 if scenario["five_g_enabled"] else 0, scenario["location"],
             scenario["starting_line_resets"], key),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (key,)).fetchone()
        return _account_row_to_dict(row)

def get_account_by_phone(phone_number: str) -> Optional[Dict[str, Any]]:
    key = phone_number.strip().replace("-", "").replace(" ", "")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (key,)).fetchone()
        return _account_row_to_dict(row) if row else None

def get_account_by_email(user_email: str) -> Optional[Dict[str, Any]]:
    """Looks up the account linked to a logged-in caller's email — used by
    the voice relay to hand Maya the caller's phone number as context
    instead of making her ask for it before she can use restart_connection."""
    key = user_email.strip().lower()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE user_email = ?", (key,)).fetchone()
        return _account_row_to_dict(row) if row else None

def increment_line_resets(phone_number: str) -> Optional[Dict[str, Any]]:
    key = phone_number.strip().replace("-", "").replace(" ", "")
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE accounts SET line_resets = line_resets + 1 WHERE phone_number = ?", (key,)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (key,)).fetchone()
        return _account_row_to_dict(row)

def hash_password(password: str, salt_bytes: bytes = None) -> tuple[str, str]:
    if salt_bytes is None:
        salt_bytes = os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 100000)
    return pwd_hash.hex(), salt_bytes.hex()

def verify_password(password: str, stored_hash: str, salt_hex: str) -> bool:
    salt_bytes = bytes.fromhex(salt_hex)
    computed_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 100000).hex()
    return computed_hash == stored_hash

SESSION_TTL_HOURS = 24

def create_session(user_email: str) -> str:
    """Issues a new opaque session token for a just-authenticated user
    (called from register_user/authenticate_user). The token is what the
    client must send back as Authorization: Bearer <token> (or ?token=
    on the /ws/voice WebSocket) on every subsequent request — server.py's
    get_current_user dependency resolves it back to this user_email via
    get_session_user rather than trusting a client-supplied email."""
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_email, expires_at) VALUES (?, ?, ?)",
            (token, user_email.strip().lower(), expires_at),
        )
        conn.commit()
    return token

def get_session_user(token: str) -> Optional[str]:
    """Resolves a session token to the user_email it was issued for, or None
    if the token is missing/unknown/expired. Opportunistically sweeps
    expired sessions on every call instead of needing a separate cleanup
    job — cheap at this project's scale."""
    if not token:
        return None
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < CURRENT_TIMESTAMP")
        row = conn.execute(
            "SELECT user_email FROM sessions WHERE token = ? AND expires_at >= CURRENT_TIMESTAMP",
            (token,),
        ).fetchone()
        conn.commit()
        return row["user_email"] if row else None

def delete_session(token: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()

def register_user(email: str, name: str, password: str, phone_number: str) -> Dict[str, Any]:
    email_clean = email.strip().lower()
    name_clean = name.strip()
    phone_clean = phone_number.strip().replace("-", "").replace(" ", "")

    if not email_clean or not name_clean or not password or not phone_clean:
        return {"success": False, "error": "All fields, including phone number, are required."}

    if not phone_clean.isdigit() or not (7 <= len(phone_clean) <= 15):
        return {"success": False, "error": "Enter a valid phone number (digits only)."}

    pwd_hash, salt = hash_password(password)

    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (email, name, password_hash, salt, phone_number) VALUES (?, ?, ?, ?, ?)",
                (email_clean, name_clean, pwd_hash, salt, phone_clean),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return {"success": False, "error": "An account with this email already exists."}

    # A telecom support account (plan/5G scenario) is created alongside the
    # login, keyed by the same phone number restart_connection looks up —
    # so every new signup has real, randomly-varied account data for the
    # voice agent to work with instead of only the few hand-picked demo
    # numbers. A duplicate phone number is rare (random 9-10 digit test
    # entries) but not fatal to registration if it happens.
    try:
        create_account_for_user(phone_clean, name_clean, email_clean)
    except sqlite3.IntegrityError:
        pass

    return {
        "success": True,
        "user": {"id": cursor.lastrowid, "email": email_clean, "name": name_clean, "phone_number": phone_clean},
        "token": create_session(email_clean),
    }

def authenticate_user(email: str, password: str) -> Dict[str, Any]:
    email_clean = email.strip().lower()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, name, password_hash, salt, phone_number FROM users WHERE email = ?", (email_clean,))
        row = cursor.fetchone()

        if not row or not verify_password(password, row["password_hash"], row["salt"]):
            return {"success": False, "error": "Invalid email or password."}

        return {
            "success": True,
            "user": {"id": row["id"], "email": row["email"], "name": row["name"], "phone_number": row["phone_number"]},
            "token": create_session(row["email"]),
        }

def create_ticket(user_email: str, subject: str, description: str, category: str, priority: str) -> Dict[str, Any]:
    if "Billing" in category:
        assigned_desk = "Billing Desk"
    elif "Network" in category:
        assigned_desk = "Network Operations"
    elif "Account" in category:
        assigned_desk = "Account Services"
    else:
        assigned_desk = "Customer Care"

    with get_db() as conn:
        cursor = conn.cursor()
        # ticket_code is derived from the row's autoincrementing id (set right
        # after insert) so ticket numbers are sequential and human-readable,
        # instead of the old epoch-seconds-mod-100000 scheme that jumped
        # around unpredictably and wrapped every ~27.8 hours.
        cursor.execute(
            """INSERT INTO tickets (ticket_code, user_email, subject, description, category, priority, assigned_desk, status)
               VALUES ('PENDING', ?, ?, ?, ?, ?, ?, 'OPEN')""",
            (user_email.strip().lower(), subject.strip(), description.strip(), category, priority, assigned_desk)
        )
        new_id = cursor.lastrowid
        ticket_code = f"SR-{new_id:05d}"
        cursor.execute("UPDATE tickets SET ticket_code = ? WHERE id = ?", (ticket_code, new_id))
        conn.commit()

    # Make the caller's mock account actually reflect what they just
    # reported — a "no 5G" ticket's phone number gets 5g_enabled=False, an
    # outage ticket gets tied to a real seeded incident location, etc. — so
    # Maya's tool results line up with the problem the caller describes on
    # the call instead of a scenario picked at random independent of it.
    account = get_account_by_email(user_email)
    if account:
        apply_ticket_scenario_to_account(account["phone_number"], subject, description, category)

    return {
        "ticket_id": f"#{ticket_code}",
        "subject": subject.strip(),
        "description": description.strip(),
        "category": category,
        "priority": priority,
        "assigned_desk": assigned_desk,
        "status": "OPEN",
        "human_requested": False,
    }

def get_tickets(user_email: Optional[str] = None, status: Optional[str] = None):
    """status: "OPEN", "CLOSED", or None/"ALL" for both."""
    status_clean = status.strip().upper() if status else None
    if status_clean == "ALL":
        status_clean = None

    where_clauses = []
    params: list = []
    if user_email:
        where_clauses.append("user_email = ?")
        params.append(user_email.strip().lower())
    if status_clean:
        where_clauses.append("status = ?")
        params.append(status_clean)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT ticket_code, user_email, subject, description, category, priority, assigned_desk, status, human_requested, created_at
                FROM tickets {where_sql} ORDER BY id DESC""",
            params,
        )
        rows = cursor.fetchall()
        return [
            {
                "ticket_id": f"#{r['ticket_code']}",
                "user_email": r["user_email"],
                "subject": r["subject"],
                "description": r["description"],
                "category": r["category"],
                "priority": r["priority"],
                "assigned_desk": r["assigned_desk"],
                "status": r["status"],
                "human_requested": bool(r["human_requested"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

def get_ticket_by_code(ticket_code: str) -> Optional[Dict[str, Any]]:
    """ticket_code may be passed with or without its leading '#'."""
    code_clean = ticket_code.lstrip("#").strip()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT ticket_code, user_email, subject, description, category, priority, assigned_desk, status, human_requested, created_at
               FROM tickets WHERE ticket_code = ?""",
            (code_clean,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "ticket_id": f"#{row['ticket_code']}",
            "user_email": row["user_email"],
            "subject": row["subject"],
            "description": row["description"],
            "category": row["category"],
            "priority": row["priority"],
            "assigned_desk": row["assigned_desk"],
            "status": row["status"],
            "human_requested": bool(row["human_requested"]),
            "created_at": row["created_at"],
        }

def get_latest_ticket_for_user(user_email: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT ticket_code, subject, description, category, priority, assigned_desk, status, human_requested, created_at
               FROM tickets WHERE user_email = ? ORDER BY id DESC LIMIT 1""",
            (user_email.strip().lower(),)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "ticket_id": f"#{row['ticket_code']}",
            "subject": row["subject"],
            "description": row["description"],
            "category": row["category"],
            "priority": row["priority"],
            "assigned_desk": row["assigned_desk"],
            "status": row["status"],
            "human_requested": bool(row["human_requested"]),
        }

def update_ticket_status(ticket_id: str, status: str) -> Optional[Dict[str, Any]]:
    """Archives (CLOSED) or reopens (OPEN) a ticket. ticket_id may be passed
    with or without its leading '#'. Returns the updated ticket, or None if
    no ticket with that code exists."""
    status_clean = status.strip().upper()
    if status_clean not in TICKET_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {sorted(TICKET_STATUSES)}.")

    ticket_code = ticket_id.lstrip("#").strip()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE tickets SET status = ? WHERE ticket_code = ?", (status_clean, ticket_code))
        conn.commit()

        if cursor.rowcount == 0:
            return None

        cursor.execute(
            """SELECT ticket_code, user_email, subject, description, category, priority, assigned_desk, status, human_requested, created_at
               FROM tickets WHERE ticket_code = ?""",
            (ticket_code,),
        )
        row = cursor.fetchone()
        return {
            "ticket_id": f"#{row['ticket_code']}",
            "user_email": row["user_email"],
            "subject": row["subject"],
            "description": row["description"],
            "category": row["category"],
            "priority": row["priority"],
            "assigned_desk": row["assigned_desk"],
            "status": row["status"],
            "human_requested": bool(row["human_requested"]),
            "created_at": row["created_at"],
        }

def _set_human_requested(ticket_id: str, flag: bool) -> Optional[Dict[str, Any]]:
    ticket_code = ticket_id.lstrip("#").strip()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tickets SET human_requested = ? WHERE ticket_code = ?",
            (1 if flag else 0, ticket_code),
        )
        conn.commit()

        if cursor.rowcount == 0:
            return None

        cursor.execute(
            """SELECT ticket_code, user_email, subject, description, category, priority, assigned_desk, status, human_requested, created_at
               FROM tickets WHERE ticket_code = ?""",
            (ticket_code,),
        )
        row = cursor.fetchone()
        return {
            "ticket_id": f"#{row['ticket_code']}",
            "user_email": row["user_email"],
            "subject": row["subject"],
            "description": row["description"],
            "category": row["category"],
            "priority": row["priority"],
            "assigned_desk": row["assigned_desk"],
            "status": row["status"],
            "human_requested": bool(row["human_requested"]),
            "created_at": row["created_at"],
        }

def request_human_callback(ticket_id: str) -> Optional[Dict[str, Any]]:
    """Flags a ticket as needing a human agent to call the customer back.
    ticket_id may be passed with or without its leading '#'. Returns the
    updated ticket, or None if no ticket with that code exists."""
    return _set_human_requested(ticket_id, True)

def cancel_human_callback(ticket_id: str) -> Optional[Dict[str, Any]]:
    """Clears a previously requested human callback for a ticket (the
    caller changed their mind, or the AI agent resolved it after all).
    ticket_id may be passed with or without its leading '#'. Returns the
    updated ticket, or None if no ticket with that code exists."""
    return _set_human_requested(ticket_id, False)

def save_transcript_line(ticket_code: str, user_email: str, role: str, text: str) -> None:
    """Persists one transcript line permanently. Never deleted on session end,
    disconnect, or navigation — only ever appended to."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO transcripts (ticket_code, user_email, role, text) VALUES (?, ?, ?, ?)",
            (ticket_code.lstrip("#").strip(), user_email.strip().lower(), role, text),
        )
        conn.commit()

def get_transcripts_for_ticket(ticket_code: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, text, created_at FROM transcripts WHERE ticket_code = ? ORDER BY id ASC",
            (ticket_code.lstrip("#").strip(),),
        )
        return [{"role": r["role"], "text": r["text"], "created_at": r["created_at"]} for r in cursor.fetchall()]

init_db()