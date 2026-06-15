#!/usr/bin/env python3
import sys
import json

data = json.loads(sys.stdin.read())
tool = data.get("tool_name", "")
tool_input = data.get("tool_input", {})
command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

BLOCKED = ["rm -rf", "DROP TABLE", "DELETE FROM", "os.remove", "shutil.rmtree"]

if tool == "Bash":
    for pattern in BLOCKED:
        if pattern in command:
            print(json.dumps({
                "decision": "block",
                "reason": f"MAAP: Blocked destructive pattern '{pattern}'. Review AGENTS.md before proceeding."
            }))
            sys.exit(0)

print(json.dumps({"decision": "approve"}))
