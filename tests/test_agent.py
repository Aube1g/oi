def test_agent_init():
    from xli.core.agent import XliAgent
    agent = XliAgent("TEST", "ag_test", "Test agent")
    assert agent.name == "TEST"

def test_subagent():
    from xli.core.agent import XliAgent, SubAgent
    parent = XliAgent("PARENT", "ag_parent", "Parent")
    child = SubAgent("CHILD", parent, "Spec", "ag_child")
    assert child.parent is parent
