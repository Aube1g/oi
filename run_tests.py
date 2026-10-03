#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "xli"))

def run_tests():
    passed = 0
    failed = 0
    
    try:
        from xli.core.config import get_config
        c = get_config()
        assert c is not None
        print("✅ config: PASSED")
        passed += 1
    except Exception as e:
        print(f"❌ config: FAILED - {e}")
        failed += 1
    
    try:
        from xli.core.agent import XliAgent
        a = XliAgent("TEST", "ag_test", "Test")
        assert a.name == "TEST"
        print("✅ agent: PASSED")
        passed += 1
    except Exception as e:
        print(f"❌ agent: FAILED - {e}")
        failed += 1
    
    try:
        from xli.core.chain import XliCore
        from xli.core.env import EnvironmentAdapter
        core = XliCore(EnvironmentAdapter())
        assert "coder" in core.agents
        print("✅ chain: PASSED")
        passed += 1
    except Exception as e:
        print(f"❌ chain: FAILED - {e}")
        failed += 1
    
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0

if __name__ == "__main__":
    sys.exit(0 if run_tests() else 1)
