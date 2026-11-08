"""Claude reasoning loop for the voice agent (Dec 9 demo build, 2026-11-06).

Manual tool loop on `claude-opus-5` (adaptive thinking left on, effort low for voice
latency, server-side refusal fallbacks enabled). Guards that live in code, not only in
the prompt:

1. physical tools only when the caller may trigger them (ToolContext.allow_physical)
2. number post-check: every number in the reply must appear in the tool results, the
   data handed to the model, or the user's own words; otherwise one corrected retry,
   then a template reply built from the data
3. settings readback: after a successful settings tool the reply must quote the new value
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import Any, Optional

import anthropic

from app.config import get_settings
from app.voice_agent import tools as agent_tools
from app.voice_agent.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

MAX_ROUNDS = 4
BUDGET_S = 25.0
MEMORY_TURNS = 6
MEMORY_TTL_S = 1800

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_ALWAYS_OK = {1}  # "one more thing", "one sentence"
_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_HM = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def numbers_in(text: str) -> set[float]:
    out: set[float] = set()
    for m in _DIGITS.findall(text or ""):
        try:
            out.add(float(m))
        except ValueError:
            pass
    for w, n in _NUM_WORDS.items():
        if re.search(rf"\b{w}\b", (text or "").lower()):
            out.add(float(n))
    return out


def allowed_numbers(sources: list[Any]) -> set[float]:
    """Numbers the reply may contain: everything numeric in the data, plus 12h/24h hour variants."""
    allowed: set[float] = set(float(x) for x in _ALWAYS_OK)
    for src in sources:
        text = src if isinstance(src, str) else json.dumps(src, default=str)
        allowed |= numbers_in(text)
        for h, m in _HM.findall(text):
            h_i, m_i = int(h), int(m)
            allowed |= {float(h_i), float(m_i), float(h_i % 12 or 12)}
            if m_i:
                allowed.add(float(h_i) + m_i / 100.0)
        for n in list(allowed):
            # seconds -> minutes/hours rounding the model may do
            if n >= 60:
                allowed |= {float(round(n / 60)), float(round(n / 3600))} if n >= 3600 else {float(round(n / 60))}
    return allowed


def offending_numbers(reply: str, allowed: set[float]) -> list[float]:
    return sorted(n for n in numbers_in(reply) if n not in allowed)


def _tool_result_content(res: dict) -> Any:
    """JSON text, plus an image block when a tool fetched a still (take_photo / get_event_photo)."""
    b64 = res.pop("_image_b64", None)
    mt = res.pop("_media_type", None) or "image/jpeg"
    text = json.dumps(res, default=str)
    if not b64:
        return text
    if mt not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        mt = "image/jpeg"
    return [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}},
            {"type": "text", "text": text}]


class Memory:
    def __init__(self) -> None:
        self._turns: dict[str, deque] = {}

    def get(self, key: str) -> list[dict]:
        dq = self._turns.get(key)
        if not dq:
            return []
        now = time.time()
        return [{"role": r, "content": c} for (r, c, ts) in dq if now - ts < MEMORY_TTL_S]

    def add(self, key: str, role: str, content: str) -> None:
        dq = self._turns.setdefault(key, deque(maxlen=MEMORY_TURNS))
        dq.append((role, content, time.time()))


class Brain:
    def __init__(self) -> None:
        s = get_settings()
        self.model = s.agent_model
        self.client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key or None)
        self.memory = Memory()

    async def _create(self, messages: list[dict], tools: Optional[list[dict]], extra_system: str = "") -> Any:
        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        if extra_system:
            system.append({"type": "text", "text": extra_system})
        kwargs: dict[str, Any] = dict(
            # max_tokens is a hard ceiling: 3 short sentences ~ 80 tokens, plus headroom for tool calls
            model=self.model, max_tokens=300, system=system, messages=messages,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if tools:
            kwargs["tools"] = tools
        return await self.client.beta.messages.create(**kwargs)

    @staticmethod
    def _text(resp: Any) -> str:
        return " ".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    async def respond(self, *, user_text: str, ctx: agent_tools.ToolContext, memory_key: str,
                      dog_hint: Optional[str] = None, extra_data: Optional[dict] = None) -> dict:
        """Full tool loop for one utterance. Returns {text_out, tools_used, refused, rounds, guard}."""
        t0 = time.monotonic()
        history = self.memory.get(memory_key)
        extra = f"Context: {json.dumps(extra_data, default=str)}" if extra_data else ""
        if dog_hint:
            extra += f"\nThe dog in question is dog_id={dog_hint}."
        messages: list[dict] = history + [{"role": "user", "content": user_text}]
        tool_results_data: list[Any] = []
        tools_used: list[str] = []
        refused: list[str] = []
        settings_changes: list[dict] = []
        reply = ""
        rounds = 0
        guard_notes: list[str] = []

        while rounds < MAX_ROUNDS:
            rounds += 1
            try:
                resp = await self._create(messages, agent_tools.TOOLS, extra)
            except anthropic.APIStatusError as e:
                logger.error(f"[BRAIN] API error {e.status_code}: {e.message}")
                return {"text_out": "Sorry, I had trouble thinking just now. Try again in a moment.",
                        "tools_used": tools_used, "refused": refused, "rounds": rounds, "guard": ["api_error"]}
            except anthropic.APIConnectionError:
                return {"text_out": "Sorry, I can't reach my brain right now.", "tools_used": tools_used,
                        "refused": refused, "rounds": rounds, "guard": ["api_connection_error"]}
            except Exception as e:  # misconfiguration (no key), SDK errors: never 500 the voice path
                logger.error(f"[BRAIN] unexpected error: {type(e).__name__}: {e}")
                return {"text_out": "Sorry, I'm not set up to think right now.", "tools_used": tools_used,
                        "refused": refused, "rounds": rounds, "guard": ["brain_error"]}

            if resp.stop_reason == "refusal":
                reply = "I can't help with that one."
                guard_notes.append("model_refusal")
                break
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                reply = self._text(resp)
                break
            messages.append({"role": "assistant", "content": resp.content})
            if time.monotonic() - t0 > BUDGET_S:
                reply = self._text(resp) or "That's taking too long; try again in a moment."
                guard_notes.append("budget")
                break
            results = await asyncio.gather(*(agent_tools.dispatch(ctx, tu.name, dict(tu.input)) for tu in tool_uses))
            blocks = []
            for tu, res in zip(tool_uses, results):
                tools_used.append(tu.name)
                tool_results_data.append(res)
                if res.get("refused"):
                    refused.append(str(res.get("reason")))
                if tu.name in ("set_quiet_hours", "set_music", "set_sg_sensitivity", "set_silent_guardian", "schedule_mission", "cancel_mission",
                               "set_treat_schedule", "set_notify_rule", "set_coach") and res.get("ok"):
                    settings_changes.append({"tool": tu.name, "result": res})
                blocks.append({"type": "tool_result", "tool_use_id": tu.id, "content": _tool_result_content(res)})
            messages.append({"role": "user", "content": blocks})
        else:
            reply = reply or "I've done what I can for now."
            guard_notes.append("max_rounds")

        # ---- post-checks ----
        allowed = allowed_numbers([user_text, extra_data or {}, *tool_results_data])
        bad = offending_numbers(reply, allowed)
        if bad and "model_refusal" not in guard_notes:
            note = [f"Your previous reply contained the number(s) {', '.join(str(int(b) if b == int(b) else b) for b in bad)}, "
                    f"which are not in the data you were given. Rewrite it using only numbers from the tool results."]
            messages.append({"role": "assistant", "content": reply or "..."})
            messages.append({"role": "user", "content": "[WIM-Z guard] " + " ".join(note) + " Reply with the corrected spoken sentences only."})
            try:
                resp2 = await self._create(messages, None, extra)
                reply2 = self._text(resp2)
                if reply2 and not offending_numbers(reply2, allowed):
                    reply = reply2
                    guard_notes.append("regenerated")
                else:
                    reply = self._template(settings_changes, tool_results_data, refused)
                    guard_notes.append("template")
            except Exception as e:  # pragma: no cover
                logger.warning(f"[BRAIN] regeneration failed: {e}")
                reply = self._template(settings_changes, tool_results_data, refused)
                guard_notes.append("template")

        # readback: a settings change must be quoted
        for ch in settings_changes:
            vals = self._readback_tokens(ch)
            if vals and not any(v in reply for v in vals):
                reply = (reply + " " + self._readback_sentence(ch)).strip()
                guard_notes.append("readback_appended")

        reply = re.sub(r"\s+", " ", reply).strip() or "Done."
        self.memory.add(memory_key, "user", user_text)
        self.memory.add(memory_key, "assistant", reply)
        return {"text_out": reply, "tools_used": tools_used, "refused": refused, "rounds": rounds, "guard": guard_notes}

    async def speak_event(self, intent: str, data_sources: list[Any]) -> str:
        """One no-tool call for agent-initiated speech (boot greeting, sg_rearm, low_hopper)."""
        messages = [{"role": "user", "content": intent}]
        try:
            resp = await self._create(messages, None, "")
        except Exception as e:
            logger.error(f"[BRAIN] speak_event failed: {e}")
            return ""
        if resp.stop_reason == "refusal":
            return ""
        reply = self._text(resp)
        allowed = allowed_numbers([intent, *data_sources])
        if offending_numbers(reply, allowed):
            messages += [{"role": "assistant", "content": reply or "..."},
                         {"role": "user", "content": "[WIM-Z guard] Rewrite using only numbers present in the data. Spoken sentences only."}]
            try:
                resp2 = await self._create(messages, None, "")
                reply2 = self._text(resp2)
                if reply2 and not offending_numbers(reply2, allowed):
                    return re.sub(r"\s+", " ", reply2).strip()
            except Exception:  # pragma: no cover
                pass
            return ""
        return re.sub(r"\s+", " ", reply).strip()

    # ---- templates ----

    @staticmethod
    def _readback_tokens(change: dict) -> list[str]:
        r = change["result"]
        if change["tool"] == "set_quiet_hours":
            qh = r.get("quiet_hours") or {}
            toks = []
            for v in (qh.get("start"), qh.get("end")):
                if v:
                    h = int(v[:2])
                    toks += [v, str(h), str(h % 12 or 12)]
            return toks
        if change["tool"] == "set_music":
            return [str(r.get("volume"))] if r.get("volume") is not None else []
        if change["tool"] == "set_sg_sensitivity":
            return [str(r.get("level") or "")]
        if change["tool"] == "set_silent_guardian":
            state = "on" if r.get("silent_guardian") == "on" else "off"
            # "turned Silent Guardian on" / "turned on Silent Guardian" / "Silent Guardian is now on"
            return [f"Silent Guardian {state}", f"{state} Silent Guardian", f"Silent Guardian is {state}",
                    f"Silent Guardian is now {state}", f"Silent Guardian mode {state}"]
        if change["tool"] == "schedule_mission":
            v = r.get("time") or ""
            if not v:
                return []
            h = int(v[:2])
            return [v, f"{h}:{v[3:]}", f"{h % 12 or 12}:{v[3:]}"] + ([str(h % 12 or 12)] if v[3:] == "00" else [])
        if change["tool"] == "cancel_mission":
            return [t for t in (r.get("name"), "cancel", "Cancel", "removed", "deleted") if t]
        if change["tool"] == "set_coach":
            state = "on" if r.get("coach") == "on" else "off"
            toks = [f"Coach mode {state}", f"{state} Coach mode", f"Coach mode is {state}", f"Coach mode is now {state}",
                    f"Coach {state}", f"{state} Coach"]
            verbs = ["start Coach", "started Coach", "starting Coach"] if state == "on" else \
                ["stop Coach", "stopped Coach", "stopping Coach", "standby"]
            return toks + verbs
        if change["tool"] == "set_treat_schedule":
            ws = r.get("windows") or []
            if not ws:
                return ["no treat windows", "cleared", "removed"]
            toks = []
            for w in ws:
                v = w["start"]
                h = int(v[:2])
                toks += [v, f"{h}:{v[3:]}", f"{h % 12 or 12}:{v[3:]}"] + ([str(h % 12 or 12)] if v[3:] == "00" else [])
            return toks
        if change["tool"] == "set_notify_rule":
            rule = r.get("rule") or {}
            return [t for t in (rule.get("event_type", "").replace("_", " ").split(" ")[0], "notif", "alert") if t]
        return []

    @staticmethod
    def _readback_sentence(change: dict) -> str:
        r = change["result"]
        if change["tool"] == "set_quiet_hours":
            qh = r.get("quiet_hours") or {}
            return f"Quiet hours are now {qh.get('start')} to {qh.get('end')}."
        if change["tool"] == "set_music":
            return f"Music is {'on' if r.get('music_enabled') else 'off'} at volume {r.get('volume')}."
        if change["tool"] == "set_sg_sensitivity":
            return (f"Silent Guardian sensitivity is now {r.get('level')}: an episode starts after "
                    f"{r.get('min_episode_s')} seconds and escalates after {r.get('escalate_after_s')} seconds.")
        if change["tool"] == "set_silent_guardian":
            if r.get("simulated"):
                return f"Silent Guardian {r.get('silent_guardian')} was simulated, because no live robot is connected."
            return f"I've told the robot to turn Silent Guardian {r.get('silent_guardian')}."
        if change["tool"] == "schedule_mission":
            from app.services.schedule_tools import type_label
            what = f"{type_label(str(r.get('type')))} at {r.get('time')} for {r.get('duration_min'):g} minutes"
            if r.get("confirmed_by_robot") and r.get("next_fire_at"):
                return f"{what[0].upper()}{what[1:]} is scheduled, and the robot has confirmed it."
            return f"I've saved {what}, but the robot hasn't confirmed it yet."
        if change["tool"] == "cancel_mission":
            tail = "" if r.get("confirmed_by_robot") else ", but the robot hasn't confirmed yet"
            return f"I've cancelled {r.get('name') or r.get('mission_id')}{tail}."
        if change["tool"] == "set_coach":
            if r.get("simulated"):
                return f"Coach mode {r.get('coach')} was simulated, because no live robot is connected."
            return ("I've told the robot to start Coach mode." if r.get("coach") == "on"
                    else "I've told the robot to stop Coach mode and go to standby.")
        if change["tool"] == "set_treat_schedule":
            ws = r.get("windows") or []
            dm = r.get("default_multiplier")
            outside = f" and {dm:g}x the rest of the day" if dm is not None else ""
            if not ws:
                return f"There are no treat windows now; treats run at {dm:g}x all day." if dm is not None \
                    else "There are no treat windows now."
            parts = [f"{w['start']} to {w['end']} at {w['multiplier']:g}x" for w in ws]
            return f"Treat windows are now {', '.join(parts)}{outside}."
        if change["tool"] == "set_notify_rule":
            rule = r.get("rule") or {}
            what = (rule.get("event_type") or "event").replace("_", " ")
            w = rule.get("when") or {}
            bits = []
            if w.get("severity_in"):
                bits.append(f"{w['severity_in'][0]} severity or higher")
            if w.get("min_duration_s"):
                bits.append(f"at least {w['min_duration_s']:g} seconds")
            if rule.get("cooldown_s"):
                bits.append(f"at most once every {rule['cooldown_s']} seconds")
            if rule.get("attach") in ("still", "clip"):
                bits.append("with a photo" if rule["attach"] == "still" else "with a clip")
            return f"You'll now get a notification for each {what}" + (f", {', '.join(bits)}" if bits else "") + "."
        return ""

    def _template(self, changes: list[dict], results: list[Any], refused: list[str]) -> str:
        parts = [self._readback_sentence(c) for c in changes if self._readback_sentence(c)]
        if refused:
            reasons = {
                "treat_cap_reached": "the daily treat cap is reached",
                "treat_rate": "treats are limited per hour",
                "quiet_hours_widen_only": "quiet hours can only be widened from here",
                "robot_offline": "the robot is offline",
                "notify_rules_limit": "the dog already has the maximum of 10 notification rules",
                "windows_overlap": "those treat windows overlap",
            }
            parts.append("I couldn't do that because " + reasons.get(refused[0], refused[0].replace("_", " ")) + ".")
        if not parts:
            parts.append("Done." if results else "I don't have that yet.")
        return " ".join(parts)


_brain: Optional[Brain] = None


def get_brain() -> Brain:
    global _brain
    if _brain is None:
        _brain = Brain()
    return _brain
