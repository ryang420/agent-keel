# Security policy

## Supported versions

keel is pre-1.0. Only the latest release receives security fixes.

## Reporting a vulnerability

Please report privately through GitHub: open the
[Security Advisories page](https://github.com/ryang420/agent-keel/security/advisories/new) of this repository and
choose "Report a vulnerability". Do not open a public issue. Include the version, a description, and steps to
reproduce. You can expect a first reply within a few days.

## Threat model in brief

keel runs model-written code, so operate it with that in mind. The details are in the
[Sandbox guide](docs/guide/sandbox.md).

- **The Docker socket is root-equivalent.** The service starts sandboxes by running the docker CLI against the
  daemon. Whoever controls the service, or holds `KEEL_API_KEY`, can control the host. Protect the key, and prefer a
  rootless daemon or a dedicated host.
- **Sandboxes run untrusted code.** For stronger isolation use a `runsc` (gVisor) or `kata` runtime in the profile
  policy. Containers drop capabilities and set `no-new-privileges` by default.
- **Limit host mounts.** Set `KEEL_SANDBOX_MOUNT_ROOT` so every mount in a profile policy must be under one path.
- **Network.** Put sandboxes on a private network, or on none, unless a profile needs more.
- **Service key.** Every endpoint except `/healthz` needs `Authorization: Bearer <KEEL_API_KEY>`. Use a long random
  key and send it only over TLS; keel does not terminate TLS itself.
- **Secrets in sandbox env.** Policy `env` values reach the container through a private temporary env file, not a
  command line, but the model-written code in that container can read them. Give a profile only what it may use.
  Tool previews and results redact common secret shapes as a best effort, not a guarantee.

Issues that only matter when an operator ignores the guidance above (for example an exposed Docker socket) are
hardening requests rather than vulnerabilities, but we are glad to hear them.
