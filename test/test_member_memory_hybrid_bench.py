"""Evaluation integrity checks; these never pretend synthetic vectors measure semantics."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

import pytest

from kiro_crew.eval.bench import member_v2
from kiro_crew.eval.bench.admission_corpus import ADMISSION_TOPICS


def test_ranking_metrics_penalize_false_positives_missing_evidence_and_bad_order():
    result = member_v2.ranking_metrics(["wrong", "gold-a"], {"gold-a", "gold-b"})
    assert result["precision_returned"] == 0.5
    assert result["fragment_recall"] == 0.5
    assert result["topic_hit"] == 1
    assert result["ndcg_at_8"] == pytest.approx((1 / math.log2(3)) / (1 + 1 / math.log2(3)))
    assert member_v2.ranking_metrics([], {"gold-a"}) == {
        "precision_returned": 0,
        "fragment_recall": 0,
        "topic_hit": 0,
        "ndcg_at_8": 0,
    }


def test_evaluation_refuses_missing_model_evidence_before_opening_memory(tmp_path):
    with pytest.raises(RuntimeError, match="failed to embed corpus input 0"):
        member_v2.evaluate(lambda _: None, tmp_path)
    assert not (tmp_path / "memory_stores").exists()


@pytest.mark.parametrize(
    ("filename", "snapshot"),
    [
        ("member-v2-hybrid-qwen3.json", "snapshot_before_retention_change"),
        ("member-v2-hybrid-qwen3-current.json", "snapshot_after_retention_change"),
    ],
)
def test_committed_hybrid_report_preserves_its_measured_corpus_and_policy(filename, snapshot):
    path = Path(member_v2.__file__).parent / "data" / filename
    report = json.loads(path.read_text(encoding="utf-8"))
    corpus = json.dumps(
        [asdict(topic) for topic in ADMISSION_TOPICS], ensure_ascii=False, sort_keys=True
    )
    assert report["corpus_sha256"] == hashlib.sha256(corpus.encode()).hexdigest()
    # Snapshot identity separates measured policies without product subversions.
    # Historical naming normalization retains the original artifact's seal.
    assert report["policy_revision"] == "member-v2"
    assert report["benchmark_snapshot"] == snapshot
    if snapshot == "snapshot_before_retention_change":
        assert report["provenance"]["label_normalization"]["original_artifact_sha256"] == (
            "d2893f5646535f1fdbdf1bf18c24e89e05f459a6ad4fe017069734862aef913a"
        )
    assert len(report["model"]["sha256"]) == 64 and report["model"]["dimension"] == 1024
    assert {mode["mode"] for mode in report["modes"]} == set(member_v2.MODES)
    for mode in report["modes"]:
        assert {row["topic"] for row in mode["per_query"]} == {
            topic.topic_id for topic in ADMISSION_TOPICS
        }
        assert (
            sum(mode["admission"][key] for key in ("tp", "fp", "fn", "tn")) == mode["pairs"] == 5000
        )
        assert mode["context_bounds_passed"] and mode["context_max_chars"] <= 3000
        for phase in ("ranked", "context"):
            for metric, value in mode["macro_metrics"][phase].items():
                assert value == pytest.approx(
                    sum(row[phase][metric] for row in mode["per_query"]) / 50
                )
        for row in mode["per_query"]:
            assert all(item["evidence"]["admitted"] for item in row["selected"])
            assert all(item["evidence"]["algorithm"] == "member-v2" for item in row["selected"])
    assert all(all(checks.values()) for checks in report["edge_cases"].values())
