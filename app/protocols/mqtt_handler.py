"""MQTT protocol handler."""
import asyncio
import json
from typing import Dict, Any, Optional, Callable
from paho.mqtt import client as mqtt_client
import structlog

logger = structlog.get_logger()


class MQTTHandler:
    """Handler for MQTT protocol connections."""
    
    def __init__(self, broker_url: str, client_id: Optional[str] = None):
        """
        Initialize MQTT handler.
        
        Args:
            broker_url: MQTT broker URL (e.g., mqtt://localhost:1883)
            client_id: Optional client ID
        """
        self.broker_url = broker_url
        self.client_id = client_id or f"gateway_{id(self)}"
        self.client: Optional[mqtt_client.Client] = None
        self.connected = False
        self.message_callbacks: Dict[str, Callable] = {}
        
    def _parse_broker_url(self) -> tuple[str, int, bool]:
        """Parse MQTT broker URL."""
        # Remove protocol prefix
        url = self.broker_url.replace("mqtt://", "").replace("mqtts://", "")
        
        # Split host and port
        if ":" in url:
            host, port = url.split(":", 1)
            port = int(port)
        else:
            host = url
            # Use default MQTT port from settings if available, otherwise 1883
            from app.config import get_settings
            settings = get_settings()
            port = settings.protocol_mqtt_default_port
        
        # Determine if SSL
        use_ssl = self.broker_url.startswith("mqtts://")
        
        return host, port, use_ssl
    
    def connect(self) -> bool:
        """Connect to MQTT broker."""
        try:
            host, port, use_ssl = self._parse_broker_url()
            
            self.client = mqtt_client.Client(
                client_id=self.client_id,
                protocol=mqtt_client.MQTTv5
            )
            
            # Set callbacks
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            
            # Connect
            if use_ssl:
                self.client.tls_set()
            
            self.client.connect(host, port, keepalive=60)
            self.client.loop_start()
            
            logger.info("mqtt_connected", broker=host, port=port)
            return True
            
        except Exception as e:
            logger.error("mqtt_connection_failed", error=str(e))
            return False
    
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """MQTT connection callback."""
        if rc == 0:
            self.connected = True
            logger.info("mqtt_connected_success")
        else:
            logger.error("mqtt_connection_failed", return_code=rc)
    
    def _on_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback."""
        self.connected = False
        logger.info("mqtt_disconnected", return_code=rc)
    
    def _on_message(self, client, userdata, msg):
        """MQTT message callback."""
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            
            # Try to parse as JSON
            try:
                data = json.loads(payload)
            except (ValueError, TypeError):
                data = {"raw": payload}
            
            # Call registered callback
            if topic in self.message_callbacks:
                self.message_callbacks[topic](topic, data)
            else:
                # Default handler
                logger.info("mqtt_message_received", topic=topic, payload=data)
                
        except Exception as e:
            logger.error("mqtt_message_error", error=str(e))
    
    def subscribe(self, topic: str, callback: Optional[Callable] = None, qos: int = 1):
        """Subscribe to MQTT topic."""
        if not self.connected:
            logger.warning("mqtt_not_connected")
            return False
        
        try:
            self.client.subscribe(topic, qos=qos)
            if callback:
                self.message_callbacks[topic] = callback
            logger.info("mqtt_subscribed", topic=topic, qos=qos)
            return True
        except Exception as e:
            logger.error("mqtt_subscribe_error", topic=topic, error=str(e))
            return False
    
    def publish(
        self,
        topic: str,
        payload: Dict[str, Any],
        qos: int = 1,
        retain: bool = False
    ) -> bool:
        """Publish message to MQTT topic."""
        if not self.connected:
            logger.warning("mqtt_not_connected")
            return False
        
        try:
            message = json.dumps(payload)
            result = self.client.publish(topic, message, qos=qos, retain=retain)
            
            if result.rc == mqtt_client.MQTT_ERR_SUCCESS:
                logger.info("mqtt_published", topic=topic, qos=qos)
                return True
            else:
                logger.error("mqtt_publish_failed", topic=topic, return_code=result.rc)
                return False
                
        except Exception as e:
            logger.error("mqtt_publish_error", topic=topic, error=str(e))
            return False
    
    def disconnect(self):
        """Disconnect from MQTT broker."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            logger.info("mqtt_disconnected")


class MQTTGateway:
    """MQTT gateway for routing MQTT messages."""
    
    def __init__(self, broker_url: str):
        """Initialize MQTT gateway."""
        self.handler = MQTTHandler(broker_url)
        self.topic_routes: Dict[str, str] = {}  # topic pattern -> backend
        
    def add_route(self, topic_pattern: str, backend_url: str):
        """Add route for MQTT topic pattern."""
        self.topic_routes[topic_pattern] = backend_url
        logger.info("mqtt_route_added", pattern=topic_pattern, backend=backend_url)
    
    def start(self) -> bool:
        """Start MQTT gateway."""
        if self.handler.connect():
            # Subscribe to all routed topics
            for topic_pattern in self.topic_routes.keys():
                self.handler.subscribe(topic_pattern)
            return True
        return False
    
    def stop(self):
        """Stop MQTT gateway."""
        self.handler.disconnect()

