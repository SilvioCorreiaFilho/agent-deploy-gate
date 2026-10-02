"""Log de auditoria do gate: uma linha JSON por evento, mais os snapshots em disco.

Por que existe: quando o deploy falha às 3h da manhã, a primeira pergunta é "o que
estava no ar antes?". Se a resposta não estiver gravada, ela não existe.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                                   # evita import circular em runtime
    from .gate import DeployResult


class AuditLog:
    """Grava eventos em JSON Lines e guarda snapshots, podando os mais antigos.

    `path` é o arquivo .jsonl. Os snapshots vão para `<path>.snapshots/`.
    """

    def __init__(self, path: str | Path = 'deploy-gate.jsonl', keep_snapshots: int = 5):
        self.path = Path(path)
        self.keep_snapshots = keep_snapshots
        self.snap_dir = self.path.with_name(self.path.name + '.snapshots')

    # ------------------------------------------------------------------ interno
    def _append(self, evento: dict) -> None:
        evento['ts'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(evento, ensure_ascii=False) + '\n')

    # ------------------------------------------------------------------ eventos
    def snapshot(self, name: str, content: bytes, sha: str) -> Path:
        """Preserva a versão anterior em disco, com timestamp no nome."""
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime('%Y%m%d-%H%M%S')
        destino = self.snap_dir / f'{Path(name).name}.{ts}.{sha[:10]}.bak'
        destino.write_bytes(content)
        self._append({'evento': 'snapshot', 'arquivo': name, 'sha256': sha,
                      'bytes': len(content), 'backup': str(destino)})
        self._podar()
        return destino

    def write(self, result: 'DeployResult') -> None:
        """Grava o desfecho de um deploy. Chamado SEMPRE, inclusive em falha."""
        self._append({
            'evento': 'deploy',
            'arquivo': result.name,
            'ok': result.ok,
            'sha256': result.sha256,
            'snapshot_sha256': result.snapshot_sha256,
            'aplicado': result.applied,
            'rollback': result.rolled_back,
            'erro': result.error or None,
            'invariantes': [{'nome': v.name, 'ok': v.ok, 'detalhe': v.detail}
                            for v in result.invariants],
            'verify': [{'nome': v.name, 'ok': v.ok, 'detalhe': v.detail}
                       for v in result.verify],
        })

    # ------------------------------------------------------------------ retenção
    def _podar(self) -> None:
        """Mantém só os N snapshots mais recentes — senão o disco enche em silêncio."""
        if self.keep_snapshots <= 0 or not self.snap_dir.exists():
            return
        arquivos = sorted(self.snap_dir.glob('*.bak'), key=lambda p: p.stat().st_mtime, reverse=True)
        for antigo in arquivos[self.keep_snapshots:]:
            antigo.unlink(missing_ok=True)

    def historico(self, nome: str | None = None) -> list[dict]:
        """Lê o log de volta. Útil em teste e em diagnóstico."""
        if not self.path.exists():
            return []
        linhas = []
        for linha in self.path.read_text(encoding='utf-8').splitlines():
            if not linha.strip():
                continue
            try:
                ev = json.loads(linha)
            except json.JSONDecodeError:
                continue
            if nome is None or ev.get('arquivo') == nome:
                linhas.append(ev)
        return linhas