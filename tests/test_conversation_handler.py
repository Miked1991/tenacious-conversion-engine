"""Tests for conversation state machine using an in-memory SQLite DB."""

import os
import pytest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import importlib
import agent.db as db_mod
importlib.reload(db_mod)

import agent.conversation_handler as conv_mod
importlib.reload(conv_mod)

from agent.db import get_or_create, save_lead, get_by_phone, link_phone, Lead

_MOCK_REPLY = "Thanks for your question — happy to explain how our engineers are vetted."
_MOCK_QUALIFIED = False


class TestGetOrCreate:
    def test_creates_new_lead_for_unknown_email(self):
        lead = get_or_create("alice@example.com")
        assert lead.email == "alice@example.com"
        assert lead.status == "new"
        assert lead.turns == 0

    def test_returns_same_lead_on_second_call(self):
        get_or_create("bob@example.com")
        lead2 = get_or_create("bob@example.com")
        assert lead2.status == "new"

    def test_persists_status_after_save(self):
        lead = get_or_create("charlie@example.com")
        lead.status = "outreach_sent"
        save_lead(lead)
        reloaded = get_or_create("charlie@example.com")
        assert reloaded.status == "outreach_sent"


class TestLinkPhone:
    def test_link_and_lookup_by_phone(self):
        get_or_create("dana@example.com")
        link_phone("dana@example.com", "+2510900000001")
        found = get_by_phone("+2510900000001")
        assert found is not None
        assert found.email == "dana@example.com"

    def test_unknown_phone_returns_none(self):
        result = get_by_phone("+0000000000")
        assert result is None


class TestHandleReply:
    def test_first_reply_sets_in_conversation(self):
        get_or_create("eve@example.com")
        with patch.object(conv_mod, "_llm_reply", return_value=_MOCK_REPLY), \
             patch.object(conv_mod, "_qualify", return_value=False):
            result = conv_mod.handle_reply("eve@example.com", "Tell me more.", "trace-1")
        assert result["status"] == "in_conversation"
        assert result["turns"] == 1
        assert result["agent_reply"] == _MOCK_REPLY

    def test_third_reply_qualifies_with_intent(self):
        get_or_create("frank@example.com")
        with patch.object(conv_mod, "_llm_reply", return_value=_MOCK_REPLY), \
             patch.object(conv_mod, "_qualify", return_value=False):
            conv_mod.handle_reply("frank@example.com", "Interesting.", "t1")
            conv_mod.handle_reply("frank@example.com", "How does onboarding work?", "t2")
        with patch.object(conv_mod, "_llm_reply", return_value=_MOCK_REPLY), \
             patch.object(conv_mod, "_qualify", return_value=True):
            result = conv_mod.handle_reply("frank@example.com", "Let's schedule a call.", "t3")
        assert result["qualified"] is True
        assert result["status"] == "qualified"

    def test_third_reply_without_intent_stays_in_conversation(self):
        get_or_create("grace@example.com")
        with patch.object(conv_mod, "_llm_reply", return_value=_MOCK_REPLY), \
             patch.object(conv_mod, "_qualify", return_value=False):
            conv_mod.handle_reply("grace@example.com", "Hmm.", "t1")
            conv_mod.handle_reply("grace@example.com", "Not sure.", "t2")
            result = conv_mod.handle_reply("grace@example.com", "Maybe later.", "t3")
        assert result["status"] == "in_conversation"

    def test_history_persists_across_calls(self):
        get_or_create("henry@example.com")
        with patch.object(conv_mod, "_llm_reply", return_value=_MOCK_REPLY), \
             patch.object(conv_mod, "_qualify", return_value=False):
            conv_mod.handle_reply("henry@example.com", "First message.", "t1")
        lead = get_or_create("henry@example.com")
        assert any(m["role"] == "user" for m in lead.history)
