from backend.agent.evidence_display import (
    display_evidence_item,
    display_evidence_list,
    evidence_detail,
    evidence_heading,
)


def test_known_tools_map_to_english_not_function_names() -> None:
    heading = evidence_heading("get_trade_pair")
    assert heading == "Compared broker and desk prices for this pair"
    assert "get_trade_pair" not in heading
    assert "_" not in heading

    similar = evidence_heading("search_similar_breaks")
    assert similar == "Searched similar human-resolved cases"
    assert "search_similar_breaks" not in similar


def test_unknown_snake_case_does_not_leak_identifier() -> None:
    heading = evidence_heading("get_secret_internal_helper")
    assert heading == "Reviewed additional records"
    assert "get_secret" not in heading


def test_already_english_heading_is_kept() -> None:
    text = "Compared broker and desk prices for this pair"
    assert evidence_heading(text) == text


def test_detail_keeps_facts_and_strips_tool_prefix() -> None:
    assert (
        evidence_detail("AAPL: 1 split(s), 0 dividend(s) in window")
        == "AAPL: 1 split(s), 0 dividend(s) in window"
    )
    assert (
        evidence_detail(
            "get_corporate_actions error: cache miss",
            tool="get_corporate_actions",
        )
        == "Could not complete this check: cache miss"
    )


def test_display_evidence_item_for_dashboard() -> None:
    out = display_evidence_item(
        {
            "tool": "get_raw_records",
            "result_summary": "raw records: 1 broker, 1 desk",
        }
    )
    assert out["tool"] == "Compared broker and desk source records"
    assert out["result_summary"] == "raw records: 1 broker, 1 desk"
    assert "get_raw_records" not in out["tool"]


def test_display_evidence_list_skips_non_dicts() -> None:
    assert display_evidence_list(None) == []
    assert display_evidence_list(["nope"]) == []
    rows = display_evidence_list(
        [{"tool": "get_desk_metadata", "result_summary": "EQ-US: 0.02 break rate"}]
    )
    assert rows[0]["tool"] == "Looked up desk reference data"
    assert "EQ-US" in rows[0]["result_summary"]
