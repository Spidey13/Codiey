import json
import sys

def parse_log(filepath):
    out = open("parsed_log2.txt", "w", encoding="utf-8")
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            try:
                val = json.loads(line)
            except Exception:
                continue
                
            cat = val.get("category")
            if cat in ["USER_SPEECH", "AI_SPEECH", "TOOL_CALL", "TOOL_RESULT", "INTERRUPT", "ERROR", "WS_CLOSE", "GOAWAY", "RECONNECT", "SETUP"]:
                dt = val.get("detail", "")
                if len(dt) > 100:
                    dt = dt[:100] + "..."
                out.write(f"{val.get('elapsedMs', 0)/1000:.1f}s | {cat:12s} | {dt}\n")

if __name__ == "__main__":
    parse_log(sys.argv[1])
