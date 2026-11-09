"""IP rule scoping (tenant / api_key) and engine equivalence with a reference."""
from __future__ import annotations

import ipaddress
import random
from types import SimpleNamespace

import pytest

from app.exceptions import AuthorizationError
from app.governance import IPRule, IPRuleEngine, _ip_matches_any_cidr
from app.middleware.auth import AuthMiddleware


def rule(rid, cidr, action, scope="global", scope_id=None):
    return IPRule(id=rid, cidr=cidr, action=action, scope=scope,
                  scope_id=scope_id, description=None, enabled=True)


def engine(*rules):
    e = IPRuleEngine()
    e._rules = list(rules)
    return e


def old_evaluate(rules, ip, tenant=None, api_key_id=None):
    """The pre-fix implementation, kept verbatim as the buggy reference."""
    for scope_name, scope_id in [("global", None), ("tenant", tenant), ("api_key", api_key_id)]:
        scope_rules = [r for r in rules
                       if r.scope == scope_name and (scope_id is None or r.scope_id == scope_id)]
        if not scope_rules:
            continue
        for r in scope_rules:
            if r.action == "deny" and _ip_matches_any_cidr(ip, [r.cidr]):
                return False, r
        allow = [r for r in scope_rules if r.action == "allow"]
        if allow and not any(_ip_matches_any_cidr(ip, [r.cidr]) for r in allow):
            return False, allow[0]
    return True, None


def straightforward(rules, ip, tenant=None, api_key_id=None):
    """Fixed semantics, written naively (no index, parses every time)."""
    for scope_name, scope_id in [("global", None), ("tenant", tenant), ("api_key", api_key_id)]:
        if scope_name == "global":
            scope_rules = [r for r in rules if r.scope == "global"]
        elif scope_id is None:
            continue
        else:
            scope_rules = [r for r in rules if r.scope == scope_name and r.scope_id == scope_id]
        if not scope_rules:
            continue
        for r in scope_rules:
            if r.action == "deny" and _ip_matches_any_cidr(ip, [r.cidr]):
                return False, r
        allow = [r for r in scope_rules if r.action == "allow"]
        if allow and not any(_ip_matches_any_cidr(ip, [r.cidr]) for r in allow):
            return False, allow[0]
    return True, None


IP = "203.0.113.9"
NET = "203.0.113.0/24"


# --- the old behaviour was wrong -------------------------------------------
def test_old_engine_applied_tenant_rule_to_tenantless_request():
    rules = [rule("t", NET, "deny", "tenant", "acme")]
    assert old_evaluate(rules, IP, tenant=None)[0] is False  # the bug
    assert engine(*rules).evaluate(IP, tenant=None)[0] is True


def test_old_middleware_passed_plaintext_key_so_key_rules_never_matched():
    rules = [rule("k", NET, "deny", "api_key", "key-id-1")]
    assert old_evaluate(rules, IP, api_key_id="sk-plaintext")[0] is True  # bypass
    assert engine(*rules).evaluate(IP, api_key_id="key-id-1")[0] is False


# --- global ----------------------------------------------------------------
def test_global_deny_applies_to_everyone():
    e = engine(rule("g", NET, "deny"))
    for kw in ({}, {"tenant": "acme"}, {"api_key_id": "k"}, {"tenant": "x", "api_key_id": "k"}):
        ok, m = e.evaluate(IP, **kw)
        assert ok is False and m.id == "g"
    assert e.evaluate("198.51.100.1", tenant="acme")[0] is True


def test_global_allow_is_allowlist_for_everyone():
    e = engine(rule("g", NET, "allow"))
    assert e.evaluate(IP)[0] is True
    assert e.evaluate("198.51.100.1", tenant="acme")[0] is False
    assert e.evaluate("198.51.100.1")[0] is False


# --- tenant ----------------------------------------------------------------
def test_tenant_deny_only_blocks_that_tenant():
    e = engine(rule("t", NET, "deny", "tenant", "acme"))
    ok, m = e.evaluate(IP, tenant="acme")
    assert ok is False and m.id == "t"
    assert e.evaluate(IP, tenant="other")[0] is True
    assert e.evaluate(IP, tenant=None)[0] is True


def test_tenant_allow_only_restricts_that_tenant():
    e = engine(rule("t", NET, "allow", "tenant", "acme"))
    assert e.evaluate(IP, tenant="acme")[0] is True
    assert e.evaluate("198.51.100.1", tenant="acme")[0] is False
    assert e.evaluate("198.51.100.1", tenant="other")[0] is True
    assert e.evaluate("198.51.100.1", tenant=None)[0] is True


def test_tenant_scoped_rule_without_scope_id_applies_to_nobody():
    e = engine(rule("t", NET, "deny", "tenant", None))
    assert e.evaluate(IP, tenant="acme")[0] is True
    assert e.evaluate(IP, tenant=None)[0] is True


# --- api_key ---------------------------------------------------------------
def test_api_key_deny_only_blocks_that_key_id():
    e = engine(rule("k", NET, "deny", "api_key", "kid-1"))
    ok, m = e.evaluate(IP, api_key_id="kid-1")
    assert ok is False and m.id == "k"
    assert e.evaluate(IP, api_key_id="kid-2")[0] is True
    assert e.evaluate(IP, api_key_id=None)[0] is True


def test_api_key_allow_only_restricts_that_key_id():
    e = engine(rule("k", NET, "allow", "api_key", "kid-1"))
    assert e.evaluate(IP, api_key_id="kid-1")[0] is True
    assert e.evaluate("198.51.100.1", api_key_id="kid-1")[0] is False
    assert e.evaluate("198.51.100.1", api_key_id="kid-2")[0] is True
    assert e.evaluate("198.51.100.1")[0] is True


# --- middleware uses the key id, not the plaintext key ---------------------
def _req(ip=IP, tenant_id=None):
    return SimpleNamespace(
        headers={}, url=SimpleNamespace(path="/x"), method="GET",
        client=SimpleNamespace(host=ip),
        state=SimpleNamespace(request_id="rid", tenant_id=tenant_id),
    )


@pytest.fixture
def gov(monkeypatch):
    import app.governance as g
    from app.governance import PolicyEngine
    monkeypatch.setattr(g, "ip_rule_engine", IPRuleEngine())
    monkeypatch.setattr(g, "policy_engine", PolicyEngine())
    return g


@pytest.mark.asyncio
async def test_middleware_key_scoped_deny_matches_by_id(gov):
    gov.ip_rule_engine._rules = [rule("k", NET, "deny", "api_key", "kid-1")]
    mw = AuthMiddleware()
    with pytest.raises(AuthorizationError):
        await mw._enforce_governance(_req(), SimpleNamespace(key="sk-secret", id="kid-1"))
    await mw._enforce_governance(_req(), SimpleNamespace(key="sk-secret", id="kid-2"))
    # a rule naming the plaintext key must not match (key is never an identifier)
    gov.ip_rule_engine._rules = [rule("k2", NET, "deny", "api_key", "sk-secret")]
    await mw._enforce_governance(_req(), SimpleNamespace(key="sk-secret", id="kid-1"))
    # principals without an id are not subject to key-scoped rules
    await mw._enforce_governance(_req(), SimpleNamespace(key="sk-secret", id=None))


@pytest.mark.asyncio
async def test_middleware_tenant_rule_does_not_hit_other_or_no_tenant(gov):
    gov.ip_rule_engine._rules = [rule("t", NET, "deny", "tenant", "acme")]
    mw = AuthMiddleware()
    p = SimpleNamespace(key="k", id="kid")
    with pytest.raises(AuthorizationError):
        await mw._enforce_governance(_req(tenant_id="acme"), p)
    await mw._enforce_governance(_req(tenant_id="other"), p)
    await mw._enforce_governance(_req(tenant_id=None), p)


# --- equivalence with a straightforward reference --------------------------
def _rand_cidr(rnd):
    kind = rnd.random()
    if kind < 0.1:
        return rnd.choice(["not-a-cidr", "300.1.1.1/8", "", "10.0.0.0/99", "::zz/8"])
    if kind < 0.35:
        return f"2001:db8:{rnd.randrange(4):x}::/{rnd.choice([32, 48, 64, 128])}"
    base = f"10.{rnd.randrange(4)}.{rnd.randrange(4)}.{rnd.randrange(256)}"
    return f"{base}/{rnd.choice([8, 16, 24, 32])}"


def _rand_ip(rnd):
    k = rnd.random()
    if k < 0.1:
        return rnd.choice(["", "garbage", "999.1.1.1"])
    if k < 0.35:
        return f"2001:db8:{rnd.randrange(4):x}::{rnd.randrange(5):x}"
    return f"10.{rnd.randrange(4)}.{rnd.randrange(4)}.{rnd.randrange(256)}"


def test_random_equivalence_with_reference():
    rnd = random.Random(1234)
    tenants = ["a", "b", None]
    keys = ["k1", "k2", None]
    for _ in range(300):
        rules = []
        for i in range(rnd.randrange(0, 14)):
            scope = rnd.choice(["global", "tenant", "api_key"])
            sid = None if scope == "global" else rnd.choice(["a", "b", "k1", "k2", None])
            rules.append(rule(f"r{i}", _rand_cidr(rnd), rnd.choice(["allow", "deny", "deny", "weird"]), scope, sid))
        e = engine(*rules)
        for _ in range(25):
            ip, t, k = _rand_ip(rnd), rnd.choice(tenants), rnd.choice(keys)
            got = e.evaluate(ip, tenant=t, api_key_id=k)
            want = straightforward(rules, ip, t, k)
            assert got[0] == want[0] and (got[1] is want[1]), (ip, t, k)


def test_index_rebuilds_after_direct_mutation():
    e = engine(rule("g", NET, "deny"))
    assert e.evaluate(IP)[0] is False
    e._rules.clear()
    assert e.evaluate(IP)[0] is True
    e._rules.append(rule("g2", NET, "deny"))
    assert e.evaluate(IP)[1].id == "g2"
    e._rules = [rule("g3", NET, "deny")]
    assert e.evaluate(IP)[1].id == "g3"
    assert [r["id"] for r in e.list_rules()] == ["g3"]


def test_evaluate_does_not_parse_networks(monkeypatch):
    e = engine(*[rule(f"r{i}", f"10.0.{i}.0/24", "deny") for i in range(50)])
    e.evaluate("10.9.9.9")  # builds index
    calls = []
    real = ipaddress.ip_network
    monkeypatch.setattr(ipaddress, "ip_network", lambda *a, **k: calls.append(a) or real(*a, **k))
    e.evaluate("10.9.9.9")
    assert calls == []
