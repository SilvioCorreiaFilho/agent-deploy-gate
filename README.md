# agent-deploy-gate

A deterministic deploy gate for AI-agent workloads: **snapshot → apply → assert → verify → rollback → audit**.

Agents change production. When something goes wrong at 3am, the first question is
*"what was running before?"* — and if that wasn't recorded, the answer doesn't exist.
This gate makes agent-driven deploys carry the guarantees we already expect from
payment code: a snapshot before the change, invariants that must hold, verification
after the change, and an automatic rollback when verification fails.

Standard library only. No dependencies, on purpose — this runs where you can't
install anything.

## The problem it solves

The naive agent deploy is `write(file, new_content)`. Three ways that hurts:

1. **It overwrites the only good copy.** There is no previous version to go back to.
2. **Nothing checks that the artifact is complete.** A truncated upload — the single
   most common failure — goes live silently.
3. **Failure is indistinguishable from success.** The agent reports "deployed" because
   the write call returned without raising.

This gate turns each of those into a checked step, and makes failure loud.

## Real run

Deploying a good version, then a bad one, then a secret check:

```console
$ agent-deploy-gate deploy --arquivo v2.conf --destino ./prod --nome app.conf \
    --min-size 5 --verify-cmd "grep -q 'versao 2' ./prod/app.conf"
  OK  app.conf (9970cea8dd)
    OK    grep -q 'versao 2' ./prod/app.conf  exit 0

$ cat ./prod/app.conf
versao 2 boa

$ agent-deploy-gate deploy --arquivo v3.conf --destino ./prod --nome app.conf \
    --min-size 5 --verify-cmd "grep -q 'versao 2' ./prod/app.conf"
  ROLLBACK  app.conf — verificação falhou: grep -q 'versao 2' ./prod/app.conf (exit 1)
    FALHA grep -q 'versao 2' ./prod/app.conf  exit 1

$ cat ./prod/app.conf          # the bad version is NOT live
versao 2 boa

$ agent-deploy-gate check --arquivo segredo.conf --not-contains API_KEY
  OK    min_size>=1  1 bytes no mínimo
  FALHA not_contains(b'API_KEY',)  não pode conter b'API_KEY'

1 invariante(s) fatal(is) não passaram — nada seria aplicado.
```

## The sequence

| # | Step | On failure |
|---|------|-----------|
| 1 | **Invariants** on the artifact — size, required markers, forbidden secrets | abort, nothing is touched |
| 2 | **Snapshot** the live version (bytes + sha256, kept on disk) | — |
| 3 | **Apply** (atomic write: temp file + rename) | rollback |
| 4 | **Assert post-apply** — is what arrived what we sent? | rollback |
| 5 | **Verify** — your function or `--verify-cmd` checks, in steps | rollback |
| 6 | **Rollback** — restore the snapshot, or remove the artifact if there was none | reported loudly |
| 7 | **Audit** — one JSON line per event, always, including failures | — |

Two details worth calling out, because both were bugs in the first version of this
code and are now covered by tests:

- **Rollback is verified, not assumed.** After restoring, the gate reads the target back
  and compares hashes. A rollback that fails silently is worse than no rollback — it
  leaves you believing the old state is up.
- **A failed first deploy removes the artifact.** If there was no previous version and
  verification fails, the unverified artifact is removed rather than left live.

## Install

```bash
pip install -e .            # or just copy src/agent_deploy_gate into your project
python -m pytest            # 15 tests, no network
```

## Library use

```python
from agent_deploy_gate import (
    AuditLog, CommandTransport, DeployGate, VerifyResult, min_size, contains, not_contains,
)

# any destination reachable by a command: scp, ssh, docker cp, rsync
gate = DeployGate(
    transport=CommandTransport(
        read_cmd=lambda n: ['ssh', 'host', f'cat /srv/{n}'],
        write_cmd=lambda n: ['ssh', 'host', f'tee /srv/{n}'],
    ),
    invariants=[min_size(500), contains(b'<?php'), not_contains(b'API_KEY')],
    audit=AuditLog('/var/log/deploy-gate.jsonl'),
)

def verify():
    # stepped verification: the shape used in production is
    # unauthenticated -> 401, wrong credential -> 401, correct -> 200 with expected payload
    return [VerifyResult('health endpoint', http_ok('/health'), 'expected 200')]

result = gate.deploy('api/endpoint.php', open('endpoint.php', 'rb').read(), verify=verify)
print(result.summary)
if not result.ok:
    raise SystemExit(result.error)
```

Writing your own transport is one class with `read`, `write`, `remove`.

## Design notes

- **Invariants run before anything is touched.** A gate that checks after applying isn't
  a gate, it's a post-mortem.
- **Atomic writes.** Temp file plus `replace`, so a partial write can never be observed.
- **Snapshots are pruned** (default: keep 5). Without a retention policy the disk fills
  silently and the snapshot you need has already been evicted.
- **`Invariant.check` raising an exception counts as failure**, not as a crash — an
  invariant is user code and user code breaks.
- **No shell interpolation.** `CommandTransport` takes argv lists, so a filename with a
  space is not an injection.

## Origin

Extracted from a deploy gate I run in production for agent workloads, where the same
sequence guards a read-only API endpoint and its credential: pre-flight lint, snapshot
of the previous version, apply, size assertion, then stepped HTTP verification
(unauthenticated → 401, wrong credential → 401, correct → 200 with the expected payload)
and a JSON audit line per deploy. Client-specific logic was removed; the pattern is
generic. The rollback and rollback-verification paths were added here — the original
aborted without rolling back.

## License

MIT.