"""
backend/db.py — SQLite database driver with user and ticket persistence.
"""
import os
import sqlite3
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Lightweight migration: older databases created before the status
        # column existed just get it added, defaulting existing rows to OPEN.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
        if "status" not in existing_columns:
            conn.execute("ALTER TABLE tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'OPEN'")

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

        conn.commit()

def hash_password(password: str, salt_bytes: bytes = None) -> tuple[str, str]:
    if salt_bytes is None:
        salt_bytes = os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 100000)
    return pwd_hash.hex(), salt_bytes.hex()

def verify_password(password: str, stored_hash: str, salt_hex: str) -> bool:
    salt_bytes = bytes.fromhex(salt_hex)
    computed_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 100000).hex()
    return computed_hash == stored_hash

def register_user(email: str, name: str, password: str) -> Dict[str, Any]:
    email_clean = email.strip().lower()
    name_clean = name.strip()

    if not email_clean or not name_clean or not password:
        return {"success": False, "error": "All fields are required."}

    pwd_hash, salt = hash_password(password)

    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (email, name, password_hash, salt) VALUES (?, ?, ?, ?)",
                (email_clean, name_clean, pwd_hash, salt),
            )
            conn.commit()
            return {
                "success": True,
                "user": {"id": cursor.lastrowid, "email": email_clean, "name": name_clean},
            }
        except sqlite3.IntegrityError:
            return {"success": False, "error": "An account with this email already exists."}

def authenticate_user(email: str, password: str) -> Dict[str, Any]:
    email_clean = email.strip().lower()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, name, password_hash, salt FROM users WHERE email = ?", (email_clean,))
        row = cursor.fetchone()

        if not row or not verify_password(password, row["password_hash"], row["salt"]):
            return {"success": False, "error": "Invalid email or password."}

        return {
            "success": True,
            "user": {"id": row["id"], "email": row["email"], "name": row["name"]},
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

    return {
        "ticket_id": f"#{ticket_code}",
        "subject": subject.strip(),
        "description": description.strip(),
        "category": category,
        "priority": priority,
        "assigned_desk": assigned_desk,
        "status": "OPEN",
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
            f"""SELECT ticket_code, user_email, subject, description, category, priority, assigned_desk, status, created_at
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
            """SELECT ticket_code, subject, description, category, priority, assigned_desk, status, created_at
               FROM tickets WHERE ticket_code = ?""",
            (code_clean,),
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
            "created_at": row["created_at"],
        }

def get_latest_ticket_for_user(user_email: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT ticket_code, subject, description, category, priority, assigned_desk, status, created_at
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
            """SELECT ticket_code, user_email, subject, description, category, priority, assigned_desk, status, created_at
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
            "created_at": row["created_at"],
        }

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