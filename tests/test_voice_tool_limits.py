"""Guard the Anthropic API's per-request strict-tool cap for the voice agent."""
from app.voice_agent.tools import MAX_STRICT_TOOLS, TOOLS


def test_strict_tool_count_within_api_limit():
    strict = [t["name"] for t in TOOLS if t.get("strict")]
    assert len(strict) <= MAX_STRICT_TOOLS, f"{len(strict)} strict tools (max {MAX_STRICT_TOOLS}): {strict}"


def test_tool_names_unique():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
