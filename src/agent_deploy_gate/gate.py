"""Núcleo do gate: transportes, invariantes e a sequência snapshot→apply→verify→rollback."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .audit import AuditLog


# --------------------------------------------------------------------------- transportes
class Transport(Protocol):
    """Como o gate lê e escreve o artefato. Troque isto para mudar o destino
    (diretório local, SFTP, container, bucket) sem tocar na lógica do gate."""

    def read(self, name: str) -> bytes | None: ...
    def write(self, name: str, content: bytes) -> None: ...
    def remove(self, name: str) -> None: ...


class LocalDirTransport:
    """Destino é um diretório local. Usado nos testes e em deploy de arquivo local."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def read(self, name: str) -> bytes | None:
        p = self.root / name
        return p.read_bytes() if p.exists() else None

    def write(self, name: str, content: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / name
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_bytes(content)      # escrita atômica: nunca deixa arquivo pela metade
        tmp.replace(p)

    def remove(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)


class CommandTransport:
    """Destino atrás de um comando de shell — scp, ssh, docker cp, rsync.

    `read_cmd` e `write_cmd` recebem o nome do arquivo e devolvem a lista de argv.
    Nada é passado por shell, então nome de arquivo com espaço não vira injeção.
    """

    def __init__(
        self,
        read_cmd: Callable[[str], Sequence[str]],
        write_cmd: Callable[[str], Sequence[str]],
        timeout: int = 300,
    ):
        self.read_cmd, self.write_cmd, self.timeout = read_cmd, write_cmd, timeout

    def read(self, name: str) -> bytes | None:
        r = subprocess.run(list(self.read_cmd(name)), capture_output=True, timeout=self.timeout)
        return r.stdout if r.returncode == 0 and r.stdout else None

    def write(self, name: str, content: bytes) -> None:
        r = subprocess.run(list(self.write_cmd(name)), input=content,
                           capture_output=True, timeout=self.timeout)
        if r.returncode != 0:
            raise OSError(f'write falhou ({r.returncode}): {r.stderr.decode()[:300]}')


# --------------------------------------------------------------------------- invariantes
@dataclass(frozen=True)
class Invariant:
    """Uma condição que o artefato PRECISA satisfazer antes de ser aplicado.

    `check` recebe os bytes e devolve True/False. `fatal=True` aborta o deploy;
    `fatal=False` só registra um aviso.
    """

    name: str
    check: Callable[[bytes], bool]
    fatal: bool = True
    detail: str = ''

    def evaluate(self, content: bytes) -> bool:
        try:
            return bool(self.check(content))
        except Exception:
            return False


def min_size(n: int) -> Invariant:
    """Guarda contra o modo de falha mais comum: subir arquivo vazio ou truncado."""
    return Invariant(f'min_size>={n}', lambda c: len(c) >= n, detail=f'{n} bytes no mínimo')


def contains(*needles: bytes) -> Invariant:
    """Guarda contra subir o arquivo errado (ex.: um placeholder)."""
    return Invariant(f'contains{needles}',
                     lambda c: all(x in c for x in needles),
                     detail='precisa conter ' + ', '.join(repr(x) for x in needles))


def not_contains(*needles: bytes) -> Invariant:
    """Guarda de segredo: aborta se o artefato carrega credencial."""
    return Invariant(f'not_contains{needles}',
                     lambda c: not any(x in c for x in needles),
                     detail='não pode conter ' + ', '.join(repr(x) for x in needles))


class InvariantViolation(Exception):
    """Levantada quando um invariante fatal não passa. O deploy NÃO acontece."""


# --------------------------------------------------------------------------- verificação
@dataclass
class VerifyResult:
    """Resultado de um degrau de verificação. `ok=False` dispara rollback."""

    name: str
    ok: bool
    detail: str = ''


# --------------------------------------------------------------------------- resultado
@dataclass
class DeployResult:
    ok: bool
    name: str
    sha256: str
    applied: bool = False
    rolled_back: bool = False
    snapshot_sha256: str | None = None
    invariants: list[VerifyResult] = field(default_factory=list)
    verify: list[VerifyResult] = field(default_factory=list)
    error: str = ''

    @property
    def summary(self) -> str:
        if self.ok:
            return f'OK  {self.name} ({self.sha256[:10]})'
        if self.rolled_back:
            return f'ROLLBACK  {self.name} — {self.error}'
        return f'ABORTADO  {self.name} — {self.error}'


# --------------------------------------------------------------------------- o gate
class DeployGate:
    """Sequência determinística, nesta ordem:

      1. invariantes no artefato  — antes de tocar em qualquer coisa
      2. snapshot do que está no ar (se já existe algo)
      3. apply
      4. assert pós-apply         — o que chegou é o que saiu?
      5. verify em degraus        — a função que você passar
      6. rollback                 — se 4 ou 5 falharem, volta o snapshot
      7. log de auditoria         — sempre, inclusive quando falha
    """

    def __init__(
        self,
        transport: Transport,
        invariants: Sequence[Invariant] = (),
        audit: AuditLog | None = None,
        keep_snapshots: int = 5,
    ):
        self.transport = transport
        self.invariants = list(invariants)
        self.audit = audit or AuditLog()
        self.keep_snapshots = keep_snapshots

    # ------------------------------------------------------------------ utilidades
    @staticmethod
    def _sha(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _snapshot(self, name: str) -> bytes | None:
        return self.transport.read(name)

    # ------------------------------------------------------------------ principal
    def deploy(
        self,
        name: str,
        content: bytes,
        verify: Callable[[], Sequence[VerifyResult]] | None = None,
        apply: bool = True,
    ) -> DeployResult:
        sha = self._sha(content)
        result = DeployResult(ok=False, name=name, sha256=sha)

        # 1. invariantes — nada é tocado se algum fatal falhar
        for inv in self.invariants:
            passed = inv.evaluate(content)
            result.invariants.append(VerifyResult(inv.name, passed, inv.detail))
            if not passed and inv.fatal:
                result.error = f'invariante fatal não passou: {inv.name} ({inv.detail})'
                self.audit.write(result)
                return result

        # 2. snapshot
        anterior = self._snapshot(name)
        if anterior is not None:
            result.snapshot_sha256 = self._sha(anterior)
            self.audit.snapshot(name, anterior, result.snapshot_sha256)

        if not apply:
            result.ok = True
            result.error = 'dry-run: nada foi aplicado'
            self.audit.write(result)
            return result

        # 3. apply
        try:
            self.transport.write(name, content)
        except Exception as e:
            result.error = f'apply falhou: {type(e).__name__}: {e}'
            self._rollback(name, anterior, result)
            self.audit.write(result)
            return result
        result.applied = True

        # 4. assert pós-apply: o que chegou é o que saiu?
        no_ar = self.transport.read(name)
        if no_ar is None or self._sha(no_ar) != sha:
            result.error = 'assert pós-apply falhou: o artefato no destino não bate com o local'
            self._rollback(name, anterior, result)
            self.audit.write(result)
            return result

        # 5. verify em degraus
        if verify is not None:
            try:
                degraus = list(verify())
            except Exception as e:
                degraus = [VerifyResult('verify', False, f'{type(e).__name__}: {e}')]
            result.verify = degraus
            ruins = [d for d in degraus if not d.ok]
            if ruins:
                result.error = 'verificação falhou: ' + '; '.join(f'{d.name} ({d.detail})' for d in ruins)
                self._rollback(name, anterior, result)
                self.audit.write(result)
                return result

        result.ok = True
        self.audit.write(result)
        return result

    # ------------------------------------------------------------------ rollback
    def _rollback(self, name: str, anterior: bytes | None, result: DeployResult) -> None:
        """Volta o estado anterior E CONFERE que voltou.

        Se não havia anterior, remove o que subimos — deixar no ar um artefato que não
        passou na verificação é pior do que não ter nada. E o resultado da restauração é
        verificado: um rollback que falha em silêncio é pior que não ter rollback, porque
        dá a impressão de que o estado anterior está de pé.
        """
        try:
            if anterior is not None:
                self.transport.write(name, anterior)
                no_ar = self.transport.read(name)
                if no_ar is not None and self._sha(no_ar) == self._sha(anterior):
                    result.rolled_back = True
                else:
                    result.rolled_back = False
                    result.error += ' | ROLLBACK NÃO CONFIRMADO: o conteúdo restaurado não bate com o snapshot'
            else:
                remover = getattr(self.transport, 'remove', None)
                if not callable(remover):
                    result.rolled_back = False
                    result.error += (' | ROLLBACK IMPOSSÍVEL: o transporte não sabe remover e não havia '
                                     'versão anterior — o artefato NÃO VERIFICADO ficou no destino')
                    return
                remover(name)
                result.rolled_back = self.transport.read(name) is None
                if not result.rolled_back:
                    result.error += ' | ROLLBACK NÃO CONFIRMADO: o artefato continua no destino'
        except Exception as e:
            result.rolled_back = False
            result.error += f' | ROLLBACK TAMBÉM FALHOU: {type(e).__name__}: {e}'