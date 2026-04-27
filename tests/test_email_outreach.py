"""Tests for email tone validation."""

import os
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from agent.email_outreach import _deterministic_tone_check, SEGMENT_LABELS
from agent.enrichment_pipeline import CompanyProfile


class TestDeterministicToneCheck:
    def test_clean_email_passes(self):
        ok, violations = _deterministic_tone_check(
            "One question about your engineering team",
            "We help engineering leads hire senior talent from East Africa in two weeks.",
        )
        assert ok
        assert violations == []

    def test_body_too_long_fails(self):
        body = " ".join(["word"] * 130)
        ok, violations = _deterministic_tone_check("Subject", body)
        assert not ok
        assert any("too long" in v for v in violations)

    def test_banned_buzzword_fails(self):
        ok, violations = _deterministic_tone_check(
            "We want to leverage synergy", "Let me tell you about our innovative disruption."
        )
        assert not ok
        assert any("buzzword" in v for v in violations)

    def test_ai_mention_fails(self):
        ok, violations = _deterministic_tone_check(
            "Our AI platform", "We use AI to transform your workflow."
        )
        assert not ok
        assert any("AI" in v for v in violations)

    def test_fm1_commitment_word_fails(self):
        ok, violations = _deterministic_tone_check(
            "Your discovery call",
            "I've booked you for Thursday at 3pm. Expect a calendar invite from me.",
        )
        assert not ok
        assert any("commit" in v for v in violations)

    def test_fm1_booking_commitment_in_subject_fails(self):
        ok, violations = _deterministic_tone_check(
            "I've scheduled your slot for Friday",
            "Looking forward to our call.",
        )
        assert not ok
        assert any("commit" in v for v in violations)

    def test_asking_for_call_is_allowed(self):
        ok, violations = _deterministic_tone_check(
            "Worth a quick call?",
            "Would it help to hop on a 20-minute call to see if there's a fit?",
        )
        assert ok


class TestSegmentLabels:
    def test_all_segments_have_labels(self):
        for seg in range(4):
            assert seg in SEGMENT_LABELS
