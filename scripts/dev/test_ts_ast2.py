from pathlib import Path

import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Parser

_SCRATCH = Path(__file__).resolve().parent / "_scratch"
_SCRATCH.mkdir(exist_ok=True)


def dump(node, source, indent=0):
    lines.append(" " * indent + f"{node.type} '{node.text.decode('utf8')}'")
    for child in node.children:
        dump(child, source, indent + 2)


code = b"export const agentFactory: AgentFactory = () => { return 1; }"
parser = Parser(Language(ts_typescript.language_typescript()))
tree = parser.parse(code)
lines = []
dump(tree.root_node, code)
(_SCRATCH / "ast_out2.txt").write_text("\n".join(lines), encoding="utf-8")
