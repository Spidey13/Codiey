"""Gemini tool declarations for function calling.

Tier 1 — Gemini needs the result, always awaited:
  grep_search, file_search, list_directory, read_file, get_function_info

Tier 2 — side-effects only, fire-and-forget (backend returns {"status":"queued"} instantly,
          frontend sends back to Gemini with SILENT scheduling so Gemini never speaks about it):
  mark_as_discussed, write_to_rules

Every tool carries a required `reasoning` field. The backend never reads it;
it exists solely to force Gemini to reason before calling.
"""

TOOL_DECLARATIONS = [
    {
        "functionDeclarations": [
            # ── Tier 1: Gemini needs the result ─────────────────────────────
            {
                "name": "grep_search",
                "description": "Search for an exact string or regex pattern across all workspace files and return matching file paths, line numbers, and content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence explaining why this tool is being called right now."
                        },
                        "query": {
                            "type": "string",
                            "description": "Text string or regex pattern to search for."
                        },
                        "regex": {
                            "type": "boolean",
                            "description": "Treat query as a regex pattern. Default: false."
                        },
                        "include": {
                            "type": "string",
                            "description": "Comma-separated file extensions to limit the search (e.g. 'py,js'). Default: all files."
                        },
                    },
                    "required": ["reasoning", "query"],
                },
            },
            {
                "name": "file_search",
                "description": "Find files whose names match a glob pattern (e.g. '*.py', '*test*') and return their relative paths.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence explaining why this tool is being called right now."
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern to match file names against (e.g. '*.py', '*config*')."
                        },
                        "dir_path": {
                            "type": "string",
                            "description": "Optional subdirectory to limit the search (relative to project root)."
                        },
                    },
                    "required": ["reasoning", "pattern"],
                },
            },
            {
                "name": "list_directory",
                "description": "List the immediate files and subdirectories at a given path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence explaining why this tool is being called right now."
                        },
                        "dir_path": {
                            "type": "string",
                            "description": "Relative path from project root to list. Default: '.' (project root)."
                        },
                    },
                    "required": ["reasoning"],
                },
            },
            {
                "name": "read_file",
                "description": "Read file contents chunked at AST boundaries; use start_line to continue after the first chunk.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence explaining why this tool is being called right now."
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to the file from project root."
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "Start reading from this line (1-indexed); use the continuation hint from a previous call."
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "Stop reading at this line (1-indexed)."
                        },
                    },
                    "required": ["reasoning", "file_path"],
                },
            },
            {
                "name": "get_function_info",
                "description": "Get a function's signature, location, callers, and callees using AST analysis.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence explaining why this tool is being called right now."
                        },
                        "function_name": {
                            "type": "string",
                            "description": "Exact name of the function or method to look up."
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Optional: narrow the search to this file (relative path). Use when the name is ambiguous."
                        },
                    },
                    "required": ["reasoning", "function_name"],
                },
            },

            # ── Tier 2: Side-effects only, fire-and-forget ───────────────────
            {
                "name": "mark_as_discussed",
                "description": "Mark a file or concept as discussed in this session to track what has been covered.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence explaining why this tool is being called right now."
                        },
                        "path": {
                            "type": "string",
                            "description": "File or directory path that was discussed."
                        },
                        "topic": {
                            "type": "string",
                            "description": "What was discussed about it."
                        },
                    },
                    "required": ["reasoning", "path", "topic"],
                },
            },
            {
                "name": "write_to_rules",
                "description": "Append a verified project insight to the correct section of the persistent rules file for future sessions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence explaining why this tool is being called right now."
                        },
                        "section": {
                            "type": "string",
                            "enum": ["architecture", "conventions", "gotchas", "session_history"],
                            "description": "Section to append to: 'architecture' for how the project is structured, 'conventions' for naming/patterns, 'gotchas' for confusing things with specific explanations, 'session_history' for decisions made in this session."
                        },
                        "insight": {
                            "type": "string",
                            "description": "A single, self-contained factual statement to record."
                        },
                    },
                    "required": ["reasoning", "section", "insight"],
                },
            },
        ]
    }
]
