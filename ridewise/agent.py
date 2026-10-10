"""Tool-calling agent loop around the NeoStats-hosted LLM.

Per turn: system prompt (role rules + account snapshot) + recent history + user message →
the model calls role-scoped tools (executed in Python) → final answer. The model's hidden
reasoning is never shown. If the LLM is unavailable, the deterministic fallback answers.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

import openai
from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ridewise import fallback, sources
from ridewise.config import settings
from ridewise.db import record_queries
from ridewise.prompts import build_system_prompt
from ridewise.tools.base import Session
from ridewise.timeutil import IST
from ridewise.tools.registry import execute, tools_for
from ridewise.verifier import verify

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
HISTORY_MESSAGES = 16            # last 8 user/assistant turns
MAX_TOOL_RESULT_CHARS = 12_000
MEMORY_CHARS = 6_000
TURN_DEADLINE_S = 120
MAX_USER_CHARS = 2_000
RETRYABLE = (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError,
             openai.InternalServerError)
BOOKING_RE = re.compile(r"\bB-\d{4}\b")


@dataclass
class Reply:
    answer: str
    sources: list[dict] = field(default_factory=list)     # typed citations (see sources.py)
    trace: list[dict] = field(default_factory=list)
    degraded: bool = False
    seconds: float = 0.0


class Agent:
    def __init__(self, session: Session, client: OpenAI | None = None):
        self.session = session
        if client is None and settings.llm_api_key and settings.llm_base_url:
            client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                            timeout=settings.llm_timeout, max_retries=0)
        self.client = client                         # None → deterministic fallback only
        self.tools = tools_for(session.role)
        self.recent_bookings: list[str] = []        # conversational memory of booking IDs
        self.last_tool_results: str = ""            # previous turn's tool outputs, for follow-ups

    @retry(retry=retry_if_exception_type(RETRYABLE), stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
    def _complete(self, messages: list[dict], allow_tools: bool = True):
        kwargs = {"tools": self.tools, "tool_choice": "auto"} if allow_tools else {}
        return self.client.chat.completions.create(
            model=settings.llm_model, messages=messages, temperature=settings.llm_temperature,
            reasoning_effort=settings.llm_reasoning_effort, **kwargs)

    def _remember(self, *texts: str) -> None:
        for t in texts:
            for bid in BOOKING_RE.findall(t or ""):
                if bid in self.recent_bookings:
                    self.recent_bookings.remove(bid)
                self.recent_bookings.append(bid)
        self.recent_bookings = self.recent_bookings[-5:]

    def _blocked(self, trace: list[dict], t0: float) -> Reply:
        """The platform's content filter blocked the request (e.g. a jailbreak attempt)."""
        trace = trace + [{"tool": "content_filter", "args": {}, "ok": False,
                          "result": {"ok": False, "error": "Blocked by the LLM platform's safety filter."}}]
        return Reply("I can't help with that request. I'm here to help with RideWise rentals, bookings, payments, "
                     "charges and policies — what would you like to know?", [], trace,
                     seconds=round(time.monotonic() - t0, 1))

    def respond(self, history: list[dict], user_message: str) -> Reply:
        """history: prior turns as [{'role': 'user'|'assistant', 'content': str}, ...]."""
        t0 = time.monotonic()
        user_message = (user_message or "").strip()[:MAX_USER_CHARS]
        if not user_message:
            return Reply("Please type a question.")
        trace: list[dict] = []
        if self.client is None:
            return self._fallback(user_message, trace, t0)
        try:
            with record_queries() as snapshot_queries:
                system_prompt = build_system_prompt(self.session, self.recent_bookings)
            snapshot_ids = _snapshot_booking_ids(system_prompt)
            messages = [{"role": "system", "content": system_prompt}]
            messages += [{"role": m["role"], "content": m["content"]} for m in history[-HISTORY_MESSAGES:]
                         if m.get("role") in ("user", "assistant") and m.get("content")]
            if history and self.last_tool_results:
                messages.append({"role": "system", "content":
                                 "Tool results from the previous turn (use them for follow-up questions; call tools "
                                 "again if you need different or fresher data):\n" + self.last_tool_results})
            messages.append({"role": "user", "content": user_message})

            answer = None
            for _ in range(MAX_TOOL_ROUNDS):
                if time.monotonic() - t0 > TURN_DEADLINE_S:
                    raise TimeoutError("turn deadline exceeded")
                choice = self._complete(messages).choices[0]
                if choice.finish_reason == "content_filter":
                    return self._blocked(trace, t0)
                msg = choice.message
                if not msg.tool_calls:
                    answer = msg.content
                    break
                messages.append({"role": "assistant", "content": msg.content or "",
                                 "tool_calls": [{"id": tc.id, "type": "function",
                                                 "function": {"name": tc.function.name,
                                                              "arguments": tc.function.arguments}}
                                                for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    s = time.monotonic()
                    result = execute(self.session, tc.function.name, tc.function.arguments)
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {"raw": tc.function.arguments}
                    trace.append({"tool": tc.function.name, "args": args, "ok": result.get("ok"),
                                  "ms": int((time.monotonic() - s) * 1000), "result": result})
                    self._remember(json.dumps(args))
                    model_view = {k: v for k, v in result.items() if k != "queries"}   # SQL is for the UI only
                    content = json.dumps(model_view, ensure_ascii=False, default=str)
                    if len(content) > MAX_TOOL_RESULT_CHARS:
                        content = content[:MAX_TOOL_RESULT_CHARS] + '…(truncated; narrow the request)"}'
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
            if answer is None:
                # Tool budget exhausted: force a final answer from what has been gathered.
                messages.append({"role": "system", "content": "Tool limit reached. Answer now using only the "
                                                              "information gathered; say what is missing."})
                answer = self._complete(messages, allow_tools=False).choices[0].message.content
            answer = _clean(answer) or "Sorry, I couldn't produce an answer. Please rephrase your question."
            answer = self._verify_and_repair(answer, messages, trace, history, user_message, system_prompt)
            self._remember(user_message, answer)
            if trace:
                self.last_tool_results = json.dumps(
                    [{"tool": t["tool"], "args": t["args"],
                      "result": {k: v for k, v in t["result"].items() if k != "queries"}} for t in trace],
                    ensure_ascii=False, default=str)[:MEMORY_CHARS]
            # Booking facts quoted from the pre-fetched account snapshot (no tool call) are cited too,
            # with the snapshot's SQL; answers that used no data (refusals, greetings) cite nothing.
            from_tools = {s.split()[-1] for s in _raw_sources(trace) if s.startswith("booking ")}
            from_snapshot = sorted((set(BOOKING_RE.findall(answer)) & snapshot_ids) - from_tools)
            if from_snapshot:
                trace.append({"tool": "account_snapshot", "args": {}, "ok": True, "ms": 0,
                              "result": {"ok": True, "sources": [f"booking {b}" for b in from_snapshot],
                                         "queries": snapshot_queries}})
            return Reply(answer, self._cite(_raw_sources(trace)), trace, seconds=round(time.monotonic() - t0, 1))
        except openai.BadRequestError as e:
            if "content_filter" in str(e):
                return self._blocked(trace, t0)
            log.warning("LLM rejected the request, using fallback: %s", str(e)[:200])
            return self._fallback(user_message, trace, t0)
        except (*RETRYABLE, openai.APIStatusError, TimeoutError) as e:
            log.warning("LLM unavailable, using fallback: %s", type(e).__name__)
            return self._fallback(user_message, trace, t0)

    def _verify_and_repair(self, answer: str, messages: list[dict], trace: list[dict], history: list[dict],
                           user_message: str, system_prompt: str) -> str:
        """Check every date and ₹ amount against this turn's evidence; on failure, ask the model once to
        rewrite using only grounded values. The check is recorded in the trace for transparency."""
        evidence = "\n".join([system_prompt, self.last_tool_results, user_message,
                              *[m["content"] for m in history if m.get("role") == "user"],
                              *[json.dumps({k: v for k, v in t["result"].items() if k != "queries"},
                                           ensure_ascii=False, default=str) for t in trace]])
        today = self.session.now().astimezone(IST).date()
        first = verify(answer, evidence, today)
        if not first.dates_checked and not first.amounts_checked:
            return answer
        step = {"tool": "verify_answer", "args": {}, "ok": first.ok, "ms": 0,
                "result": {"ok": first.ok, "dates_checked": first.dates_checked,
                           "amounts_checked": first.amounts_checked, "issues_found": first.issues}}
        if not first.ok:
            repair = messages + [
                {"role": "assistant", "content": answer},
                {"role": "system", "content": "Verification failed. These values in your answer do not match the "
                 "tool results or records: " + "; ".join(first.issues) + ". Rewrite the answer copying dates and "
                 "amounts exactly as they appear in the tool results. Remove any figure you computed yourself."}]
            try:
                fixed = _clean(self._complete(repair, allow_tools=False).choices[0].message.content)
            except Exception:                      # repair is best-effort; never lose the answer
                fixed = ""
            second = verify(fixed, evidence, today) if fixed else first
            if fixed:
                answer = fixed
            step["result"]["after_repair"] = second.issues
            step["ok"] = step["result"]["ok"] = second.ok
            if not second.ok:
                answer += ("\n\n_Note: some figures above could not be verified against RideWise records — "
                           "please check the sources below or contact support._")
        trace.append(step)
        return answer

    def _cite(self, raw: list[str]) -> list[dict]:
        return sources.normalise(raw, self.session.customer_id)

    def _fallback(self, user_message: str, trace: list[dict], t0: float) -> Reply:
        text, raw, fb_trace = fallback.respond(self.session, user_message)
        return Reply(text, self._cite(raw), trace + fb_trace, degraded=True, seconds=round(time.monotonic() - t0, 1))


_NATIVE_CITATION_RE = re.compile(r"【[^】}\n]*[】}]?")


def _clean(text: str | None) -> str:
    """Strip model-native citation markers and normalise typographic characters."""
    text = _NATIVE_CITATION_RE.sub("", text or "")
    for ch, rep in (("‑", "-"), ("‐", "-"), (" ", " "), (" ", " ")):
        text = text.replace(ch, rep)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def _raw_sources(trace: list[dict]) -> list[str]:
    seen: list[str] = []
    for step in trace:
        for s in (step["result"].get("sources") or []) if step.get("ok") else []:
            if s not in seen:
                seen.append(s)
    return seen


def _snapshot_booking_ids(system_prompt: str) -> set[str]:
    """Booking IDs listed in the account snapshot section of the system prompt."""
    if "## Account snapshot" not in system_prompt:
        return set()
    section = system_prompt.split("## Account snapshot", 1)[1].split("Bookings discussed earlier", 1)[0]
    return set(BOOKING_RE.findall(section))
