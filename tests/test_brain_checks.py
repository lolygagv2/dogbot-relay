"""Voice-agent guards that do not need the API: number post-check, health blocklist, templates."""
from app.voice_agent.brain import Brain, allowed_numbers, health_hit, numbers_in, offending_numbers


def test_numbers_in_handles_digits_and_words():
    assert numbers_in("Max barked twelve times for 3 minutes") == {12.0, 3.0}


def test_allowed_numbers_from_tool_results_with_time_variants():
    data = [{"quiet_hours": {"start": "20:00", "end": "08:30"}, "totals": {"treats": 5, "bark_s": 330}}]
    allowed = allowed_numbers(data)
    assert {20.0, 8.0, 30.0, 5.0, 330.0}.issubset(allowed)
    assert 6.0 in allowed            # 330 s -> about 6 minutes
    assert 8.3 in allowed            # "8:30" spoken as 8.30
    assert offending_numbers("Quiet hours are now 8 pm to 8:30 am and he had 5 treats.", allowed) == []
    assert offending_numbers("He had 7 treats.", allowed) == [7.0]
    assert offending_numbers("One more thing: 5 treats.", allowed) == []   # 'one' always allowed


def test_health_blocklist():
    assert health_hit("Max barked twice today.") is None
    assert health_hit("He seems anxious and might need the vet.") in ("anxious", "vet")


def test_templates():
    b = Brain.__new__(Brain)
    ch = {"tool": "set_quiet_hours", "result": {"ok": True, "quiet_hours": {"start": "21:00", "end": "07:30"}}}
    assert Brain._readback_sentence(ch) == "Quiet hours are now 21:00 to 07:30."
    assert "21:00" in Brain._readback_tokens(ch) and "9" in Brain._readback_tokens(ch)
    t = b._template([ch], [{"ok": True}], ["treat_cap_reached"])
    assert t.startswith("Quiet hours are now 21:00 to 07:30.") and "daily treat cap" in t
