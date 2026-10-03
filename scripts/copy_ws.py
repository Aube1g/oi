#!/usr/bin/env python3
import shutil
src = "/storage/emulated/0/Download/web_search.py"
dst = "xli/tools/web_search.py"
shutil.copy2(src, dst)
print(f"Copied {src} -> {dst}")
