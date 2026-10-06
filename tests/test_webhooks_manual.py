#!/usr/bin/env python3
"""Manual test script for webhook functionality."""
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.webhooks import WebhookManager, WebhookEventType


async def test_webhook_basic_operations():
    """Test basic webhook operations."""
    print("🧪 Testing Webhook Support")
    print("=" * 50)
    
    manager = WebhookManager()
    
    try:
        # Test 1: Register webhook
        print("\n1️⃣ Testing webhook registration...")
        webhook_id = await manager.register_webhook(
            event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
            url="https://example.com/webhook",
            secret="test-secret-123"
        )
        assert webhook_id is not None
        assert webhook_id.startswith("rate_limit_exceeded_")
        print(f"   ✅ Webhook registered: {webhook_id}")
        
        # Test 2: List webhooks
        print("\n2️⃣ Testing webhook listing...")
        webhooks = manager.list_webhooks(WebhookEventType.RATE_LIMIT_EXCEEDED)
        assert len(webhooks) == 1
        assert webhooks[0]["id"] == webhook_id
        assert webhooks[0]["url"] == "https://example.com/webhook"
        print(f"   ✅ Found {len(webhooks)} webhook(s)")
        
        # Test 3: Register multiple webhooks
        print("\n3️⃣ Testing multiple webhook registration...")
        webhook_id2 = await manager.register_webhook(
            event_type=WebhookEventType.REQUEST_ERROR,
            url="https://example.com/error-webhook"
        )
        webhook_id3 = await manager.register_webhook(
            event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
            url="https://example.com/webhook2"
        )
        print(f"   ✅ Registered 2 more webhooks")
        
        # Test 4: List all webhooks
        print("\n4️⃣ Testing list all webhooks...")
        all_webhooks = manager.list_webhooks()
        assert len(all_webhooks) == 3
        print(f"   ✅ Found {len(all_webhooks)} total webhook(s)")
        
        # Test 5: List by event type
        rate_limit_webhooks = manager.list_webhooks(WebhookEventType.RATE_LIMIT_EXCEEDED)
        assert len(rate_limit_webhooks) == 2
        print(f"   ✅ Found {len(rate_limit_webhooks)} webhook(s) for rate_limit_exceeded")
        
        # Test 6: Unregister webhook
        print("\n5️⃣ Testing webhook unregistration...")
        success = await manager.unregister_webhook(
            WebhookEventType.RATE_LIMIT_EXCEEDED,
            webhook_id
        )
        assert success is True
        remaining = manager.list_webhooks(WebhookEventType.RATE_LIMIT_EXCEEDED)
        assert len(remaining) == 1
        print(f"   ✅ Webhook unregistered, {len(remaining)} remaining")
        
        # Test 7: Test webhook with filters
        print("\n6️⃣ Testing webhook filtering...")
        filtered_webhook_id = await manager.register_webhook(
            event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
            url="https://example.com/filtered-webhook",
            filters={"api_key_id": "key123"}
        )
        print(f"   ✅ Registered filtered webhook: {filtered_webhook_id}")
        
        # Test 8: Test event types
        print("\n7️⃣ Testing event types...")
        event_types = [
            WebhookEventType.RATE_LIMIT_EXCEEDED,
            WebhookEventType.REQUEST_ERROR,
            WebhookEventType.CIRCUIT_BREAKER_OPEN,
            WebhookEventType.ANOMALY_DETECTED,
            WebhookEventType.API_KEY_CREATED,
            WebhookEventType.API_KEY_REVOKED,
            WebhookEventType.HIGH_ERROR_RATE,
            WebhookEventType.TRAFFIC_SPIKE,
        ]
        print(f"   ✅ {len(event_types)} event types available")
        for event_type in event_types:
            print(f"      - {event_type}")
        
        print("\n" + "=" * 50)
        print("✅ All webhook tests passed!")
        print("=" * 50)
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        await manager.close()
    
    return True


async def test_webhook_structure():
    """Test webhook data structure."""
    print("\n🔍 Testing Webhook Data Structure")
    print("=" * 50)
    
    manager = WebhookManager()
    
    try:
        webhook_id = await manager.register_webhook(
            event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
            url="https://example.com/webhook",
            secret="test-secret",
            headers={"X-Custom-Header": "value"},
            filters={"api_key_id": "key123"}
        )
        
        webhooks = manager.list_webhooks(WebhookEventType.RATE_LIMIT_EXCEEDED)
        webhook = webhooks[0]
        
        print("\n📋 Webhook Structure:")
        print(f"   ID: {webhook['id']}")
        print(f"   URL: {webhook['url']}")
        print(f"   Enabled: {webhook.get('enabled', True)}")
        print(f"   Has Secret: {'Yes' if webhook.get('secret') else 'No'}")
        print(f"   Headers: {webhook.get('headers', {})}")
        print(f"   Filters: {webhook.get('filters', {})}")
        print(f"   Created At: {webhook.get('created_at', 'N/A')}")
        print(f"   Failure Count: {webhook.get('failure_count', 0)}")
        
        assert "id" in webhook
        assert "url" in webhook
        assert "enabled" in webhook
        assert webhook["enabled"] is True
        
        print("\n✅ Webhook structure is correct!")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        await manager.close()
    
    return True


async def main():
    """Run all tests."""
    print("\n" + "=" * 50)
    print("🚀 WEBHOOK SUPPORT TEST SUITE")
    print("=" * 50)
    
    results = []
    
    # Run tests
    results.append(await test_webhook_basic_operations())
    results.append(await test_webhook_structure())
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 TEST SUMMARY")
    print("=" * 50)
    passed = sum(results)
    total = len(results)
    print(f"✅ Passed: {passed}/{total}")
    print(f"{'❌ Failed: ' + str(total - passed) if passed < total else '✅ All tests passed!'}")
    print("=" * 50)
    
    return all(results)


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)

