"""Voice-agent guards that do not need the API: number post-check, templates. (No topic blocklist.)"""
from app.voice_agent.brain import Brain, allowed_numbers, numbers_in, offending_numbers


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


def test_no_health_topic_restriction():
    import app.voice_agent.brain as brain
    import app.voice_agent.prompts as prompts
    assert not hasattr(brain, "health_hit") and not hasattr(prompts, "HEALTH_WORDS")
    assert "vet" not in prompts.SYSTEM_PROMPT.lower() and "health" not in prompts.SYSTEM_PROMPT.lower()


def test_templates():
    b = Brain.__new__(Brain)
    ch = {"tool": "set_quiet_hours", "result": {"ok": True, "quiet_hours": {"start": "21:00", "end": "07:30"}}}
    assert Brain._readback_sentence(ch) == "Quiet hours are now 21:00 to 07:30."
    assert "21:00" in Brain._readback_tokens(ch) and "9" in Brain._readback_tokens(ch)
    t = b._template([ch], [{"ok": True}], ["treat_cap_reached"])
    assert t.startswith("Quiet hours are now 21:00 to 07:30.") and "daily treat cap" in t


def test_persona_has_no_accent_or_dialect_instructions():
    """2026-11-07: the accent comes from the ElevenLabs voice; the persona is warm, dry, brief, plain English."""
    from app.voice_agent import prompts
    every = " ".join(v for k, v in vars(prompts).items() if k.isupper() and isinstance(v, str)).lower()
    for word in ("austral", "aussie", "arvo", "mate", "g'day", "slang", "dialect", "accent", "british", "american"):
        assert word not in every, word
    assert "warm, dry and brief" in prompts.SYSTEM_PROMPT and "plain, everyday english" in prompts.SYSTEM_PROMPT.lower()
    assert "one light joke in the start-up greeting only" in prompts.SYSTEM_PROMPT
    assert "joke" in prompts.BOOT_GREETING_INTENT
    for intent in (prompts.SG_REARM_INTENT, prompts.LOW_HOPPER_INTENT):
        assert "joke" not in intent
