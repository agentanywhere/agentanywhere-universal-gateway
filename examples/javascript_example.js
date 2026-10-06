/**
 * JavaScript/Node.js Example - AgentAnywhere Universal Gateway
 * 
 * This example demonstrates how to use the gateway from Node.js
 */

const axios = require('axios');

// Configuration
const GATEWAY_URL = 'http://localhost:8000';
const API_KEY = 'your-api-key-here'; // Replace with your actual API key

// Create axios instance with default headers
const gateway = axios.create({
  baseURL: GATEWAY_URL,
  headers: {
    'X-API-Key': API_KEY,
    'Content-Type': 'application/json'
  }
});

/**
 * Example: MCP Request
 */
async function mcpQuery() {
  try {
    const response = await gateway.post('/mcp/query', {
      query: 'What is the weather today?',
      context: { user_id: 'user123' }
    });
    console.log('MCP Response:', response.data);
    return response.data;
  } catch (error) {
    console.error('MCP Error:', error.response?.data || error.message);
    throw error;
  }
}

/**
 * Example: A2A Message
 */
async function a2aSendMessage() {
  try {
    const response = await gateway.post('/a2a/message', {
      message: 'Hello from agent A',
      target_agent: 'agent_b',
      metadata: { priority: 'high' }
    });
    console.log('A2A Response:', response.data);
    return response.data;
  } catch (error) {
    console.error('A2A Error:', error.response?.data || error.message);
    throw error;
  }
}

/**
 * Example: MQTT Publish
 */
async function mqttPublish() {
  try {
    const response = await gateway.post('/mqtt/publish', {
      topic: 'sensors/temperature',
      payload: {
        value: 25.5,
        unit: 'celsius',
        timestamp: new Date().toISOString()
      },
      qos: 1
    });
    console.log('MQTT Published:', response.data);
    return response.data;
  } catch (error) {
    console.error('MQTT Error:', error.response?.data || error.message);
    throw error;
  }
}

/**
 * Example: WebSocket Connection
 */
function websocketExample() {
  const WebSocket = require('ws');
  const ws = new WebSocket(`ws://localhost:8000/ws/endpoint?api_key=${API_KEY}`);
  
  ws.on('open', () => {
    console.log('WebSocket connected');
    ws.send(JSON.stringify({ message: 'Hello from client' }));
  });
  
  ws.on('message', (data) => {
    console.log('Received:', JSON.parse(data.toString()));
  });
  
  ws.on('error', (error) => {
    console.error('WebSocket error:', error);
  });
  
  ws.on('close', () => {
    console.log('WebSocket closed');
  });
}

/**
 * Example: Health Check (no auth required)
 */
async function healthCheck() {
  try {
    const response = await axios.get(`${GATEWAY_URL}/health`);
    console.log('Gateway Status:', response.data.status);
    console.log('Version:', response.data.version);
    return response.data;
  } catch (error) {
    console.error('Health check failed:', error.message);
    throw error;
  }
}

/**
 * Example: Error Handling
 */
async function exampleWithErrorHandling() {
  try {
    await mcpQuery();
  } catch (error) {
    if (error.response) {
      switch (error.response.status) {
        case 401:
          console.error('Authentication failed - check your API key');
          break;
        case 429:
          console.error('Rate limit exceeded - slow down your requests');
          break;
        case 502:
          console.error('Backend service unavailable');
          break;
        default:
          console.error(`HTTP Error: ${error.response.status}`);
      }
    } else {
      console.error('Request failed:', error.message);
    }
  }
}

// Run examples
if (require.main === module) {
  (async () => {
    console.log('AgentAnywhere Universal Gateway - JavaScript Examples\n');
    console.log('='.repeat(50));
    
    // Health check
    await healthCheck();
    
    // Uncomment to test with actual backend:
    // await mcpQuery();
    // await a2aSendMessage();
    // await mqttPublish();
    // websocketExample();
    
    // Error handling example
    await exampleWithErrorHandling();
    
    console.log('\n' + '='.repeat(50));
    console.log('Examples completed!');
  })();
}

module.exports = {
  mcpQuery,
  a2aSendMessage,
  mqttPublish,
  websocketExample,
  healthCheck
};


