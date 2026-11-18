import pytest

from agent.backends import BudgetExceeded, budgeted


def fake_chat(messages, fmt=None, role=None):
    return "ok", {"input_tokens": 60, "output_tokens": 40}


def test_budget_stops_calls_once_spent():
    chat = budgeted(fake_chat, 250)
    for _ in range(3):  # 100, 200, 300: the call that crosses the limit still completes
        assert chat([{"role": "user", "content": "x"}])[0] == "ok"
    with pytest.raises(BudgetExceeded):
        chat([{"role": "user", "content": "x"}])
    assert chat.state["used"] == 300


def test_budgets_nest():
    command = budgeted(fake_chat, 1000)
    run = budgeted(command, 100, "per-run")
    run([{"role": "user", "content": "x"}])
    with pytest.raises(BudgetExceeded, match="per-run"):
        run([{"role": "user", "content": "x"}])
    assert command.state["used"] == 100
