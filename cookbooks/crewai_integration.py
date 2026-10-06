"""
CrewAI Integration Example

This example shows how to integrate AgentAnywhere Universal Gateway with CrewAI agents.
"""

from crewai import Agent, Task, Crew
import requests

# Gateway configuration
GATEWAY_URL = "http://localhost:8000"
API_KEY = "your-api-key"


class GatewayTool:
    """Tool wrapper for gateway endpoints."""
    
    def __init__(self, gateway_url: str, api_key: str):
        self.gateway_url = gateway_url
        self.api_key = api_key
        self.headers = {"X-API-Key": api_key}
    
    def mcp_query(self, query: str, context: dict = None) -> str:
        """Query MCP backend through gateway."""
        response = requests.post(
            f"{self.gateway_url}/mcp/query",
            headers=self.headers,
            json={"query": query, "context": context or {}}
        )
        response.raise_for_status()
        result = response.json()
        return result.get("response", str(result))
    
    def a2a_message(self, message: str, target_agent: str) -> str:
        """Send A2A message through gateway."""
        response = requests.post(
            f"{self.gateway_url}/a2a/message",
            headers=self.headers,
            json={"message": message, "target_agent": target_agent}
        )
        response.raise_for_status()
        result = response.json()
        return result.get("response", str(result))


# Create gateway tool instance
gateway_tool = GatewayTool(GATEWAY_URL, API_KEY)


# Define CrewAI agents
researcher = Agent(
    role='Researcher',
    goal='Research and gather information using MCP gateway',
    backstory='Expert researcher who uses MCP protocol for data gathering',
    tools=[gateway_tool.mcp_query],
    verbose=True
)

communicator = Agent(
    role='Communicator',
    goal='Communicate with other agents using A2A protocol',
    backstory='Specialized in agent-to-agent communication',
    tools=[gateway_tool.a2a_message],
    verbose=True
)


# Define tasks
research_task = Task(
    description='Research the weather forecast using MCP gateway',
    agent=researcher
)

communication_task = Task(
    description='Send research results to another agent via A2A',
    agent=communicator
)


# Create crew
crew = Crew(
    agents=[researcher, communicator],
    tasks=[research_task, communication_task],
    verbose=True
)


# Example usage
if __name__ == "__main__":
    # Execute crew
    result = crew.kickoff()
    print("\nCrew Result:", result)


