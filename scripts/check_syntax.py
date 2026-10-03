#!/usr/bin/env python3
import ast
import sys
from pathlib import Path

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("xli/cli.py")
try:
    ast.parse(target.read_text(encoding="utf-8"))
    print(f"✅ {target} — syntax OK")
except SyntaxError as e:
    print(f"❌ {target} — SyntaxError: {e}")
    sys.exit(1)
