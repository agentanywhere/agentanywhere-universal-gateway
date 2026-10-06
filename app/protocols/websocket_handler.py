"""WebSocket protocol handler."""
import asyncio
import json
from typing import Dict, Any, Optional, Callable
from fastapi import WebSocket, WebSocketDisconnect
import httpx
import structlog

logger = structlog.get_logger()


class WebSocketHandler:
    """Handler for WebSocket connections."""
    
    def __init__(self, backend_url: str):
        """
        Initialize WebSocket handler.
        
        Args:
            backend_url: Backend WebSocket URL
        """
        self.backend_url = backend_url
        self.active_connections: Dict[str, WebSocket] = {}
        self.message_handlers: Dict[str, Callable] = {}
        
    async def handle_connection(
        self,
        websocket: WebSocket,
        path: str,
        client_id: Optional[str] = None
    ):
        """Handle WebSocket connection."""
        await websocket.accept()
        
        connection_id = client_id or f"ws_{id(websocket)}"
        self.active_connections[connection_id] = websocket
        
        logger.info("websocket_connected", connection_id=connection_id, path=path)
        
        try:
            # Forward connection to backend
            async with httpx.AsyncClient() as client:
                backend_ws_url = f"{self.backend_url.rstrip('/')}{path}"
                
                # Proxy messages
                while True:
                    # Receive from client
                    try:
                        data = await websocket.receive_text()
                        message = json.loads(data)
                        
                        # Forward to backend
                        await self._forward_to_backend(client, backend_ws_url, message)
                        
                        # Send response back
                        response = await self._get_backend_response(client, backend_ws_url)
                        if response:
                            await websocket.send_text(json.dumps(response))
                            
                    except WebSocketDisconnect:
                        break
                    except json.JSONDecodeError:
                        # Handle binary or non-JSON messages
                        data = await websocket.receive_bytes()
                        await websocket.send_bytes(data)
                        
        except Exception as e:
            logger.error("websocket_error", connection_id=connection_id, error=str(e))
        finally:
            # Cleanup
            if connection_id in self.active_connections:
                del self.active_connections[connection_id]
            logger.info("websocket_disconnected", connection_id=connection_id)
    
    async def _forward_to_backend(
        self,
        client: httpx.AsyncClient,
        backend_url: str,
        message: Dict[str, Any]
    ):
        """Forward message to backend."""
        try:
            # For WebSocket, we'd typically use a WebSocket client
            # Here we simulate with HTTP for simplicity
            # In production, use websockets library for full WebSocket support
            response = await client.post(
                backend_url,
                json=message,
                timeout=30
            )
            return response.json() if response.status_code == 200 else None
        except Exception as e:
            logger.error("websocket_forward_error", error=str(e))
            return None
    
    async def _get_backend_response(
        self,
        client: httpx.AsyncClient,
        backend_url: str
    ) -> Optional[Dict[str, Any]]:
        """Get response from backend."""
        # This would be handled by WebSocket connection in production
        return None
    
    def broadcast(self, message: Dict[str, Any]):
        """Broadcast message to all connected clients."""
        disconnected = []
        for connection_id, websocket in self.active_connections.items():
            try:
                asyncio.create_task(websocket.send_text(json.dumps(message)))
            except Exception as e:
                logger.error("websocket_broadcast_error", connection_id=connection_id, error=str(e))
                disconnected.append(connection_id)
        
        # Clean up disconnected
        for conn_id in disconnected:
            del self.active_connections[conn_id]
    
    def get_connection_count(self) -> int:
        """Get number of active connections."""
        return len(self.active_connections)


