# Compare gateways

`perf/compare/` measures this gateway against other gateways and a plain reverse proxy on
the same machines, under the same rules, so that the comparison can be rerun by anyone.

## What is compared

| Target | What runs on the gateway host |
|---|---|
| `nginx` | nginx as a reverse proxy that checks one static API key: the floor, with no gateway features |
| `python` | this gateway's Python process, as shipped (SQLite, no Redis) |
| `python-nodb` | the same, with request-log writes off |
| `python-tuned` | the same, with request-log writes and anomaly scoring off and validated keys cached for 5 s |
| `rust` | the Rust data plane in front of the Python gateway, both on the pinned cores |
| `rust-split` | the Rust data plane on the pinned cores, the Python control plane on the next core |
| `agentgateway` | [agentgateway](https://agentgateway.dev/) standalone, with a strict static API key |
| `litellm` | the [LiteLLM](https://docs.litellm.ai/) proxy, with a pass-through endpoint and its master key |

Every target listens on port 8000 in front of the same backend, and refuses a request
without its key (`target_up.sh` checks for that before each run). The configuration files
for the other gateways are `nginx.conf`, `agentgateway.yaml` and `litellm.yaml`.

## The rules

- **Same cores.** Each target is pinned with `taskset` to the cores in `--cores` (default
  `0-1`) and given that many workers or threads. `rust-split` puts the Python control plane,
  which is not on the request path, on the next core. Kernel network processing can run
  on any core, for every target alike.
- **Open-loop load.** The load generator ([vegeta](https://github.com/tsenart/vegeta)) sends at
  a constant rate whatever the target does, so a slow target shows up as latency and
  errors, not as a slower client.
- **Sustained rate.** A rate counts when, after a 5 s warm-up, at least 99.9% of requests
  succeed, at least 97% of the rate is answered, and p99 is at most 100 ms. A rate that
  fails is run once more before it counts as failed, because one transient stall can push
  a single run over the p99 limit. Each target's rate rises by `--step` until it fails,
  then one bisection step is tried.
- **Latency at a fixed rate.** Each target also runs at `--fixed-rate`, which every target
  should carry, to compare latency where none is saturated.
- **A fast backend.** The backend is nginx returning a fixed 11-byte JSON body, far faster
  than any target, so the backend does not set the result. Measure it directly first.
- **The load generator must not be the limit.** Measure its ceiling against the backend
  directly; pin targets to fewer cores if a target comes close to it.

## Running it

You need three Linux hosts on one private network: a gateway host, a backend host and a
load generator, described in a fleet file as for `perf/fleet/ladder.py`:

```json
{"user": "azureuser", "key": "~/.ssh/bench_key",
 "gateway": {"ssh": "10.0.0.4", "private": "10.0.0.4"},
 "backend": {"ssh": "10.0.0.5", "private": "10.0.0.5"},
 "loadgen": {"ssh": "10.0.0.6", "private": "10.0.0.6"}}
```

An optional `"known_hosts"` names the SSH known-hosts file to use. Running the driver on
the load generator itself, with private addresses, keeps a long run independent of your
own connection.

On every host: this repository in `~/gw`. On the gateway host: the Python virtualenv
(`~/gw/.venv`), the data plane built (`cargo build --release` in `dataplane/`), nginx,
`sysstat`, agentgateway at `~/bin/agentgateway`, LiteLLM in `~/litellm-venv`, and
`~/gw/run/secrets.env` with `ADMIN_TOKEN` and `SECRET_KEY`. On the backend: nginx serving
the fixed body on port 8003. On the load generator: vegeta.

```bash
python perf/compare/compare.py --fleet fleet.json --cores 0-0 --targets nginx,rust-split,agentgateway --out results.jsonl
python perf/compare/report.py results.jsonl
```

`--start-rates` sets where each target's ramp begins (the Python targets and LiteLLM start
low); `--step` sets how fast it climbs.

## Reading the results fairly

- One core per target measures the cost per request. It says nothing about how a target
  scales across cores; run with `--cores 0-1` or wider for that.
- Results near a target's limit vary from run to run by several per cent. Repeat a run
  before drawing a conclusion from a small difference.
- The targets do different amounts of work: nginx checks a key and proxies; the gateways
  also apply their own policies, metrics and logging as configured here. Say which
  configuration a number came from.
