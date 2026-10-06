"""
LangGraph Integration Example

This example shows how to integrate AgentAnywhere Universal Gateway with LangGraph agents.
"""

from langgraph.graph import StateGraph, END
from typing import TypedDict
import requests

# Gateway configuration
GATEWAY_URL = "http://localhost:8000"
API_KEY = "your-api-key"


class AgentState(TypedDict):
    """State for LangGraph agent."""
    messages: list
    user_id: str
    context: dict


def call_mcp_gateway(state: AgentState) -> AgentState:
    """Call MCP backend through gateway."""
    headers = {"X-API-Key": API_KEY}
    
    # Get last user message
    last_message = state["messages"][-1] if state["messages"] else ""
    
    # Call MCP endpoint
    response = requests.post(
        f"{GATEWAY_URL}/mcp/query",
        headers=headers,
        json={
            "query": last_message,
            "context": state.get("context", {})
        }
    )
    
    result = response.json()
    
    # Add response to messages
    state["messages"].append({
        "role": "assistant",
        "content": result.get("response", "")
    })
    
    return state


def call_a2a_gateway(state: AgentState) -> AgentState:
    """Call A2A backend through gateway for agent communication."""
    headers = {"X-API-Key": API_KEY}
    
    # Send message to another agent
    response = requests.post(
        f"{GATEWAY_URL}/a2a/message",
        headers=headers,
        json={
            "message": state["messages"][-1],
            "target_agent": "agent_b",
            "metadata": {"user_id": state["user_id"]}
        }
    )
    
    result = response.json()
    
    # Add response to messages
    state["messages"].append({
        "role": "assistant",
        "content": result.get("response", "")
    })
    
    return state


# Build LangGraph workflow
def create_agent_graph():
    """Create LangGraph agent with gateway integration."""
    workflow = StateGraph(AgentState)
    
    # Add nodes
    workflow.add_node("mcp_query", call_mcp_gateway)
    workflow.add_node("a2a_communicate", call_a2a_gateway)
    
    # Define edges
    workflow.set_entry_point("mcp_query")
    workflow.add_edge("mcp_query", "a2a_communicate")
    workflow.add_edge("a2a_communicate", END)
    
    return workflow.compile()


# Example usage
if __name__ == "__main__":
    # Create agent
    agent = create_agent_graph()
    
    # Run agent
    initial_state = {
        "messages": [{"role": "user", "content": "What is the weather?"}],
        "user_id": "user123",
        "context": {}
    }
    
    result = agent.invoke(initial_state)
    print("Agent Response:", result["messages"][-1]["content"])


