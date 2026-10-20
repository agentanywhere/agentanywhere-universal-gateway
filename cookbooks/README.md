# Cookbooks

Sketches of agent frameworks calling services through AgentAnywhere Universal
Gateway. Each one sends requests to the gateway with an API key and uses the
paths of the sample backends (`/mcp/query`, `/a2a/message`), so start the
gateway and the MCP and A2A samples first
([sample backends](../samples/backends/README.md)).

These scripts were not run for the current documentation, and the framework
packages are not in `requirements.txt`. Treat them as starting points.

| File | Framework | Install |
|---|---|---|
| `langgraph_integration.py` | LangGraph: graph nodes that call MCP and A2A through the gateway | `pip install langgraph` |
| `crewai_integration.py` | CrewAI: a tool class that calls MCP and A2A through the gateway | `pip install crewai` |

Set the key in the script (`API_KEY = "your-api-key"`) before you run it:

```bash
python cookbooks/langgraph_integration.py
python cookbooks/crewai_integration.py
```

## Adding a cookbook

1. Add a Python file here with a docstring that says what it shows and which
   packages it needs.
2. Read the gateway URL and key from variables at the top of the file.
3. Add a row to the table above.

See [CONTRIBUTING.md](../CONTRIBUTING.md).
