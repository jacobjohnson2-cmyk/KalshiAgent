from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from kalshi_agent.agent import Agent


def _msg(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        msg = self.responses.pop(0)
        if isinstance(msg, BaseException):
            raise msg
        yield SimpleNamespace(
            text_stream=iter([b.text for b in msg.content if b.type == "text"]),
            get_final_message=lambda: msg,
        )


class FakeToolbox:
    def __init__(self):
        self.calls = []

    def run(self, name, args):
        self.calls.append((name, args))
        return '{"ok": true}', False


def make(responses):
    client, tb, out = FakeClient(responses), FakeToolbox(), []
    agent = Agent(client, tb, model="claude-opus-5", effort="high", on_text=out.append,
                  on_tool_call=lambda n, a: None, on_notice=out.append)
    return agent, client, tb, out


def test_tool_loop_runs_tools_then_finishes():
    agent, client, tb, out = make([
        _msg("tool_use", _text("Checking."), _tool("t1", "get_portfolio_summary", {}),
             _tool("t2", "get_open_orders", {})),
        _msg("end_turn", _text("All good.")),
    ])
    agent.send("how am I doing?")
    assert [c[0] for c in tb.calls] == ["get_portfolio_summary", "get_open_orders"]
    assert out == ["Checking.", "All good."]
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    results = agent.messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # one message, all results
    req = client.requests[0]
    assert req["fallbacks"] == "default" and req["thinking"] == {"type": "adaptive"}


def test_max_tokens_does_not_execute_truncated_tool_call():
    agent, _, tb, out = make([_msg("max_tokens", _text("Sell"),
                                   _tool("t1", "place_order", {"ticker": "K"}))])
    agent.send("sell everything")
    assert tb.calls == []
    assert all(b.type != "tool_use" for b in agent.messages[-1]["content"])
    assert "output limit" in out[-1]


def test_refusal_stops_without_running_tools():
    agent, _, tb, out = make([_msg("refusal", _tool("t1", "place_order", {}))])
    agent.send("x")
    assert tb.calls == [] and "declined" in out[-1]
    assert agent.messages[-1]["role"] == "assistant"


def test_exception_rolls_back_partial_turn():
    agent, _, _, _ = make([
        _msg("end_turn", _text("hi")),
        _msg("tool_use", _tool("t1", "get_open_orders", {})),
        KeyboardInterrupt(),
    ])
    agent.send("first")
    with pytest.raises(KeyboardInterrupt):
        agent.send("second")
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]
