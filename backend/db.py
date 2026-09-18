"""
backend/db.py — SQLite database driver with user and ticket persistence.
"""
import os
import sqlite3
import hashlib
import time
from pathlib import Path
from typing import Dict, Any, Optional

DB_PATH = Path(__file__).resolve().parent / "users.db"

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
    ticket_code = f"SR-{int(time.time()) % 100000}"
    assigned_desk = "Core Auth Desk" if any(k in category for k in ["SSO", "Auth", "Permissions"]) else "Specialist Dispatch"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO tickets (ticket_code, user_email, subject, description, category, priority, assigned_desk)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ticket_code, user_email.strip().lower(), subject.strip(), description.strip(), category, priority, assigned_desk)
        )
        conn.commit()

    return {
        "ticket_id": f"#{ticket_code}",
        "subject": subject.strip(),
        "description": description.strip(),
        "category": category,
        "priority": priority,
        "assigned_desk": assigned_desk,
    }

def get_tickets(user_email: Optional[str] = None):
    with get_db() as conn:
        cursor = conn.cursor()
        if user_email:
            cursor.execute(
                """SELECT ticket_code, user_email, subject, description, category, priority, assigned_desk, created_at
                   FROM tickets WHERE user_email = ? ORDER BY id DESC""",
                (user_email.strip().lower(),)
            )
        else:
            cursor.execute(
                """SELECT ticket_code, user_email, subject, description, category, priority, assigned_desk, created_at
                   FROM tickets ORDER BY id DESC"""
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
                "created_at": r["created_at"],
            }
            for r in rows
        ]

def get_latest_ticket_for_user(user_email: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT ticket_code, subject, description, category, priority, assigned_desk, created_at
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
        }

init_db()