import pytest

from app.context import Budget, BudgetExceeded, DeadlineExceeded


def test_budget_llm_limit() -> None:
    budget = Budget()
    for _ in range(6):
        budget.charge_llm()
    with pytest.raises(BudgetExceeded):
        budget.charge_llm()


def test_budget_tool_limit() -> None:
    budget = Budget()
    for _ in range(4):
        budget.charge_tool()
    with pytest.raises(BudgetExceeded):
        budget.charge_tool()


def test_budget_web_limit_is_one() -> None:
    budget = Budget()
    budget.charge_web()
    with pytest.raises(BudgetExceeded):
        budget.charge_web()


def test_budget_deadline() -> None:
    budget = Budget(deadline_seconds=0)
    with pytest.raises(DeadlineExceeded):
        budget.check_deadline()
