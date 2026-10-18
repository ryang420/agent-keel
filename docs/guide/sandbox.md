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

Bind mounts require `KEEL_SANDBOX_MOUNT_ROOT`. Each profile's sources must be under a dedicated directory whose
name is the SHA-256 hex digest of the keel profile ID, directly under that root. For example, an operator can
compute the directory with `python3 -c 'import hashlib; print(hashlib.sha256(b"p1").hexdigest())'`, create it, and
give only that profile's sandbox access to it. No profile can mount the common root or another profile's directory.
Sources within one policy cannot overlap each other, including a directory and its descendant.

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

One container serves a profile and is reused across runs and sessions. A policy can only change while the profile
has no run in progress and no sandbox usage left (otherwise `PUT` returns 409 `sandbox_busy`); the next run then
waits until a maintenance pass removes the old container and a new one is created. Passes rotate through 100
profile records at a time, so this takes about one minute per 200 sandbox records. A container idle for `KEEL_SANDBOX_IDLE_SECONDS`
is removed.

## Leftovers and lost workers

Each instance runs one sandbox coordinator, shared by its workers. An instance admits sandbox runs only after it has
classified every container of this deployment on the configured Docker daemon against the recorded sandbox state,
in batches of 100. At start it spends up to 30 seconds on this before its workers start; whatever is left continues
in the background, retried every 15 seconds, while sandbox runs stay queued. Once admission is open it repeats a
recovery pass and an idle pass about once a minute, each over at most 100 profiles, skipping profiles another
instance is working on. Startup never removes containers because of which profiles have runs in flight:
every removal is a recorded decision about one exact container ID. A container keel has no record of, or whose
labels conflict with the record, is kept and blocks new sandbox runs for that profile until it is resolved.

When a worker is lost (its instance crashed or stalled and the run's lease lapsed), the run fails as `worker_lost`
first; the sweep then hands its sandbox usage to recovery, which drains the generation while other runs of the
profile finish and then removes that exact container. No numeric process is signalled. The sweep never creates a
container.

Claims never wait for Docker. If the Docker daemon or the sandbox helper cannot be served (refused or hanging), the
service still starts within about 30 seconds and runs chat-only profiles. Runs of sandbox profiles stay queued (they
are never run without their tools) and can be stopped; admission is retried every 15 seconds and opens once Docker
answers and the inventory has been classified.

The daemon ID is read afresh for every ownership or absence decision. If it changes while an instance runs, that
instance closes sandbox admission (an error is logged) and never treats the old daemon's containers as gone;
restore the recorded daemon and restart the service.

## Coordinated recovery API

`SandboxCoordinator.reconcile_once()` reconciles persisted usage and lifecycle attempts, including stopped
containers and late results from retired creates. `recover_run(run_id)` performs the same guarded recovery for
one run; `reap_idle_once()` removes safely released idle generations. The service runs these passes (see above)
and the lease sweep calls `recover_run`. Each scan takes bounded batches (at most 100 profile candidates and 100
inventory observations), rotating through retained tombstones between passes.

Expired usage drains its generation while other valid users can finish. Recovery removes only recorded exact
container IDs and confirms absence before releasing bound orphan usage or replacing a generation. A never-bound
preparation with no authorized start or execution may release without Docker, retaining its create tombstone
indefinitely. Unknown or conflicting inventory blocks new admission for the affected profile while the recorded
exact-ID generation keeps its execution and cancellation authority. A conflict recorded on the published
container itself clears when that container is verified matching again; a conflict recorded on another container
clears when that exact ID is proven absent on the same daemon. A generation still draining for another reason
keeps draining when its conflict evidence clears. A daemon identity change is not resolved by inventory: it
requires restoring the recorded daemon configuration, and a new daemon never proves that the previous daemon's
containers are absent. Recovery reports count
unresolved create tombstones and their oldest age, with bounded diagnostic codes.

## The Docker socket

The service starts containers by running the docker CLI against the Docker daemon. Giving it the Docker socket is
**root-equivalent on the host**: anyone who can make the service run arbitrary docker commands controls the host.
Mitigate by protecting tenant-bound service keys, limiting host mounts with `KEEL_SANDBOX_MOUNT_ROOT`, and running
the daemon rootless or on a dedicated host where that is an option.

Host mount paths and tool credentials belong to one profile, including profiles in the same tenant.
`PUT /v1/profiles/{p}` rejects any mount outside that profile's directory or overlapping another profile's mount,
including read-only sources.
After a profile has mounts, its mount list cannot be changed through `PUT`; older live containers may still use
those mounts. Public skills can be packaged into a shared image. The mount source is checked again when creating
a container. Protect the common root and each profile directory from changes by other profiles or host users.

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
