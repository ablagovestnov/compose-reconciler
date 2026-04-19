# compose-reconciler

File-driven launcher for Docker Compose artifacts. Watches a directory, validates
each artifact against a security policy, and applies `docker compose up / down`.

Producers (humans, CI, AI coding agents) only write files; they never touch the
Docker socket. The reconciler is the sole component with daemon access.

## Contract

The reconciler expects this layout under `PROJECTS_DIR`:

```
projects/
  <slug>/
    docker-compose.yml        # or compose.yml / compose.yaml
    .env                      # optional, chmod 600 recommended
    <any tracked subdir>/     # site/, backend/, bot/ — tracked by default
    DEPLOY                    # touch to trigger apply
    REMOVE                    # touch to trigger teardown
    .reconciler/              # written by the reconciler (do not edit)
      status.json             # { state, containers, last_action, last_error }
      applied.hash            # content hashes of what's currently deployed
      last_apply.log          # stdout+stderr of the last compose invocation
```

Slugs must match `^[a-z][a-z0-9-]{1,30}$` by default and pass the reserved-slug
list. See `config/policy.example.yaml`.

## State machine

| state     | meaning                                                   |
|-----------|-----------------------------------------------------------|
| pending   | accepted, about to lint                                   |
| building  | `docker compose up -d --build` in flight                  |
| removing  | `docker compose down -v` in flight                        |
| up        | all containers running                                    |
| degraded  | some containers not running (crash loop, missing image)   |
| failed    | lint rejected / compose exited non-zero / timeout         |

On restart, any in-flight state is marked `failed` with a hint to re-touch
`DEPLOY`. Projects whose containers are already running but have no status
file are adopted (state recorded, hashes baselined).

## Policy enforcement

Every apply runs through `linter.py`, which rejects compose files that:

- Use a disallowed slug (pattern mismatch or reserved list).
- Declare `ports:` (use your reverse proxy's labels instead).
- Declare a privileged service key (`privileged`, `pid`, `network_mode`,
  `cap_add`, `devices`).
- Bind-mount a forbidden host path prefix (`/var/run`, `/etc`, `/root`, ...).
- Join a network that is neither the project's own `{prefix}{slug}-net`
  nor an explicitly allowed external network.
- Use a `container_name` that doesn't start with `{prefix}{slug}-`.
- Set `name:` to anything other than `{prefix}{slug}`.

All rules are data-driven — edit `config/policy.yaml` to tune for your host.

## Quickstart (local)

```sh
cp config/policy.example.yaml config/policy.yaml
mkdir -p projects
PROJECTS_DIR=./projects HOST_PROJECTS_DIR=$PWD/projects docker compose up --build
```

Drop a compose artifact under `projects/hello/`, `touch projects/hello/DEPLOY`,
then watch `projects/hello/.reconciler/status.json`.

## Env vars

| var                   | default                            | notes                                         |
|-----------------------|------------------------------------|-----------------------------------------------|
| `PROJECTS_DIR`        | `/projects`                        | path inside the container                     |
| `HOST_PROJECTS_DIR`   | — (warns if unset)                 | host-absolute path to the same directory      |
| `POLICY_FILE`         | `/etc/reconciler/policy.yaml`      | mounted from the host                         |
| `RECONCILE_INTERVAL`  | `30`                               | seconds between full scans                    |
| `COMPOSE_TIMEOUT`     | `900`                              | seconds per compose invocation                |

## Security posture

The reconciler holds `/var/run/docker.sock` — this is equivalent to root on
the host. Its job is to be the *only* component that holds it. Producers are
expected to be less trusted (human typos, AI hallucinations, compromised CI)
and are denied daemon access; they can only write files. The linter is the
boundary that prevents a malicious or mistaken compose file from escaping.

Run this service on hosts whose Docker daemon you own and whose producers
you trust to *some* degree — the linter stops accidents, not a determined
attacker with arbitrary write access to `PROJECTS_DIR`.
