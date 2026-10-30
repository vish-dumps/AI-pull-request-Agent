import asyncio
import json

from scripts.demo import generate
from scripts.simulate_load import simulate


def test_simulation_accepts_deduplicates_drains_and_reads_saved_reviews():
    report = asyncio.run(simulate(requests=6, concurrency=3, model_delay_ms=0, duplicate_every=3))
    assert report["successful"] and report["mode"] == "in_process_simulation"
    assert report["boundaries"]["publishing"] is False
    outcome = report["outcome"]
    assert outcome["accepted"] == 6 and outcome["duplicates"] == 2
    assert outcome["unique_reviews"] == outcome["completed"] == 4
    assert outcome["model_calls"] == 16
    assert outcome["github_file_fetches"] == 1
    assert outcome["remaining_queue"] == outcome["inflight"] == 0
    assert not outcome["errors"]
    assert report["timings"]["acceptance_ms"]["p99"] >= report["timings"]["acceptance_ms"]["p50"]


def test_demo_artifacts_are_actual_complete_and_limited_mock_workflows(tmp_path):
    summary = asyncio.run(generate(tmp_path))
    assert summary["persisted"] is summary["published"] is False
    complete = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    limited = json.loads((tmp_path / "limited.json").read_text(encoding="utf-8"))
    assert complete["status"] == "COMPLETED" and complete["review"]["risk_score"] == 7.7
    assert limited["status"] == "PARTIAL" and limited["context_warnings"]
    assert len(complete["agent_results"]) == len(limited["agent_results"]) == 4
    assert "Demo review" in (tmp_path / "review.md").read_text(encoding="utf-8")
    assert "Incomplete review" in (tmp_path / "limited.md").read_text(encoding="utf-8")
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    agents = [
        event
        for event in events
        if event.get("agent") and event["message"] == "Specialized review finished"
    ]
    assert len(agents) == 8 and len({event["review_id"] for event in agents}) == 2
