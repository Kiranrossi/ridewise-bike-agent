"""Check that the NeoStats LLM endpoint is reachable and supports tool calling.

Usage: python scripts/smoke_test_llm.py
"""
import json
import os
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
    timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
)
model = os.environ["LLM_MODEL"]

# 1. Plain chat
t0 = time.time()
resp = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Reply with exactly: pong"}],
)
print(f"[chat] {time.time() - t0:.1f}s -> {resp.choices[0].message.content!r}")

# 2. Tool calling
tools = [{
    "type": "function",
    "function": {
        "name": "get_booking",
        "description": "Look up a RideWise booking by its ID.",
        "parameters": {
            "type": "object",
            "properties": {"booking_id": {"type": "string", "description": "Format B-0001"}},
            "required": ["booking_id"],
        },
    },
}]
t0 = time.time()
resp = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "What is the status of booking B-0042?"}],
    tools=tools,
)
msg = resp.choices[0].message
if msg.tool_calls:
    call = msg.tool_calls[0]
    print(f"[tools] {time.time() - t0:.1f}s -> {call.function.name}({json.loads(call.function.arguments)})")
    print("OK: tool calling supported")
else:
    print(f"[tools] no tool call returned; content={msg.content!r}")
    sys.exit(1)
