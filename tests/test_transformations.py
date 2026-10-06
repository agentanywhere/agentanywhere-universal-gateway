"""Tests for declarative request/response transformations."""
from app.transformers import (
    TransformConfig,
    TransformationManager,
    TransformationRule,
    create_header_enrichment_transformation,
    create_version_transformation,
    transformation_manager,
)


class TestTransformConfig:
    def test_from_dict_round_trip(self):
        data = {
            "headers_set": {"X-Foo": "bar"},
            "headers_remove": ["X-Old"],
            "headers_rename": {"X-Legacy": "X-Modern"},
            "query_set": {"v": "2"},
            "query_remove": ["debug"],
            "body_set": {"meta.source": "gateway"},
            "body_remove": ["secret"],
            "body_rename": {"old.path": "new.path"},
            "status_map": {"500": 502},
            "path_rewrite": {"pattern": "^/api/", "replacement": "/v2/api/"},
            "template": "{{ body }}",
        }
        cfg = TransformConfig.from_dict(data)
        assert cfg.to_dict() == data


class TestRequestTransformation:
    def setup_method(self):
        self.mgr = TransformationManager()

    def test_set_and_remove_headers(self):
        self.mgr.register_rule(
            TransformationRule(
                name="headers",
                path_pattern="*",
                request_config=TransformConfig(
                    headers_set={"X-Trace": "abc"},
                    headers_remove=["X-Internal"],
                ),
            )
        )
        h, q, b, p = self.mgr.transform_request(
            method="GET",
            path="/users",
            headers={"X-Internal": "secret", "X-Other": "keep"},
            query_params={},
        )
        assert h["X-Trace"] == "abc"
        assert "X-Internal" not in h
        assert h["X-Other"] == "keep"

    def test_rename_headers_case_insensitive(self):
        self.mgr.register_rule(
            TransformationRule(
                name="rename",
                path_pattern="*",
                request_config=TransformConfig(headers_rename={"X-Old": "X-New"}),
            )
        )
        h, _, _, _ = self.mgr.transform_request(
            method="GET",
            path="/anything",
            headers={"x-old": "value"},
            query_params={},
        )
        assert "X-New" in h
        assert "x-old" not in h
        assert "X-Old" not in h

    def test_query_set_and_remove(self):
        self.mgr.register_rule(
            TransformationRule(
                name="query",
                path_pattern="*",
                request_config=TransformConfig(
                    query_set={"v": "2"}, query_remove=["debug"]
                ),
            )
        )
        _, q, _, _ = self.mgr.transform_request(
            method="GET",
            path="/foo",
            headers={},
            query_params={"debug": "true", "x": "y"},
        )
        assert q == {"v": "2", "x": "y"}

    def test_body_set_remove_rename(self):
        self.mgr.register_rule(
            TransformationRule(
                name="body",
                path_pattern="*",
                request_config=TransformConfig(
                    body_set={"meta.source": "gateway"},
                    body_remove=["password"],
                    body_rename={"username": "user.name"},
                ),
            )
        )
        body = {"username": "alice", "password": "secret"}
        _, _, transformed, _ = self.mgr.transform_request(
            method="POST",
            path="/users",
            headers={},
            query_params={},
            body=body,
        )
        assert transformed == {
            "user": {"name": "alice"},
            "meta": {"source": "gateway"},
        }

    def test_path_rewrite(self):
        self.mgr.register_rule(
            TransformationRule(
                name="path",
                path_pattern="*",
                request_config=TransformConfig(
                    path_rewrite={"pattern": r"^/old/", "replacement": "/new/"}
                ),
            )
        )
        _, _, _, p = self.mgr.transform_request(
            method="GET",
            path="/old/users",
            headers={},
            query_params={},
        )
        assert p == "/new/users"

    def test_method_filter(self):
        self.mgr.register_rule(
            TransformationRule(
                name="get-only",
                path_pattern="*",
                methods=["GET"],
                request_config=TransformConfig(headers_set={"X-Get": "1"}),
            )
        )
        h_get, _, _, _ = self.mgr.transform_request("GET", "/x", {}, {})
        h_post, _, _, _ = self.mgr.transform_request("POST", "/x", {}, {})
        assert h_get.get("X-Get") == "1"
        assert "X-Get" not in h_post

    def test_path_pattern_filtering(self):
        self.mgr.register_rule(
            TransformationRule(
                name="only-api",
                path_pattern="/api/*",
                request_config=TransformConfig(headers_set={"X-API": "1"}),
            )
        )
        h_match, _, _, _ = self.mgr.transform_request("GET", "/api/users", {}, {})
        h_other, _, _, _ = self.mgr.transform_request("GET", "/other/users", {}, {})
        assert h_match.get("X-API") == "1"
        assert "X-API" not in h_other

    def test_disabled_rule_is_skipped(self):
        self.mgr.register_rule(
            TransformationRule(
                name="off",
                path_pattern="*",
                enabled=False,
                request_config=TransformConfig(headers_set={"X-Off": "1"}),
            )
        )
        h, _, _, _ = self.mgr.transform_request("GET", "/x", {}, {})
        assert "X-Off" not in h


class TestResponseTransformation:
    def setup_method(self):
        self.mgr = TransformationManager()

    def test_status_map(self):
        self.mgr.register_rule(
            TransformationRule(
                name="map-500",
                path_pattern="*",
                response_config=TransformConfig(status_map={"500": 502}),
            )
        )
        s, h, b = self.mgr.transform_response(
            status_code=500,
            headers={},
            body=None,
            request_context={"path": "/anything", "method": "GET"},
        )
        assert s == 502

    def test_response_headers(self):
        self.mgr.register_rule(
            TransformationRule(
                name="powered-by",
                path_pattern="*",
                response_config=TransformConfig(headers_set={"X-Powered-By": "Gateway"}),
            )
        )
        s, h, _ = self.mgr.transform_response(
            status_code=200,
            headers={"Content-Length": "42"},
            body={},
            request_context={"path": "/x", "method": "GET"},
        )
        assert h["X-Powered-By"] == "Gateway"
        assert "Content-Length" not in h

    def test_response_body_remove(self):
        self.mgr.register_rule(
            TransformationRule(
                name="strip-internal",
                path_pattern="*",
                response_config=TransformConfig(body_remove=["internal"]),
            )
        )
        _, _, body = self.mgr.transform_response(
            status_code=200,
            headers={},
            body={"data": "ok", "internal": "hidden"},
            request_context={"path": "/x", "method": "GET"},
        )
        assert body == {"data": "ok"}


class TestManagerLifecycle:
    def setup_method(self):
        self.mgr = TransformationManager()

    def test_register_from_dict_returns_id(self):
        rule = self.mgr.register_from_dict(
            {
                "name": "x",
                "path_pattern": "*",
                "request_config": {"headers_set": {"X": "1"}},
            }
        )
        assert rule.id
        assert self.mgr.get_rule(rule.id) is rule

    def test_remove_rule(self):
        rule = self.mgr.register_from_dict({"name": "x", "request_config": {"headers_set": {"X": "1"}}})
        assert self.mgr.remove_rule(rule.id) is True
        assert self.mgr.get_rule(rule.id) is None
        assert self.mgr.remove_rule(rule.id) is False

    def test_list_rules(self):
        self.mgr.register_from_dict({"name": "a", "request_config": {"headers_set": {"X": "1"}}})
        self.mgr.register_from_dict({"name": "b", "response_config": {"headers_set": {"Y": "1"}}})
        assert {r.name for r in self.mgr.list_rules()} == {"a", "b"}


class TestBuiltinHelpers:
    def test_version_transformation(self):
        rule = create_version_transformation("v1", "v2")
        mgr = TransformationManager()
        mgr.register_rule(rule)
        h, _, _, p = mgr.transform_request(
            method="GET",
            path="/api/v1/users",
            headers={},
            query_params={},
        )
        assert h["X-API-Version"] == "v2"
        assert h["X-Original-Version"] == "v1"
        assert "/v2/" in p

    def test_header_enrichment(self):
        rule = create_header_enrichment_transformation({"X-Env": "prod"})
        mgr = TransformationManager()
        mgr.register_rule(rule)
        h, _, _, _ = mgr.transform_request("GET", "/x", {}, {})
        assert h["X-Env"] == "prod"


def teardown_module(_):
    """Reset global manager state between test modules to keep things clean."""
    transformation_manager.rules_by_id.clear()
    transformation_manager.rules_by_path.clear()
