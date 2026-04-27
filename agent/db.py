"""
SQLite-backed persistent lead store.

Replaces the in-memory _LEADS dict from conversation_handler.py.
Uses SQLAlchemy 2.0 with StaticPool so FastAPI worker threads share one
SQLite connection without "check_same_thread" errors.

Public interface
----------------
get_or_create(email)       → Lead
get_by_phone(phone)        → Lead | None
link_phone(email, phone)   → None
save_lead(lead)            → None
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import Column, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

# ── DB setup ──────────────────────────────────────────────────────────────────

_DB_URL = os.getenv("DATABASE_URL", "sqlite:///./leads.db")

_engine_kwargs: dict = {}
if _DB_URL.startswith("sqlite"):
    _engine_kwargs = {
        "connect_args": {"check_same_thread": False},
        "poolclass": StaticPool,
    }

_engine = create_engine(_DB_URL, **_engine_kwargs)
_Session = sessionmaker(bind=_engine, autocommit=False, autoflush=False)

_Base = declarative_base()


class _LeadRow(_Base):
    __tablename__ = "leads"

    email              = Column(String, primary_key=True)
    lead_id            = Column(String, nullable=False)
    phone              = Column(String, default="")
    status             = Column(String, default="new")
    history_json       = Column(Text,   default="[]")
    turns              = Column(Integer, default=0)
    created_at         = Column(String, nullable=False)
    profile_json       = Column(Text,   default="{}")
    booking_url        = Column(String, default="")
    hubspot_contact_id = Column(String, default="")
    updated_at         = Column(String, default="")


_Base.metadata.create_all(bind=_engine)


@contextmanager
def _session():
    db = _Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Lead value object ─────────────────────────────────────────────────────────

LeadStatus = Literal["new", "outreach_sent", "in_conversation", "qualified", "disqualified"]


@dataclass
class Lead:
    email:              str            = ""
    lead_id:            str            = field(default_factory=lambda: str(uuid.uuid4()))
    phone:              str            = ""
    status:             LeadStatus     = "new"
    history:            list[dict]     = field(default_factory=list)
    turns:              int            = 0
    created_at:         str            = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    profile:            dict           = field(default_factory=dict)
    booking_url:        str            = ""
    hubspot_contact_id: str            = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _row_to_lead(row: _LeadRow) -> Lead:
    return Lead(
        email              = row.email,
        lead_id            = row.lead_id or str(uuid.uuid4()),
        phone              = row.phone or "",
        status             = row.status or "new",
        history            = json.loads(row.history_json or "[]"),
        turns              = row.turns or 0,
        created_at         = row.created_at or "",
        profile            = json.loads(row.profile_json or "{}"),
        booking_url        = row.booking_url or "",
        hubspot_contact_id = row.hubspot_contact_id or "",
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── public API ────────────────────────────────────────────────────────────────

def get_or_create(email: str) -> Lead:
    """Load a lead from the DB, creating it if it doesn't exist."""
    with _session() as db:
        row = db.get(_LeadRow, email)
        if row is None:
            row = _LeadRow(
                email      = email,
                lead_id    = str(uuid.uuid4()),
                created_at = _now(),
            )
            db.add(row)
            db.flush()
        return _row_to_lead(row)


def get_by_phone(phone: str) -> Lead | None:
    """Reverse-lookup a lead by phone number."""
    with _session() as db:
        row = db.query(_LeadRow).filter(_LeadRow.phone == phone).first()
        return _row_to_lead(row) if row else None


def link_phone(email: str, phone: str) -> None:
    """Associate an Africa's Talking phone number with an email-keyed lead."""
    with _session() as db:
        row = db.get(_LeadRow, email)
        if row and phone:
            row.phone      = phone
            row.updated_at = _now()


def save_lead(lead: Lead) -> None:
    """Upsert all mutable fields for a lead."""
    with _session() as db:
        row = db.get(_LeadRow, lead.email)
        if row is None:
            row = _LeadRow(
                email      = lead.email,
                lead_id    = lead.lead_id,
                created_at = lead.created_at,
            )
            db.add(row)
        row.phone              = lead.phone
        row.status             = lead.status
        row.history_json       = json.dumps(lead.history)
        row.turns              = lead.turns
        row.profile_json       = json.dumps(lead.profile)
        row.booking_url        = lead.booking_url
        row.hubspot_contact_id = lead.hubspot_contact_id
        row.updated_at         = _now()
