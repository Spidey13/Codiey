import json
from pathlib import Path
from codiey.codebase.parser import parse_file

_SCRATCH = Path(__file__).resolve().parent / "_scratch"
_SCRATCH.mkdir(exist_ok=True)
_ts = _SCRATCH / "test2.ts"
_ts.write_text("export const agentFactory = async () => {};\n", encoding="utf-8")
res = parse_file(_ts)
(_SCRATCH / "out2.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
