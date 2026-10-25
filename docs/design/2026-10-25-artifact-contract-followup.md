# Artifact submission contract and offline diagnosis

Date: 2026-10-25. Scope: the owner's approved first three follow-up steps.

Keep strict fixed-file submission rather than ignoring caches or accepting arbitrary subdirectories. Ignoring
incidental files could simplify submissions but would change the accepted output surface; repairing candidate
code would erase the distinction between model delivery and evaluator intervention. Instead make the existing
requirements explicit, provide useful rejection evidence, and diagnose recovered code separately.

Version the prompt change as `artifact-correctness-v2`, prepared under a new root. Every task states its allowed
filenames, required final development files (including structured error outputs), top-level placement, and removal
of caches, test outputs and temporary directories. Explain that `py_compile` creates `__pycache__`; final submission
must remove it. State that only `--input-dir` and `--output-dir` should determine data/result locations.

Export remains no-follow and bounded (1 MiB/file, 10 MiB total). Reject unexpected names, directories, links and
special files. Return structured issue codes, bounded entry names and entry types through the existing coordinated
helper; persist these details in execution records and reports. Never surface arbitrary helper tracebacks.

Recover four existing candidate scripts from successful write_file/patch operations in the original ordered tool
transcripts. A failed patch makes no edit. Reject ambiguous/unpaired logs; include transcript hash, recovered script
hash, tool message references and the limits of reconstruction. Manually audit terminal commands for additional
source mutations. Never execute or import candidate code on the host.

Replay recovered scripts unchanged against development and both hidden scenarios through the existing no-model
executor. Verify raw-input and gold hashes against the original v1 run configuration first. Use the original pinned
evaluation image; no credentials or live provider are needed. Independent tasks may overlap, while each task's
stateful steps remain ordered. Results are diagnostic only: source submission layout has been bypassed, not fixed.
Do not overwrite, rescore, or relabel the original baseline. Reading a historical report retains its stored protocol.

This follow-up changes evaluation code and documentation only. It does not change Keel runtime behavior, task data,
checkers, DAB scoring, or call a model. A later v2 live run requires a separate spending decision.
