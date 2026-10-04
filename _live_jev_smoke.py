import importlib
import json
import os
import sys
import tempfile

sys.path.insert(0, "src")

tmp_home = tempfile.mkdtemp(prefix="huddle-jev-live-")
os.environ["MCP_HUDDLE_HOME"] = tmp_home

from mcp_huddle import bus
importlib.reload(bus)
from mcp_huddle import jev_judge

module = jev_judge._load_jev_module()
print("jev module found:", module is not None)
if module is not None:
    print("jev module file:", getattr(module, "__file__", "?"))

request = {
    "category": "huddle_result_judge",
    "correlation_id": "huddle-live-smoke-1",
    "result_link": None,
    "candidate_ids": ["result"],
    "metadata": {"task_kind": "huddle_result", "harness": "cli", "candidate_count": 1},
    "fixtures": [
        {"id": "result", "kind": "agent",
         "description": "Task: reverse string | Criteria: 'abc'->'cba', has test | Result: def reverse(s): return s[::-1] | Evidence: pytest passed",
         "tags": []},
    ],
    "questions": {
        "verdict": {
            "type": "choice",
            "instructions": "Judge the worker result against the task and acceptance criteria using only the given evidence.",
            "criteria": {
                "ready": "Meets acceptance criteria; evidence supports it.",
                "revise": "Concrete defect found; same worker should fix it.",
                "insufficient": "Not enough evidence to judge either way.",
            },
        },
    },
    "max_cost_usd": "0.02",
}

client = module.JevClient()
outcome = client.decide(request)
print("outcome:")
print(json.dumps(outcome, indent=2, default=str))
