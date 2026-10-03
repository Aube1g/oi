from xli.core.goal import Goal, Criterion, GoalStatus


def test_goal_evaluates_criteria():
    state = {"done": False}
    goal = Goal(description="test", criteria=[Criterion("thing works", check=lambda: state["done"])])
    assert goal.evaluate() is False
    state["done"] = True
    assert goal.evaluate() is True


def test_goal_should_continue_respects_max_iterations():
    goal = Goal(description="test", criteria=[Criterion("x", check=lambda: False)], max_iterations=2)
    goal.status = GoalStatus.IN_PROGRESS
    assert goal.should_continue() is True
    goal.record_attempt(layer_id="a", note="n")
    assert goal.should_continue() is True
    goal.record_attempt(layer_id="b", note="n")
    assert goal.should_continue() is False


def test_goal_criterion_exception_counts_as_not_met():
    def _boom():
        raise ValueError("nope")
    goal = Goal(description="test", criteria=[Criterion("x", check=_boom)])
    assert goal.evaluate() is False


def test_goal_summary_format():
    goal = Goal(description="ship it", criteria=[Criterion("tests pass", check=lambda: True)])
    goal.evaluate()
    text = goal.summary()
    assert "ship it" in text
    assert "tests pass" in text
