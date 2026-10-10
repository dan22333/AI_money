import os
import sys

# put the agent package dir on the path so `import store` etc. works
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from langchain_core.messages import AIMessage

import memory
import store


@pytest.fixture(autouse=True)
def clean_state():
    """Fresh in-memory stores for every test."""
    store.reset_memory()
    memory._fallback.clear()
    memory._clients.clear()
    yield
    store.reset_memory()


class FakeChat:
    """Stand-in for the voice model: returns a scripted sequence of AIMessages."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        msg = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return msg


def ai(text="", tool=None, args=None):
    """Build an AIMessage, optionally with one tool call."""
    tool_calls = []
    if tool:
        tool_calls = [{"name": tool, "args": args or {}, "id": "call1", "type": "tool_call"}]
    return AIMessage(content=text, tool_calls=tool_calls)


@pytest.fixture
def fake_chat():
    return FakeChat


@pytest.fixture
def make_ai():
    return ai
