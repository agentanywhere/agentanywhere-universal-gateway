"""Unit tests for app.transformers."""
import json
import re

import pytest

from app import transformers as t
from app.transformers import (RequestTransformer, ResponseTransformer, TransformConfig,
                              TransformationManager, TransformationRule)


# ---------- helpers ----------
def test_glob_to_regex():
    assert t._glob_to_regex("/api/*").match("/api/users")
    assert t._glob_to_regex("^/x/\\d+$").match("/x/12")
    assert not t._glob_to_regex("/api/*").match("/other")


@pytest.mark.parametrize("body,expected", [
    (None, None), ({"a": 1}, {"a": 1}), (b'{"a":1}', {"a": 1}), (bytearray(b'{"a":2}'), {"a": 2}),
    (b"\xff\xfe", None), ('{"a":3}', {"a": 3}), ("not json", None), (42, None),
])
def test_coerce_body(body, expected):
    assert t._coerce_body_to_dict(body) == expected


def test_nested_helpers():
    d = {}
    t._set_nested(d, "a.b.c", 1)
    assert d == {"a": {"b": {"c": 1}}}
    d["a"]["b"] = "scalar"
    t._set_nested(d, "a.b.c", 2)
    assert d["a"]["b"] == {"c": 2}
    assert t._get_nested(d, "a.b.c") == 2 and t._get_nested(d, "a.x") is None
    t._remove_nested(d, "a.b.c")
    assert d["a"]["b"] == {}
    t._remove_nested(d, "zzz.q")
    t._remove_nested({"a": 1}, "a.b")  # intermediate not a dict -> falls out safely


# ---------- config ----------
def test_config_roundtrip():
    data = {"headers_set": {"X": "1"}, "headers_remove": ["Y"], "status_map": {500: "502"},
            "path_rewrite": {"pattern": "a", "replacement": "b"}, "template": "t"}
    cfg = TransformConfig.from_dict(data)
    assert cfg.status_map == {"500": 502}
    d = cfg.to_dict()
    assert d["path_rewrite"] == {"pattern": "a", "replacement": "b"} and d["template"] == "t"
    assert TransformConfig().to_dict()["path_rewrite"] is None


# ---------- rule matching ----------
def test_rule_matches_variants():
    r = TransformationRule(name="r", path_pattern="/api/*", methods=["get"])
    assert r.matches({"path": "/api/x", "method": "GET"})
    assert not r.matches({"path": "/api/x", "method": "POST"})
    assert r.matches({"path": "/api/x", "method": ""})
    assert not r.matches({"path": "/zzz", "method": "GET"})
    r.enabled = False
    assert not r.matches({"path": "/api/x"})
    assert TransformationRule(name="any", path_pattern="**").matches({})


def test_rule_bad_regex_falls_back_to_substring():
    r = TransformationRule(name="r", path_pattern="^(unclosed")
    assert not r.matches({"path": "/x"})
    assert r.matches({"path": "foo^(unclosed"})


def test_rule_condition():
    ok = TransformationRule(name="ok", condition=lambda c: c["path"] == "/a")
    assert ok.matches({"path": "/a"}) and not ok.matches({"path": "/b"})
    bad = TransformationRule(name="bad", condition=lambda c: 1 / 0)
    assert bad.matches({"path": "/a"}) is False


def test_rule_dict_roundtrip():
    r = TransformationRule.from_dict({"name": "n", "path_pattern": "/p", "methods": ["GET"], "enabled": False,
                                      "request_config": {"headers_set": {"a": "b"}},
                                      "response_config": {"status_map": {"500": 502}}})
    d = r.to_dict()
    assert d["enabled"] is False and d["has_request_transform"] and d["has_response_transform"]
    assert d["request_config"]["headers_set"] == {"a": "b"}
    assert TransformationRule(name="x").to_dict()["has_request_transform"] is False
    assert TransformationRule(name="x", transform_request=lambda *a: None).to_dict()["has_request_transform"]
    assert TransformationRule.from_dict({"name": "x"}).id


# ---------- apply ops ----------
def test_header_ops():
    cfg = TransformConfig(headers_rename={"x-old": "X-New"}, headers_remove=["REMOVE-ME"],
                          headers_set={"X-Set": "v"})
    out = t._apply_header_ops({"X-Old": "1", "Remove-Me": "2", "keep": "3"}, cfg)
    assert out == {"X-New": "1", "keep": "3", "X-Set": "v"}
    assert t._apply_header_ops({"a": "b"}, TransformConfig(headers_rename={"zz": "y"})) == {"a": "b"}


def test_query_ops():
    cfg = TransformConfig(query_remove=["a", "nope"], query_set={"c": "3"})
    assert t._apply_query_ops({"a": 1, "b": 2}, cfg) == {"b": 2, "c": "3"}


def test_body_ops_dict():
    cfg = TransformConfig(body_rename={"u.name": "user.full_name", "missing": "m"},
                          body_remove=["secret"], body_set={"meta.v": 2})
    body = {"u": {"name": "A"}, "secret": "s"}
    out = t._apply_body_ops(body, cfg)
    assert out == {"u": {}, "user": {"full_name": "A"}, "meta": {"v": 2}}


def test_body_ops_noop_and_unparseable():
    assert t._apply_body_ops("raw", TransformConfig()) == "raw"
    assert t._apply_body_ops("raw", TransformConfig(body_set={"a": 1})) == "raw"
    assert t._apply_body_ops("raw", TransformConfig(template="<{{ body }}>")) == "<raw>"
    assert t._apply_body_ops("raw", TransformConfig(template="{{ body.x.y(")) == "raw"  # bad template


def test_body_ops_template_on_dict():
    cfg = TransformConfig(template='{"n": "{{ body.name }}"}')
    assert t._apply_body_ops({"name": "z"}, cfg) == {"n": "z"}
    cfg2 = TransformConfig(template="Hello {{ body.name }}")
    assert t._apply_body_ops(b'{"name": "z"}', cfg2) == "Hello z"
    bad = TransformConfig(template="{% if %}")
    assert t._apply_body_ops({"a": 1}, bad) == {"a": 1}


def test_path_rewrite():
    assert t._apply_path_rewrite("/v1/x", TransformConfig()) == "/v1/x"
    assert t._apply_path_rewrite("/v1/x", TransformConfig(path_rewrite={"pattern": "/v1/", "replacement": "/v2/"})) == "/v2/x"
    assert t._apply_path_rewrite("/v1/x", TransformConfig(path_rewrite={"replacement": "q"})) == "/v1/x"
    assert t._apply_path_rewrite("/v1/x", TransformConfig(path_rewrite={"pattern": "(", "replacement": ""})) == "/v1/x"


# ---------- transformers ----------
def test_request_transformer_declarative_and_imperative():
    rt = RequestTransformer()
    cfg = TransformConfig(headers_set={"X": "1"}, query_set={"q": "1"}, body_set={"b": 1},
                          path_rewrite={"pattern": "old", "replacement": "new"})
    rt.add_rule(TransformationRule(name="a", request_config=cfg))
    rt.add_rule(TransformationRule(name="skip", path_pattern="/nomatch"))
    rt.add_rule(TransformationRule(name="imp3", transform_request=lambda m, p, h, q, b, c: ({**h, "I3": "y"}, q, b)))
    rt.add_rule(TransformationRule(name="imp4", transform_request=lambda m, p, h, q, b, c: (h, q, b, p + "/z")))
    rt.add_rule(TransformationRule(name="imp_none", transform_request=lambda *a: None))
    rt.add_rule(TransformationRule(name="imp_len2", transform_request=lambda *a: (1, 2)))
    rt.add_rule(TransformationRule(name="imp_err", transform_request=lambda *a: 1 / 0))
    h, q, b, p = rt.transform("GET", "/old", {"h": "v"}, {}, {"a": 1})
    assert h == {"h": "v", "X": "1", "I3": "y"} and q == {"q": "1"} and b == {"a": 1, "b": 1}
    assert p == "/new/z"


def test_request_transformer_template_helpers():
    rt = RequestTransformer()
    assert rt.transform_headers({"a": "1"}, '{"b": "{{ headers.a }}"}') == {"b": "1"}
    assert rt.transform_headers({"a": "1"}, "not-json") == {"a": "1"}
    assert rt.transform_headers({"a": "1"}, "{% if %}") == {"a": "1"}
    assert rt.transform_body({"a": 1}, '{"x": {{ body_json.a }}}') == {"x": 1}
    assert rt.transform_body(b'{"a": 1}', "plain {{ body }}").startswith("plain")
    assert rt.transform_body({"a": 1}, "{% bad") == {"a": 1}
    assert rt.transform_body(42, "v={{ body }}") == "v=42"


def test_response_transformer():
    rt = ResponseTransformer()
    cfg = TransformConfig(headers_set={"X": "1"}, body_set={"w": True}, status_map={"500": 502})
    rt.add_rule(TransformationRule(name="d", response_config=cfg))
    rt.add_rule(TransformationRule(name="i", transform_response=lambda s, h, b, c: (s + 1, {**h, "I": "1"}, b)))
    rt.add_rule(TransformationRule(name="none", transform_response=lambda *a: None))
    rt.add_rule(TransformationRule(name="err", transform_response=lambda *a: 1 / 0))
    s, h, b = rt.transform(500, {"Content-Length": "10", "content-length": "1"}, {"a": 1}, {"path": "/p", "method": "GET"})
    assert s == 503 and h == {"X": "1", "I": "1"} and b == {"a": 1, "w": True}
    s, h, b = rt.transform(200, {}, None)
    assert s == 201


# ---------- manager ----------
def test_manager_register_remove_list():
    m = TransformationManager()
    r = m.register_from_dict({"name": "a", "path_pattern": "/x/*",
                              "request_config": {"headers_set": {"A": "1"}}})
    r2 = TransformationRule(name="b", path_pattern="/x/*")
    m.add_rule("/x/*", r2)
    assert m.get_rule(r.id) is r and len(m.list_rules()) == 2
    assert m.remove_rule("nope") is False
    assert m.remove_rule(r.id) is True
    assert "/x/*" in m.rules_by_path
    assert m.remove_rule(r2.id) is True
    assert m.rules_by_path == {}


def test_manager_transform_request_response_isolated():
    m = TransformationManager()
    m.register_from_dict({"name": "a", "path_pattern": "/api/*", "methods": ["GET"],
                          "request_config": {"headers_set": {"A": "1"}, "path_rewrite": {"pattern": "api", "replacement": "svc"}},
                          "response_config": {"status_map": {"404": 410}}})
    h, q, b, p = m.transform_request("GET", "/api/x", {}, {}, None)
    assert h == {"A": "1"} and p == "/svc/x"
    assert m.request_transformer.rules == []  # restored
    h, q, b, p = m.transform_request("POST", "/api/x", {}, {}, None)
    assert h == {} and p == "/api/x"
    s, h, b = m.transform_response(404, {}, None, {"path": "/api/x", "method": "GET"})
    assert s == 410
    assert m.response_transformer.rules == []
    s, h, b = m.transform_response(404, {}, None)
    assert s == 404
    assert [r.name for r in m.get_rules_for_path("/api/x", "get")] == ["a"]


def test_builtin_helpers():
    r = t.create_version_transformation("v1", "v2")
    assert r.matches({"path": "/api/v1/users"})
    h, q, b, p = RequestTransformer.transform(_rt_with(r), "GET", "/api/v1/users", {}, {}, None)
    assert p == "/api/v2/users" and h["X-API-Version"] == "v2" and h["X-Original-Version"] == "v1"
    e = t.create_header_enrichment_transformation({"X-A": "1"})
    assert _rt_with(e).transform("GET", "/", {}, {}, None)[0] == {"X-A": "1"}


def _rt_with(rule):
    rt = RequestTransformer()
    rt.add_rule(rule)
    return rt
