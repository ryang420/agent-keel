# Sandbox

Tools (`terminal`, `execute_code`, file tools, skills) run inside a Docker container owned by the profile. A
profile gets tools only when it has a non-empty `sandbox` policy and the service runs with `KEEL_SANDBOX=docker`.
A profile with an empty policy answers without tools.

The policy is set on `PUT /v1/profiles/{id}` and checked there (400 `invalid_input` when invalid). It is checked
again when a run starts; a policy that has become invalid, or a container that cannot be created or reached, ends
the run as `failed` with `error_code: sandbox_unavailable`.

## Policy keys

Only these keys are accepted; any other key is refused. `image` and `workdir` are required.

```json
{
  "image": "python:3.12-slim",
  "workdir": "/work",
  "runtime": "runc",
  "network": "my-network",
  "mounts": [{"source": "/srv/data", "target": "/data", "read_only": true}],
  "env": {"NAME": "value"},
  "user": "10000:10000",
  "cpus": 1.0,
  "memory_mb": 1536,
  "pids_limit": 512,
  "skills_path": "/work/skills"
}
```

| Key | Rule |
|---|---|
| `image` | An image reference without whitespace or control characters, not starting with `-`. |
| `workdir` | Absolute path. Also the first working directory of a session. |
| `runtime` | `runc` (default), `runsc` (gVisor) or `kata`. The runtime must be installed on the Docker host. |
| `network` | An existing Docker network name, or `"none"` for no network at all. Without it the container uses Docker's default network. |
| `mounts` | List of `{source, target, read_only}`. `source` and `target` are absolute paths without commas; `read_only` defaults to false. Bind mounts. |
| `env` | Names matching `[A-Za-z_][A-Za-z0-9_]*`; string values without line breaks or NUL. |
| `user` | `uid` or `uid:gid` (names allowed). |
| `cpus` | Finite number above 0. |
| `memory_mb`, `pids_limit` | Whole numbers of at least 1. |
| `skills_path` | Absolute path inside the container where skills live (see below). |

When `KEEL_SANDBOX_MOUNT_ROOT` is set, each mount `source` must equal that root or be under it.

## Image requirements

The image must provide `sh`, `bash`, `setsid`, `base64`, `head`, `find`, `grep`, `tr`, and `python3` (for
`execute_code`). The container runs `sleep infinity`, so the image needs `sleep` too. Debian-based images such as
`python:3.12-slim` have all of these.

`rg` (ripgrep) is optional. With it, `search_files` is faster and also skips files ignored by `.gitignore`; without
it, `search_files` uses `grep` and `find`, which skip only hidden directories.

## Hardening

Every container is started with:

- `--init`, all capabilities dropped except `DAC_OVERRIDE`, `CHOWN` and `FOWNER`, and `no-new-privileges`;
- tmpfs mounts on `/tmp` (512 MB), `/var/tmp` and `/run`, all `nosuid`;
- the policy's runtime, network, user, CPU, memory and PID limits and mounts;
- labels `keel.managed=1`, `keel.deployment`, `keel.profile` and `keel.policy` (a digest of the policy), and a name
  `keel-sbx-<hash>`.

Policy `env` values reach the container through a private temporary env file, never on a command line, and never
change the docker client's own environment.

One container serves a profile and is reused across runs and sessions; if the policy changes, a new container
replaces it once no command is running in the old one. A container idle for `KEEL_SANDBOX_IDLE_SECONDS` is removed.

## Leftovers and lost workers

At start, keel removes this deployment's containers (those labelled with its `KEEL_DEPLOYMENT`), except those of
profiles with a run queued, running or stopping on any instance, so a restart does not pull a container from under
a run another instance is executing. A removed container is created again on its next use.

When a worker is lost (its instance crashed or stalled and the run's lease lapsed), the run fails as `worker_lost`
and the command it left running is killed: keel reaches the profile's container (creating it if needed) and kills
the command's whole process group.

## The Docker socket

The service starts containers by running the docker CLI against the Docker daemon. Giving it the Docker socket is
**root-equivalent on the host**: anyone who can make the service run arbitrary docker commands controls the host.
Mitigate by protecting `KEEL_API_KEY`, limiting host mounts with `KEEL_SANDBOX_MOUNT_ROOT`, and running the daemon
rootless or on a dedicated host where that is an option.

The image runs as uid 10001, so a deployment must also give it the socket's group (compose
`group_add: ["<socket gid>"]`, or `docker run --group-add <gid>`). The dev compose file runs as root only because
Docker Desktop's socket is root-only.

## Working directory and commands

- Each session remembers its working directory. If it no longer exists, the next `terminal` or `execute_code`
  call runs in the policy `workdir`, says so in its output, and the session's directory is reset to it.
- `terminal` defaults to a 180 s timeout (max 600 s); `execute_code` has a 300 s timeout. Output over 50,000
  characters is shown head and tail, and the full text is saved to a file in the sandbox.
- Stopping a run kills the command's whole process group.

## Skills

When the policy has `skills_path` and the profile lists skills (`"skills": ["name", …]`), keel looks for
`SKILL.md` files up to three levels below `skills_path`. A skill is a directory containing `SKILL.md`; its name is
`name:` in the front matter, or the directory name. A skill one level deeper is grouped under its parent
directory's name as a category. Only the profile's enabled skills are used. The first 8 KiB of each `SKILL.md`
gives the name and `description:`, which go into the system prompt as an index (descriptions cut to 60
characters). The model reads a full skill with `skill_view`, and its linked files with `skill_view` and
`file_path`. The list is cached for 60 s per profile and policy.
