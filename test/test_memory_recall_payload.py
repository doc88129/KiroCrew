"""The actual HTTP/MCP JSON contains selected snippets, never full source rows."""

import json

import pytest
from test_member_memory_api import env as _memory_env
from test_member_memory_api import request

from kiro_crew import mcp_shared
from kiro_crew.dashboard.handlers import memory_member
from kiro_crew.mcp_tools import learn
from kiro_crew.memory_recall import MAX_RECALL_PAYLOAD_BYTES, recall_json
from kiro_crew.validation import build_tool_response

env = _memory_env


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
@pytest.mark.parametrize("legacy_oversized", [False, True])
async def test_long_episode_is_a_snippet_in_actual_http_and_mcp_json(
    env, monkeypatch, name, legacy_oversized
):
    tier = env.tiers[name]
    source = "PostgreSQL database decision. " + "Historical detail. " * 90 + "PRIVATE_TAIL_SENTINEL"
    assert tier.write_episodic(source, importance=1.0, defer_embedding=True)
    original = tier.get_episodic_list()[0]
    if legacy_oversized:
        # Older/restored SQLite rows can predate today's write-time 2000-char
        # admission cap. Retrieval must still project only its selected snippet.
        source = (
            "PostgreSQL database decision. "
            + "Historical detail. " * 2000
            + "PRIVATE_TAIL_SENTINEL"
        )
        tier.db.execute(f"UPDATE {tier._epi_rel} SET text=? WHERE id=?", (source, original["id"]))
        tier.db.commit()
        tier._invalidate_episodic_scoring()
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database", "store": name or "default"}, owner=True)
    )
    assert response.status == 200
    payload = json.loads(response.text)
    evidence = payload["retrieval"]["episodes"]
    assert [row["id"] for row in evidence] == [original["id"]]
    assert evidence[0]["text"] == source[:1500]
    assert evidence[0]["text_truncated"] is True
    assert evidence[0]["text"] in payload["episodic_context"]
    assert "PRIVATE_TAIL_SENTINEL" not in response.text
    assert len(response.body) <= MAX_RECALL_PAYLOAD_BYTES
    assert payload["total_chars"] <= 3000
    monkeypatch.setattr(learn.mcp_core, "_resolve_session_key_strict", lambda: "dashboard:alice")
    monkeypatch.setattr(learn.mcp_core, "_get", lambda *args, **kwargs: payload)
    result = learn.memory_recall("memory_recall", {"query": "PostgreSQL database"})
    assert len(result.encode("utf-8")) <= MAX_RECALL_PAYLOAD_BYTES
    assert "PRIVATE_TAIL_SENTINEL" not in result
    assert json.loads(result)["retrieval"]["episodes"] == evidence
    assert tier.get_episodic_list()[0]["text"] == source


@pytest.mark.parametrize("name", ["", "member-alice"])
def test_unicode_and_metadata_overhead_fit_whole_payload_with_matching_ids(env, name):
    tier = env.tiers[name]
    for index in range(18):
        assert (
            tier.set_semantic(
                f"project.database_{index}",
                "PostgreSQL database " + "🚀数据库" * 30,
                1.0,
                "user_explicit",
            )
            is None
        )
    result = tier.recall("PostgreSQL database", cap=12000)
    wire = recall_json(result)
    assert len(wire.encode("utf-8")) <= MAX_RECALL_PAYLOAD_BYTES
    unicode_wire = recall_json(result, ensure_ascii=False)
    assert "数据库" in unicode_wire
    assert len(unicode_wire.encode("utf-8")) <= MAX_RECALL_PAYLOAD_BYTES
    assert "error" not in result
    assert result["total_chars"] <= 12000
    assert result["retrieval"]["facts"]
    assert result["retrieval"].get("omitted_for_payload_budget", 0) > 0
    for row in result["retrieval"]["facts"]:
        assert f"[memory:{row['id']}] {row['snippet']}" in result["semantic_context"]
        assert "embedding" not in row and "value_json" not in row
    assert len(tier.get_all_semantic()) == 18


@pytest.mark.parametrize("name", ["", "member-alice"])
def test_selected_evidence_keeps_source_identity_and_compact_copy_provenance(
    env, monkeypatch, name
):
    tier = env.tiers[name]
    monkeypatch.setattr(
        tier,
        "_semantic_candidates_v2" if name else "_semantic_candidates_v1",
        lambda query: [
            {
                "key": "project.database",
                "value_json": '"PostgreSQL"',
                "source": "user_seed",
                "embedding": b"not transport data",
                "derived_from": json.dumps(
                    {"kind": "fact", "store": "member-bob", "item_id": "original-key"}
                ),
                "retrieval": {"reason": "keyword_match", "matched_terms": ["database"]},
                "unused_source_body": "DO_NOT_RETURN" * 10000,
            }
        ],
    )
    result = tier.recall("database")
    row = result["retrieval"]["facts"][0]
    assert row["id"] == "key:project.database"
    assert row["source"] == "user_seed"
    assert row["derived_from"] == {"kind": "fact", "store": "member-bob", "item_id": "original-key"}
    assert row["retrieval"]["matched_terms"] == ["database"]
    assert "DO_NOT_RETURN" not in recall_json(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
@pytest.mark.parametrize("content_length", [False, True])
async def test_actual_stdio_frame_counts_nested_json_escaping_and_content_wrapper(
    env, monkeypatch, name, content_length
):
    tier = env.tiers[name]
    for index in range(18):
        tier.set_semantic(
            f"project.database_{index}",
            "PostgreSQL database " + '🚀数据库\\"\n' * 25,
            1.0,
            "user_explicit",
        )
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database", "store": name or "default"}, owner=True)
    )
    assert response.status == 200
    assert len(response.body) <= MAX_RECALL_PAYLOAD_BYTES
    payload = json.loads(response.text)
    assert payload["retrieval"]["facts"]
    monkeypatch.setattr(learn.mcp_core, "_resolve_session_key_strict", lambda: "dashboard:alice")
    monkeypatch.setattr(learn.mcp_core, "_resolve_session_key", lambda: "dashboard:alice")
    monkeypatch.setattr(learn.mcp_core, "_get", lambda *args, **kwargs: payload)
    text = learn.mcp_core._call_tool("memory_recall", {"query": "PostgreSQL database"})
    # Capture the production writer's bytes, after its default ensure_ascii=True
    # JSON encoder. A UTF-8 count of the inner handler string misses this layer.
    frames = []
    monkeypatch.setattr(mcp_shared, "_use_content_length", content_length)
    monkeypatch.setattr(mcp_shared, "_stdout_fd", 98765)
    monkeypatch.setattr(mcp_shared, "_write_all", lambda fd, frame: frames.append(frame))
    request_id = "memory-recall-76c7803c-9890-4427-8500-51c7b531fc99"
    mcp_shared.respond(request_id, build_tool_response(text))
    assert len(frames) == 1
    frame = frames[0]
    assert len(frame) <= MAX_RECALL_PAYLOAD_BYTES
    if content_length:
        header, body = frame.split(b"\r\n\r\n", 1)
        assert int(header.removeprefix(b"Content-Length: ")) == len(body)
    else:
        assert frame.endswith(b"\n")
        body = frame
    assert b"\\u6570" in body
    envelope = json.loads(body)
    assert envelope["id"] == request_id
    assert envelope["result"] == {"content": [{"type": "text", "text": text}]}
    selected = json.loads(envelope["result"]["content"][0]["text"])
    assert selected == payload
    assert selected["total_chars"] <= 3000
    for row in selected["retrieval"]["facts"]:
        assert f"[memory:{row['id']}] {row['snippet']}" in selected["semantic_context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
async def test_final_redaction_cannot_expand_context_past_requested_char_budget(
    env, monkeypatch, name
):
    tier = env.tiers[name]
    for index in range(12):
        tier.set_semantic(
            f"project.database_{index}", "PostgreSQL database TOKEN" * 5, 1.0, "user_explicit"
        )

    def expand_redaction(value):
        if isinstance(value, str):
            return value.replace("TOKEN", "[REDACTED CREDENTIAL]" * 10)
        if isinstance(value, dict):
            return {key: expand_redaction(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand_redaction(item) for item in value]
        return value

    monkeypatch.setattr(memory_member, "_redact_memory_field", expand_redaction)
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database", "store": name or "default"}, owner=True)
    )
    payload = json.loads(response.text)
    assert payload["retrieval"]["facts"]
    assert "TOKEN" not in response.text
    assert payload["retrieval"]["omitted_for_payload_budget"] > 0
    assert payload["total_chars"] == sum(
        len(payload[f"{kind}_context"]) for kind in ("semantic", "episodic", "lessons")
    )
    assert payload["total_chars"] <= 3000
    assert len(response.body) <= MAX_RECALL_PAYLOAD_BYTES
    for row in payload["retrieval"]["facts"]:
        assert f"[memory:{row['id']}] {row['snippet']}" in payload["semantic_context"]
