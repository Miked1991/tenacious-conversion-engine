"""Tests for the deterministic AI maturity scorer."""

import pytest
from agent.ai_maturity import score_ai_maturity


def _job(titles=None, eng_roles=0, ai_roles=0):
    return {"sample_titles": titles or [], "open_engineering_roles": eng_roles, "ai_role_count": ai_roles}


def _cb(stage=""):
    return {"funding_stage": stage}


class TestScoreAiMaturity:
    def test_no_signals_returns_zero(self):
        result = score_ai_maturity(_job(), _cb(), "generic.com", llm_estimate=0)
        assert result["score"] == 0

    def test_high_ai_titles_hit_score_three(self):
        titles = ["ML Engineer", "LLM Ops Engineer", "Prompt Engineer", "Deep Learning Lead"]
        result = score_ai_maturity(_job(titles=titles), _cb(), "acme.com")
        assert result["score"] == 3

    def test_single_ai_title_scores_two(self):
        result = score_ai_maturity(_job(titles=["Machine Learning Engineer"]), _cb(), "acme.com")
        assert result["score"] == 2

    def test_data_only_titles_score_one(self):
        titles = ["Data Scientist", "Data Engineer", "Analytics Engineer"]
        result = score_ai_maturity(_job(titles=titles), _cb(), "acme.com")
        assert result["score"] == 1

    def test_high_ai_role_ratio_scores_three(self):
        result = score_ai_maturity(_job(eng_roles=10, ai_roles=4), _cb(), "acme.com")
        assert result["score"] == 3

    def test_medium_ai_role_ratio_scores_two(self):
        result = score_ai_maturity(_job(eng_roles=10, ai_roles=2), _cb(), "acme.com")
        assert result["score"] == 2

    def test_ai_domain_boosts_to_two(self):
        result = score_ai_maturity(_job(), _cb(), "neural.io", llm_estimate=0)
        assert result["score"] == 2

    def test_llm_estimate_floor_capped_at_two(self):
        result = score_ai_maturity(_job(), _cb(), "generic.com", llm_estimate=3)
        assert result["score"] == 2

    def test_late_stage_funding_floor_one(self):
        result = score_ai_maturity(_job(), _cb(stage="series d"), "acme.com", llm_estimate=0)
        assert result["score"] == 1

    def test_github_signals_three_ai_repos(self):
        gh = {"repos": ["llm-api", "bert-finetune", "rag-pipeline"], "ai_commit_count": 0}
        result = score_ai_maturity(_job(), _cb(), "acme.com", github_signals=gh)
        assert result["score"] == 3

    def test_output_schema_keys_present(self):
        result = score_ai_maturity(_job(), _cb(), "acme.com")
        assert "score" in result
        assert "reason" in result
        assert "signals" in result
        sig = result["signals"]
        for key in ("job_title_hits", "tool_stack_hits", "github_repos",
                    "patents", "conference_talks", "ai_role_ratio", "domain_match"):
            assert key in sig

    def test_patent_signals_boost_score(self):
        patents = {"titles": ["Neural network-based classification model", "Deep learning inference"]}
        result  = score_ai_maturity(_job(), _cb(), "acme.com", patent_signals=patents)
        assert result["score"] >= 2

    def test_score_clamps_to_three(self):
        titles = ["LLM Engineer", "ML Ops Engineer", "Prompt Engineer", "AI Infrastructure Lead"]
        gh     = {"repos": ["llm-core", "rag-engine", "bert-service"], "ai_commit_count": 500}
        result = score_ai_maturity(_job(titles=titles, eng_roles=5, ai_roles=4), _cb(), "ai.io",
                                   github_signals=gh)
        assert result["score"] == 3
