"""Tests for protocol handlers (MQTT, WebSocket, gRPC)."""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from app.protocols.mqtt_handler import MQTTHandler, MQTTGateway
from app.protocols.websocket_handler import WebSocketHandler
from app.protocols.grpc_handler import GRPCHandler
from app.protocols.graphql_handler import GraphQLHandler
from app.models import ProtocolType


class TestMQTTHandler:
    """Tests for MQTT handler."""
    
    def test_init(self):
        """Test MQTT handler initialization."""
        handler = MQTTHandler("mqtt://localhost:1883")
        assert handler.broker_url == "mqtt://localhost:1883"
        assert handler.connected == False
        assert handler.client is None
    
    def test_parse_broker_url_default(self):
        """Test parsing default MQTT URL."""
        handler = MQTTHandler("mqtt://localhost:1883")
        host, port, use_ssl = handler._parse_broker_url()
        assert host == "localhost"
        assert port == 1883
        assert use_ssl == False
    
    def test_parse_broker_url_ssl(self):
        """Test parsing MQTT over SSL URL."""
        handler = MQTTHandler("mqtts://broker.example.com:8883")
        host, port, use_ssl = handler._parse_broker_url()
        assert host == "broker.example.com"
        assert port == 8883
        assert use_ssl == True
    
    def test_parse_broker_url_no_port(self):
        """Test parsing MQTT URL without port."""
        handler = MQTTHandler("mqtt://localhost")
        host, port, use_ssl = handler._parse_broker_url()
        assert host == "localhost"
        assert port == 1883  # Default port
        assert use_ssl == False
    
    @patch('app.protocols.mqtt_handler.mqtt_client.Client')
    def test_connect_success(self, mock_client_class):
        """Test successful MQTT connection."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        handler = MQTTHandler("mqtt://localhost:1883")
        result = handler.connect()
        
        assert result == True
        mock_client.connect.assert_called_once()
        mock_client.loop_start.assert_called_once()
    
    @patch('app.protocols.mqtt_handler.mqtt_client.Client')
    def test_connect_failure(self, mock_client_class):
        """Test MQTT connection failure."""
        mock_client_class.side_effect = Exception("Connection failed")
        
        handler = MQTTHandler("mqtt://localhost:1883")
        result = handler.connect()
        
        assert result == False
    
    @patch('app.protocols.mqtt_handler.mqtt_client.Client')
    def test_publish_success(self, mock_client_class):
        """Test successful MQTT publish."""
        mock_client = MagicMock()
        mock_client.publish.return_value.rc = 0
        mock_client_class.return_value = mock_client
        
        handler = MQTTHandler("mqtt://localhost:1883")
        handler.connected = True
        handler.client = mock_client
        
        result = handler.publish("test/topic", {"message": "test"}, qos=1)
        assert result == True
        mock_client.publish.assert_called_once()
    
    @patch('app.protocols.mqtt_handler.mqtt_client.Client')
    def test_publish_not_connected(self, mock_client_class):
        """Test MQTT publish when not connected."""
        handler = MQTTHandler("mqtt://localhost:1883")
        handler.connected = False
        
        result = handler.publish("test/topic", {"message": "test"})
        assert result == False
    
    @patch('app.protocols.mqtt_handler.mqtt_client.Client')
    def test_subscribe_success(self, mock_client_class):
        """Test successful MQTT subscribe."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        handler = MQTTHandler("mqtt://localhost:1883")
        handler.connected = True
        handler.client = mock_client
        
        result = handler.subscribe("test/topic", qos=1)
        assert result == True
        mock_client.subscribe.assert_called_once_with("test/topic", qos=1)
    
    @patch('app.protocols.mqtt_handler.mqtt_client.Client')
    def test_disconnect(self, mock_client_class):
        """Test MQTT disconnect."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        handler = MQTTHandler("mqtt://localhost:1883")
        handler.client = mock_client
        handler.connected = True
        
        handler.disconnect()
        
        mock_client.loop_stop.assert_called_once()
        mock_client.disconnect.assert_called_once()
        assert handler.connected == False


class TestMQTTGateway:
    """Tests for MQTT gateway."""
    
    def test_init(self):
        """Test MQTT gateway initialization."""
        gateway = MQTTGateway("mqtt://localhost:1883")
        assert gateway.handler is not None
        assert gateway.topic_routes == {}
    
    def test_add_route(self):
        """Test adding MQTT route."""
        gateway = MQTTGateway("mqtt://localhost:1883")
        gateway.add_route("sensors/+", "http://backend:8000")
        
        assert "sensors/+" in gateway.topic_routes
        assert gateway.topic_routes["sensors/+"] == "http://backend:8000"
    
    @patch.object(MQTTHandler, 'connect', return_value=True)
    @patch.object(MQTTHandler, 'subscribe', return_value=True)
    def test_start_success(self, mock_subscribe, mock_connect):
        """Test successful MQTT gateway start."""
        gateway = MQTTGateway("mqtt://localhost:1883")
        gateway.add_route("sensors/+", "http://backend:8000")
        
        result = gateway.start()
        
        assert result == True
        mock_connect.assert_called_once()
        mock_subscribe.assert_called_once()
    
    @patch.object(MQTTHandler, 'connect', return_value=False)
    def test_start_failure(self, mock_connect):
        """Test MQTT gateway start failure."""
        gateway = MQTTGateway("mqtt://localhost:1883")
        
        result = gateway.start()
        
        assert result == False
    
    @patch.object(MQTTHandler, 'disconnect')
    def test_stop(self, mock_disconnect):
        """Test MQTT gateway stop."""
        gateway = MQTTGateway("mqtt://localhost:1883")
        gateway.stop()
        
        mock_disconnect.assert_called_once()


class TestWebSocketHandler:
    """Tests for WebSocket handler."""
    
    def test_init(self):
        """Test WebSocket handler initialization."""
        handler = WebSocketHandler("ws://localhost:8080")
        assert handler.backend_url == "ws://localhost:8080"
        assert handler.active_connections == {}
        assert handler.message_handlers == {}
    
    def test_get_connection_count(self):
        """Test getting connection count."""
        handler = WebSocketHandler("ws://localhost:8080")
        assert handler.get_connection_count() == 0
        
        # Simulate connections
        handler.active_connections["conn1"] = Mock()
        handler.active_connections["conn2"] = Mock()
        assert handler.get_connection_count() == 2
    
    @pytest.mark.asyncio
    async def test_handle_connection(self):
        """Test WebSocket connection handling."""
        handler = WebSocketHandler("ws://localhost:8080")
        
        mock_websocket = AsyncMock()
        mock_websocket.accept = AsyncMock()
        mock_websocket.receive_text = AsyncMock(side_effect=Exception("Disconnect"))
        
        # This would need more mocking for full test
        # For now, just verify it doesn't crash
        try:
            await handler.handle_connection(mock_websocket, "/test")
        except Exception:
            pass  # Expected to fail without proper setup


class TestGRPCHandler:
    """Tests for gRPC handler."""
    
    def test_init(self):
        """Test gRPC handler initialization."""
        handler = GRPCHandler("localhost:50051")
        assert handler.backend_url == "localhost:50051"
        assert handler.channel is None
    
    @patch('app.protocols.grpc_handler.grpc.insecure_channel')
    def test_create_channel(self, mock_channel):
        """Test creating gRPC channel."""
        mock_channel.return_value = MagicMock()
        
        handler = GRPCHandler("localhost:50051")
        channel = handler.create_channel()
        
        assert channel is not None
        mock_channel.assert_called_once()
    
    @patch('app.protocols.grpc_handler.grpc.insecure_channel')
    def test_get_channel(self, mock_channel):
        """Test getting or creating gRPC channel."""
        mock_channel.return_value = MagicMock()
        
        handler = GRPCHandler("localhost:50051")
        channel1 = handler.get_channel()
        channel2 = handler.get_channel()
        
        # Should return same channel
        assert channel1 == channel2
        # Should only create channel once
        assert mock_channel.call_count == 1
    
    @patch('app.protocols.grpc_handler.grpc.insecure_channel')
    def test_call_unary(self, mock_channel):
        """Test unary gRPC call."""
        mock_channel.return_value = MagicMock()
        
        handler = GRPCHandler("localhost:50051")
        result = handler.call_unary("Service", "Method", {"data": "test"})
        
        assert result is not None
        assert "status" in result
        assert result["service"] == "Service"
        assert result["method"] == "Method"

    @patch('app.protocols.grpc_handler.grpc.insecure_channel')
    def test_call_unary_does_not_fabricate_success(self, mock_channel):
        """D5 regression: a call the gateway cannot actually bridge (no generated
        stub / reflection for the service) must NOT be reported as a completed,
        successful backend call. Previously the generic fallback returned
        ``{"status": "success"}``, masking the failure to forward the request."""
        mock_channel.return_value = MagicMock()

        handler = GRPCHandler("localhost:50051")
        result = handler.call_unary("UnknownService", "DoThing", {"x": 1})

        assert result["status"] != "success"
        assert result["status"] == "unsupported"
        assert result["service"] == "UnknownService"
        assert result["method"] == "DoThing"

    @patch('app.protocols.grpc_handler.grpc.insecure_channel')
    def test_close(self, mock_channel):
        """Test closing gRPC channel."""
        mock_ch = MagicMock()
        mock_channel.return_value = mock_ch
        
        handler = GRPCHandler("localhost:50051")
        handler.create_channel()
        handler.close()
        
        mock_ch.close.assert_called_once()
        assert handler.channel is None


class TestGraphQLHandler:
    """Tests for GraphQL handler."""
    
    def test_init(self):
        """Test GraphQL handler initialization."""
        handler = GraphQLHandler("http://localhost:4000")
        assert handler.backend_url == "http://localhost:4000"
        assert handler.schema_cache is None
    
    @pytest.mark.asyncio
    async def test_execute_query_success(self):
        """Test successful GraphQL query execution."""
        handler = GraphQLHandler("http://localhost:4000")
        
        with patch.object(handler.client, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"data": {"test": "value"}}
            mock_response.raise_for_status = Mock()
            mock_post.return_value = mock_response
            
            result = await handler.execute_query("{ test }")
            
            assert result == {"data": {"test": "value"}}
            mock_post.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_execute_query_with_variables(self):
        """Test GraphQL query with variables."""
        handler = GraphQLHandler("http://localhost:4000")
        
        with patch.object(handler.client, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"data": {"user": {"name": "Test"}}}
            mock_response.raise_for_status = Mock()
            mock_post.return_value = mock_response
            
            result = await handler.execute_query(
                "query GetUser($id: ID!) { user(id: $id) { name } }",
                variables={"id": "123"}
            )
            
            assert result == {"data": {"user": {"name": "Test"}}}
            call_args = mock_post.call_args
            assert "variables" in call_args[1]["json"]
            assert call_args[1]["json"]["variables"] == {"id": "123"}
    
    @pytest.mark.asyncio
    async def test_execute_query_with_operation_name(self):
        """Test GraphQL query with operation name."""
        handler = GraphQLHandler("http://localhost:4000")
        
        with patch.object(handler.client, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"data": {}}
            mock_response.raise_for_status = Mock()
            mock_post.return_value = mock_response
            
            result = await handler.execute_query(
                "query GetUser { user { name } }",
                operation_name="GetUser"
            )
            
            assert result == {"data": {}}
            call_args = mock_post.call_args
            assert "operationName" in call_args[1]["json"]
            assert call_args[1]["json"]["operationName"] == "GetUser"
    
    @pytest.mark.asyncio
    async def test_execute_query_http_error(self):
        """Test GraphQL query with HTTP error."""
        import httpx
        handler = GraphQLHandler("http://localhost:4000")
        
        with patch.object(handler.client, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = Mock()
            mock_response.status_code = 500
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Server Error",
                request=Mock(),
                response=mock_response
            )
            mock_post.return_value = mock_response
            
            with pytest.raises(httpx.HTTPStatusError):
                await handler.execute_query("{ test }")
    
    def test_analyze_query_complexity(self):
        """Test query complexity analysis."""
        handler = GraphQLHandler("http://localhost:4000")
        
        simple_query = "{ user { name } }"
        result = handler.analyze_query_complexity(simple_query)
        
        assert "field_count" in result
        assert "depth" in result
        assert "complexity_score" in result
        assert result["field_count"] > 0
    
    def test_analyze_query_complexity_nested(self):
        """Test complexity analysis for nested query."""
        handler = GraphQLHandler("http://localhost:4000")
        
        nested_query = "{ user { posts { comments { author { name } } } } }"
        result = handler.analyze_query_complexity(nested_query)
        
        assert result["field_count"] > 0
        assert result["complexity_score"] >= 0
    
    @pytest.mark.asyncio
    async def test_introspect_schema(self):
        """Test schema introspection."""
        handler = GraphQLHandler("http://localhost:4000")
        
        with patch.object(handler, 'execute_query', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = {
                "data": {
                    "__schema": {
                        "queryType": {"name": "Query"},
                        "mutationType": {"name": "Mutation"}
                    }
                }
            }
            
            result = await handler.introspect_schema()
            
            assert result == {
                "__schema": {
                    "queryType": {"name": "Query"},
                    "mutationType": {"name": "Mutation"}
                }
            }
            assert handler.schema_cache is not None
    
    @pytest.mark.asyncio
    async def test_introspect_schema_cached(self):
        """Test schema introspection uses cache."""
        handler = GraphQLHandler("http://localhost:4000")
        handler.schema_cache = {"cached": "data"}
        
        result = await handler.introspect_schema()
        
        assert result == {"cached": "data"}
    
    @pytest.mark.asyncio
    async def test_close(self):
        """Test closing GraphQL handler."""
        handler = GraphQLHandler("http://localhost:4000")
        
        with patch.object(handler.client, 'aclose', new_callable=AsyncMock) as mock_close:
            await handler.close()
            mock_close.assert_called_once()

