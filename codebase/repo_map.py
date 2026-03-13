"""Tree Traversal Retrieval — Ranked Symbol Map of Workspace.

Builds a directed graph of definitions and references across the workspace.
Uses PageRank with personalization to rank files and symbols by importance.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, namedtuple
from pathlib import Path

import networkx as nx

from codiey.codebase.parser import EXTENSION_TO_LANGUAGE, get_parser
from codiey.codebase.workspace import walk_source_files

logger = logging.getLogger("codiey.repo_map")

# kind: "def" (definition) or "ref" (reference)
Tag = namedtuple("Tag", ["rel_path", "abs_path", "name", "kind", "line"])


class RepoMap:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.cache_path = workspace / ".codiey" / "repo_map_cache.json"
        
        self.graph = nx.MultiDiGraph()
        
        # In-memory mapping
        self.tags_cache: dict[str, dict] = {}  # rel_path -> {"mtime": float, "tags": list[dict]}
        
        self.defines: dict[str, set[str]] = defaultdict(set) # symbol -> set[rel_path]
        self.references: dict[str, list[str]] = defaultdict(list) # symbol -> list[rel_path]
        
        # Results
        self.ranked_files: list[tuple[str, float]] = []

    def load_cache(self) -> bool:
        """Load from disk cache. Return True if cache is valid and some data was loaded."""
        if not self.cache_path.exists():
            return False
        
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                return False
                
            self.tags_cache = data.get("files", {})
            return bool(self.tags_cache)
        except Exception as e:
            logger.warning(f"Failed to load repo_map cache: {e}")
            return False

    def save_cache(self):
        """Save the parsed tags to disk."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "version": 1,
                "files": self.tags_cache
            }
            self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to save repo_map cache: {e}")

    def build(self):
        """Build or incrementally update the graph and rankings."""
        changed = False
        
        # 1. Extract Tags for all files
        for abs_path in walk_source_files(self.workspace):
            try:
                rel_path = str(abs_path.relative_to(self.workspace)).replace("\\", "/")
            except ValueError:
                continue
                
            try:
                mtime = abs_path.stat().st_mtime
            except OSError:
                continue

            cached = self.tags_cache.get(rel_path)
            if cached and cached.get("mtime") == mtime:
                continue  # Up to date
                
            # Parse and cache
            tags = extract_tags(abs_path, rel_path)
            self.tags_cache[rel_path] = {
                "mtime": mtime,
                "tags": [t._asdict() for t in tags]
            }
            changed = True
            
        # Clean up deleted files from cache
        current_files = {str(p.relative_to(self.workspace)).replace("\\", "/") for p in walk_source_files(self.workspace)}
        for rel_path in list(self.tags_cache.keys()):
            if rel_path not in current_files:
                del self.tags_cache[rel_path]
                changed = True

        if changed:
            self.save_cache()
            
        # 2. Build the indices
        self.defines.clear()
        self.references.clear()
        
        for rel_path, data in self.tags_cache.items():
            for t_dict in data["tags"]:
                if t_dict["kind"] == "def":
                    self.defines[t_dict["name"]].add(rel_path)
                elif t_dict["kind"] == "ref":
                    self.references[t_dict["name"]].append(rel_path)
                    
        # 3. Build the NetworkX Graph
        self.graph.clear()
        
        # Add all files as nodes
        for rel_path in self.tags_cache.keys():
            self.graph.add_node(rel_path)
            
        import math
            
        for symbol, ref_files in self.references.items():
            def_files = self.defines.get(symbol, set())
            
            # Skip built-ins, or overly generic symbols defined everywhere
            if not def_files or len(def_files) > 5:
                continue
                
            # Calculate weight multiplier based on symbol quality
            weight_mult = 1.0
            if len(symbol) >= 8 or symbol != symbol.lower(): # heuristic for "meaningful name"
                weight_mult *= 10.0
            if symbol.startswith("_"):
                weight_mult *= 0.1
                
            ref_count = len(ref_files)
            if ref_count == 0:
                continue
                
            # Diminishing returns on many references to the same file
            weight = weight_mult * (1.0 / math.sqrt(ref_count))
            
            for referencer in ref_files:
                for definer in def_files:
                    if referencer != definer:
                        self.graph.add_edge(referencer, definer, weight=weight, symbol=symbol)
                        
        # 4. Rank with PageRank
        self._rank_files()

    def _rank_files(self, personalization: dict[str, float] = None):
        """Compute pagerank on the graph."""
        if not self.graph.nodes:
            self.ranked_files = []
            return
            
        try:
            # Handle empty personalization logic within nx.pagerank by passing None
            pr = nx.pagerank(self.graph, weight="weight", personalization=personalization)
            self.ranked_files = sorted(pr.items(), key=lambda x: x[1], reverse=True)
        except ZeroDivisionError:
            # Fallback if graph is completely disconnected
            self.ranked_files = [(n, 1.0) for n in self.graph.nodes]

    def update_personalization(self, active_files: set[str]):
        """Re-rank files given a set of highly relevant active files."""
        if not active_files or not self.graph.nodes:
            return
            
        # Distribute extra weight to active files
        pers = {n: 0.1 for n in self.graph.nodes}
        boost = 100.0 / len(active_files)
        
        valid_boost = False
        for f in active_files:
            if f in pers:
                pers[f] += boost
                valid_boost = True
                
        if valid_boost:
            self._rank_files(personalization=pers)

    def get_file_summary(self, rel_path: str) -> list[Tag]:
        """Return the most important definitions in a file, ranked by incoming references."""
        data = self.tags_cache.get(rel_path)
        if not data:
            return []
            
        # Only grab definitions
        defs = [Tag(**t) for t in data["tags"] if t["kind"] == "def"]
        
        # Sort them by how many files reference them 
        # (local PageRank approximation for definitions)
        def _score(tag: Tag) -> int:
            return sum(1 for f in self.references.get(tag.name, []) if f != rel_path)
            
        return sorted(defs, key=_score, reverse=True)
        

def extract_tags(abs_path: Path, rel_path: str) -> list[Tag]:
    """Parse definitions and references using tree-sitter."""
    ext = abs_path.suffix.lower()
    if ext not in EXTENSION_TO_LANGUAGE:
        return []
        
    try:
        source = abs_path.read_bytes()
    except OSError:
        return []
        
    language = EXTENSION_TO_LANGUAGE[ext]
    parser = get_parser(language)
    tree = parser.parse(source)
    
    tags = []
    
    def _walk(node):
        line = node.start_point[0] + 1
        
        # Definitions (Python)
        if node.type in ("function_definition", "class_definition"):
            name = _get_child_text(node, "identifier")
            if name: tags.append(Tag(rel_path, str(abs_path), name, "def", line))
            
        # Definitions (JS/TS)
        elif node.type in ("function_declaration", "class_declaration", "method_definition"):
            name = _get_child_text(node, "identifier")
            if not name and node.type == "method_definition":
                 name = _get_child_text(node, "property_identifier")
            if name: tags.append(Tag(rel_path, str(abs_path), name, "def", line))
            
        elif node.type == "variable_declarator":
            name = _get_child_text(node, "identifier")
            # Only top-level variable declarators logic is hard to enforce purely by node type,
            # but as a heuristic this is okay.
            if name: tags.append(Tag(rel_path, str(abs_path), name, "def", line))
            
        # References
        elif node.type in ("call", "call_expression"):
            name = _get_call_name(node, is_python=(ext == ".py"))
            if name: tags.append(Tag(rel_path, str(abs_path), name, "ref", line))
            
        elif node.type in ("import_from_statement", "import_statement", "import_declaration"):
            # A bit noisy, but we can capture module imports as references
            for child in node.children:
                if child.type in ("dotted_name", "identifier", "import_specifier"):
                     name = child.text.decode("utf-8")
                     if name: tags.append(Tag(rel_path, str(abs_path), name, "ref", line))

        for child in node.children:
            _walk(child)
            
    _walk(tree.root_node)
    return tags

def _get_child_text(node, child_type: str) -> str:
    for child in node.children:
        if child.type == child_type:
            return child.text.decode("utf-8")
    return ""

def _get_call_name(call_node, is_python: bool) -> str:
    for child in call_node.children:
        if child.type == "identifier":
            return child.text.decode("utf-8")
        if child.type in ("attribute", "member_expression"):
            for inner in reversed(child.children):
                if inner.type in ("identifier", "field_identifier", "property_identifier"):
                    return inner.text.decode("utf-8")
    return ""
