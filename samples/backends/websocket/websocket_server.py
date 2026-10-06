"""
Sample WebSocket Backend Server

This is a simple WebSocket server that demonstrates real-time bidirectional communication.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
import structlog
import json
from typing import Dict, Set
from datetime import datetime, timezone

logger = structlog.get_logger()
app = FastAPI(title="Sample WebSocket Backend", version="1.0.0")


# Connection manager
class ConnectionManager:
    """Manages WebSocket connections."""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.connection_metadata: Dict[WebSocket, Dict] = {}
    
    async def connect(self, websocket: WebSocket, client_id: str = None):
        """Accept a WebSocket connection."""
        await websocket.accept()
        self.active_connections.add(websocket)
        self.connection_metadata[websocket] = {
            "client_id": client_id or f"client_{len(self.active_connections)}",
            "connected_at": datetime.now(timezone.utc).isoformat()
        }
        logger.info("websocket_connected", client_id=self.connection_metadata[websocket]["client_id"])
    
    def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection."""
        if websocket in self.active_connections:
            client_id = self.connection_metadata.get(websocket, {}).get("client_id", "unknown")
            self.active_connections.remove(websocket)
            if websocket in self.connection_metadata:
                del self.connection_metadata[websocket]
            logger.info("websocket_disconnected", client_id=client_id)
    
    async def send_personal_message(self, message: str, websocket: WebSocket):
        """Send a message to a specific connection."""
        try:
            await websocket.send_text(message)
        except Exception as e:
            logger.error("websocket_send_error", error=str(e))
            self.disconnect(websocket)
    
    async def broadcast(self, message: str):
        """Broadcast a message to all connections."""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.error("websocket_broadcast_error", error=str(e))
                disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "websocket-backend",
        "active_connections": len(manager.active_connections)
    }


@app.get("/")
async def get():
    """Simple HTML page for testing WebSocket."""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>WebSocket Test</title>
    </head>
    <body>
        <h1>WebSocket Test Client</h1>
        <div id="messages"></div>
        <input type="text" id="messageInput" placeholder="Type a message...">
        <button onclick="sendMessage()">Send</button>
        <script>
            const ws = new WebSocket('ws://localhost:8080/ws');
            const messagesDiv = document.getElementById('messages');
            
            ws.onmessage = (event) => {
                const p = document.createElement('p');
                p.textContent = 'Received: ' + event.data;
                messagesDiv.appendChild(p);
            };
            
            ws.onopen = () => {
                const p = document.createElement('p');
                p.textContent = 'Connected to WebSocket server';
                messagesDiv.appendChild(p);
            };
            
            function sendMessage() {
                const input = document.getElementById('messageInput');
                ws.send(JSON.stringify({message: input.value, timestamp: new Date().toISOString()}));
                input.value = '';
            }
        </script>
    </body>
    </html>
    """)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, client_id: str = None):
    """WebSocket endpoint."""
    await manager.connect(websocket, client_id)
    
    try:
        # Send welcome message
        welcome_msg = {
            "type": "welcome",
            "message": "Connected to WebSocket server",
            "client_id": manager.connection_metadata[websocket]["client_id"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        await manager.send_personal_message(json.dumps(welcome_msg), websocket)
        
        # Broadcast connection to others
        broadcast_msg = {
            "type": "client_connected",
            "client_id": manager.connection_metadata[websocket]["client_id"],
            "total_connections": len(manager.active_connections),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        await manager.broadcast(json.dumps(broadcast_msg))
        
        # Listen for messages
        while True:
            data = await websocket.receive_text()
            
            try:
                message_data = json.loads(data)
                logger.info("websocket_message_received", data=message_data)
                
                # Echo back with metadata
                response = {
                    "type": "echo",
                    "original": message_data,
                    "client_id": manager.connection_metadata[websocket]["client_id"],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                await manager.send_personal_message(json.dumps(response), websocket)
                
                # Broadcast to others (optional)
                if message_data.get("broadcast", False):
                    broadcast_response = {
                        "type": "broadcast",
                        "from": manager.connection_metadata[websocket]["client_id"],
                        "message": message_data.get("message", ""),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    await manager.broadcast(json.dumps(broadcast_response))
                    
            except json.JSONDecodeError:
                # Handle plain text messages
                response = {
                    "type": "echo",
                    "message": data,
                    "client_id": manager.connection_metadata[websocket]["client_id"],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                await manager.send_personal_message(json.dumps(response), websocket)
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        # Broadcast disconnection
        broadcast_msg = {
            "type": "client_disconnected",
            "client_id": manager.connection_metadata.get(websocket, {}).get("client_id", "unknown"),
            "total_connections": len(manager.active_connections),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        await manager.broadcast(json.dumps(broadcast_msg))


if __name__ == "__main__":
    logger.info("starting_websocket_backend", port=8080)
    uvicorn.run(app, host="0.0.0.0", port=8080)


