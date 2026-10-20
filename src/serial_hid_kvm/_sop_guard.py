"""Standalone Antigravity hook: exact-file read allowlist, everything else denied.

Copied into an analysis workspace. Uses only stdlib, and never trusts model input
to select a command, writable location, or additional permission.
"""
import json
import os
from pathlib import Path
import sys

# Read-only file tools and their path argument (agy models differ: older
# ones call view_file, newer ones read_file).
READ_TOOLS = {"view_file": "AbsolutePath", "read_file": "Path"}


def read_path(call):
    """The path a read-only file tool asks for, or None for any other tool."""
    args = call.get("args", {})
    key = READ_TOOLS.get(call.get("name"))
    if key is None or not isinstance(args, dict):
        return None
    path = args.get(key, args.get("AbsolutePath"))
    return path if isinstance(path, str) else None


def decide(payload, allowed):
    call = payload.get("toolCall", {})
    # Structured-output completion carries a result object, without filesystem,
    # network, target, or process side effects.
    if call.get("name") == "finish":
        return "allow"
    path = read_path(call)
    if path is not None:
        try:
            canonical = os.path.normcase(str(Path(path).resolve()))
            if canonical in {os.path.normcase(p) for p in allowed} and Path(path).is_file():
                return "auto_approve"
        except (OSError, ValueError):
            pass
    return "deny"


def main():
    root = Path(__file__).resolve().parent
    mode = sys.argv[1] if len(sys.argv) == 2 else "invalid"
    try:
        payload = json.load(sys.stdin)
        allowed = set(json.loads((root / "allowed.json").read_text(encoding="utf-8")))
        call = payload.get("toolCall", {})
        decision = decide(payload, allowed)
        record = {"mode": mode, "tool": call.get("name"),
                  "path": read_path(call),
                  "decision": decision, "error": payload.get("error"),
                  "model": payload.get("modelName")}
        with (root / "audit.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=True)+"\n")
        if mode == "pre":
            print(json.dumps({"decision": decision, "reason": "SOP analysis permits only the exact media/evidence file reads listed for this job."}))
        elif mode in ("post", "start"):
            print("{}")
        else:
            print('{"decision":"deny"}')
    except Exception:
        print('{"decision":"deny","reason":"SOP read guard could not validate this tool call."}')


if __name__ == "__main__":
    main()
