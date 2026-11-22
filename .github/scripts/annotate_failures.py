"""Turn unittest failures in test-output.txt into GitHub annotations.

Annotations can be read without signing in to GitHub, so a failing run can be diagnosed from anywhere.
"""
import re
import sys

text = open(sys.argv[1] if len(sys.argv) > 1 else "test-output.txt", encoding="utf-8", errors="replace").read()
blocks = re.split(r"\n={50,}\n", text)
for block in blocks[1:9]:
    msg = block.strip().replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print("::error title=Test failure::" + msg[:3500])
if len(blocks) < 2:
    print("::error title=Test output tail::" + text[-3000:].replace("%", "%25").replace("\n", "%0A"))
