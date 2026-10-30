"""Static checks on the shipped deployment files.

These read docker-compose.yml, the Dockerfile, env.example and
nginx/nginx.conf. They do not start containers; they pin the settings that
keep the documented Docker quick start working and its data persistent.
"""
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())


def _env_list(service):
    out = {}
    for item in service.get("environment", []):
        name, _, value = item.partition("=")
        out[name] = value
    return out


def _env_example():
    values = {}
    for line in (REPO / "env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.split("  #")[0].strip()
    return values


# --- SQLite database survives container recreation ---------------------------

def test_gateway_database_is_on_a_named_volume(compose):
    gateway = compose["services"]["gateway"]
    assert "gateway_data:/app/data" in gateway["volumes"]
    assert "gateway_data" in compose["volumes"]

    default = _env_list(gateway)["DATABASE_URL"]
    # ${DATABASE_URL:-<default>}: the default must be an absolute path on the volume.
    m = re.fullmatch(r"\$\{DATABASE_URL:-(.+)\}", default)
    assert m, default
    assert m.group(1) == "sqlite+aiosqlite:////app/data/gateway.db"


def test_dockerfile_creates_data_dir_for_non_root_user():
    text = (REPO / "Dockerfile").read_text()
    final_stage = text.split("FROM python:3.11-slim\n", 1)[1]
    mkdir = final_stage.index("mkdir -p /app/data")
    assert "chown gateway:gateway /app/data" in final_stage
    # Created (as root) before the switch to the non-root user.
    assert mkdir < final_stage.index("USER gateway")
    assert "WORKDIR /app" in final_stage


def test_env_example_does_not_override_the_compose_database_path():
    # An active relative SQLite URL in .env would win over the compose default
    # and put the database back inside the container.
    assert "DATABASE_URL" not in _env_example()


def test_dockerignore_keeps_local_databases_out_of_the_image():
    ignored = (REPO / ".dockerignore").read_text().splitlines()
    assert "*.db" in ignored
    assert "data/" in ignored


# --- backend URLs inside the container ----------------------------------------

BACKEND_VARS = {
    "MCP_BACKEND_URL": "mcp-backend",
    "A2A_BACKEND_URL": "a2a-backend",
    "HTTP_BACKEND_URL": "http-backend",
    "WEBSOCKET_BACKEND_URL": "websocket-backend",
    "MQTT_BROKER_URL": "mqtt-broker",
    "GRPC_BACKEND_URL": "grpc-backend",
    "GRAPHQL_BACKEND_URL": "http-backend",
}


@pytest.mark.parametrize("var,service", sorted(BACKEND_VARS.items()))
def test_compose_backend_urls_use_sample_service_names(compose, var, service):
    env = _env_list(compose["services"]["gateway"])
    value = env[var]
    # Must not interpolate the .env variable of the same name (env.example sets
    # it to localhost, which inside the container is the gateway itself).
    assert "${" + var not in value
    assert value.startswith("${DOCKER_" + var + ":-"), value
    assert service in value
    assert "localhost" not in value
    assert service in compose["services"]
    assert "backends" in compose["services"][service].get("profiles", [])


# --- ports and passwords -------------------------------------------------------

@pytest.mark.parametrize("service", ["redis", "postgres"])
def test_unauthenticated_stores_bind_to_localhost(compose, service):
    for port in compose["services"][service]["ports"]:
        assert str(port).startswith("127.0.0.1:"), port


def test_postgres_password_has_no_fallback_and_is_enforced(compose):
    pg = compose["services"]["postgres"]
    env = _env_list(pg)
    assert env["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD:-}"
    script = pg["entrypoint"][-1]
    assert '""|change-me*)' in script and "exit 1" in script
    assert script.rstrip().endswith("exec docker-entrypoint.sh postgres")
    assert pg["profiles"] == ["production"]


def test_grafana_password_has_no_fallback_and_is_enforced(compose):
    gf = compose["services"]["grafana"]
    env = _env_list(gf)
    assert env["GF_SECURITY_ADMIN_PASSWORD"] == "${GRAFANA_ADMIN_PASSWORD:-}"
    script = gf["entrypoint"][-1]
    assert '""|admin)' in script and "exit 1" in script
    assert script.rstrip().endswith("exec /run.sh")
    assert gf["profiles"] == ["monitoring"]


def test_env_example_ships_no_usable_grafana_or_postgres_password():
    values = _env_example()
    assert values.get("GRAFANA_ADMIN_PASSWORD", "") == ""
    assert "POSTGRES_PASSWORD" not in values


def test_no_required_variable_syntax_in_compose():
    # ${VAR:?} is evaluated for every service, active profile or not, so it
    # would break the default `docker compose up`.
    for line in (REPO / "docker-compose.yml").read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert not re.search(r"\$\{[A-Z_]+:?\?", line), line


# --- nginx: operational endpoints are not public ------------------------------

def _nginx_location(path):
    text = (REPO / "nginx" / "nginx.conf").read_text()
    m = re.search(r"location\s+" + re.escape(path) + r"\s*\{(.*?)\n    \}", text, re.S)
    assert m, f"no location {path} in nginx/nginx.conf"
    return [l.strip() for l in m.group(1).splitlines()
            if l.strip() and not l.strip().startswith("#")]


@pytest.mark.parametrize("path", ["/metrics", "/health/detailed"])
def test_nginx_restricts_operational_endpoints(path):
    lines = _nginx_location(path)
    allows = [l for l in lines if l.startswith("allow ")]
    assert {"allow 127.0.0.1;", "allow ::1;", "allow 10.0.0.0/8;",
            "allow 172.16.0.0/12;", "allow 192.168.0.0/16;"} <= set(allows)
    assert "allow all;" not in lines
    deny = lines.index("deny all;")
    # deny all comes after every allow (first match wins in nginx).
    assert all(lines.index(a) < deny for a in allows)
    assert any(l.startswith("proxy_pass http://gateway_backend") for l in lines)


def test_nginx_config_is_installed_as_a_server_level_file(compose):
    assert "./nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro" in compose["services"]["nginx"]["volumes"]
    assert "COPY nginx.conf /etc/nginx/conf.d/default.conf" in (REPO / "nginx" / "Dockerfile").read_text()
    text = (REPO / "nginx" / "nginx.conf").read_text()
    # upstream/server at top level: valid only inside the stock http {} block.
    assert re.search(r"^upstream ", text, re.M)
    assert not re.search(r"^(events|http)\s*\{", text, re.M)


def test_published_ports_bind_to_localhost_except_nginx(compose):
    """Only nginx's 80 and 443 may listen on every interface; the gateway's own port must not
    bypass nginx. Everything else binds to 127.0.0.1 unless BIND_ADDRESS says otherwise."""
    open_ports = {}
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            text = str(port)
            if text.startswith("127.0.0.1:") or text.startswith("${BIND_ADDRESS:-127.0.0.1}:"):
                continue
            open_ports.setdefault(name, []).append(text)
    assert open_ports == {"nginx": ["80:80", "443:443"]}, open_ports


def test_gateway_port_default_is_localhost(compose):
    assert compose["services"]["gateway"]["ports"] == ["${BIND_ADDRESS:-127.0.0.1}:${PORT:-8000}:8000"]


def test_admin_console_is_an_es_module_so_its_postcss_config_loads():
    """postcss.config.js and tailwind.config.js use `export default`; without "type": "module"
    the production build (and the console's Docker image) fails."""
    import json
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "admin-ui"
    assert json.loads((root / "package.json").read_text(encoding="utf-8")).get("type") == "module"
    assert "export default" in (root / "postcss.config.js").read_text(encoding="utf-8")


# --- sample backend health checks --------------------------------------------

@pytest.mark.parametrize("service", ["mcp-backend", "a2a-backend", "http-backend", "websocket-backend"])
def test_sample_backend_healthchecks_use_tools_in_the_image(compose, service):
    # The sample images are python:slim, which has neither wget nor curl.
    test = compose["services"][service]["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python", "-c"], test
