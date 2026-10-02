"""CLI do gate.

    python -m agent_deploy_gate check  --arquivo x.php --min-size 500
    python -m agent_deploy_gate deploy --arquivo x.php --destino ./public --min-size 500

`check` só avalia invariantes (não toca em nada). `deploy` roda a sequência
completa. Exit code 0 = ok; 1 = invariante ou verificação falhou.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .audit import AuditLog
from .gate import DeployGate, LocalDirTransport, VerifyResult, min_size, contains, not_contains


def _monta_verify(comandos):
    """Cada comando vira um degrau de verificação. Exit 0 = passou.
    É assim que o gate real confere HTTP depois de subir (401 sem token, 200 com token)."""
    if not comandos:
        return None

    def verify():
        degraus = []
        for cmd in comandos:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            saida = (r.stdout or r.stderr or '').strip().replace('\n', ' ')[:160]
            degraus.append(VerifyResult(cmd, r.returncode == 0,
                                        saida if saida else f'exit {r.returncode}'))
        return degraus

    return verify


def _monta_invariantes(a) -> list:
    invs = []
    if a.min_size:
        invs.append(min_size(a.min_size))
    if a.contains:
        invs.append(contains(*[x.encode() for x in a.contains]))
    if a.not_contains:
        invs.append(not_contains(*[x.encode() for x in a.not_contains]))
    return invs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog='agent-deploy-gate',
                                description='Gate de deploy determinístico (snapshot → apply → verify → rollback).')
    sub = p.add_subparsers(dest='cmd', required=True)

    for nome, ajuda in (('check', 'avalia invariantes sem aplicar nada'),
                        ('deploy', 'aplica com verificação e rollback')):
        s = sub.add_parser(nome, help=ajuda)
        s.add_argument('--arquivo', required=True, help='artefato local a publicar')
        s.add_argument('--destino', default='.', help='diretório de destino (modo local)')
        s.add_argument('--nome', help='nome do arquivo no destino (padrão: nome local)')
        s.add_argument('--min-size', type=int, default=1, help='tamanho mínimo em bytes')
        s.add_argument('--contains', action='append', default=[],
                       help='exige que o conteúdo contenha este texto (repetível)')
        s.add_argument('--not-contains', action='append', default=[],
                       help='aborta se o conteúdo contiver este texto (repetível)')
        s.add_argument('--log', default='deploy-gate.jsonl', help='arquivo de auditoria')
        s.add_argument('--verify-cmd', action='append', default=[],
                       help='comando de verificação pós-deploy (exit 0 = passou; repetível). '
                            'Falha aqui dispara rollback.')
        s.add_argument('--json', action='store_true', help='saída legível por máquina')

    a = p.parse_args(argv)
    origem = Path(a.arquivo)
    if not origem.exists():
        print(f'erro: {origem} não existe', file=sys.stderr)
        return 1

    conteudo = origem.read_bytes()
    nome = a.nome or origem.name
    gate = DeployGate(
        transport=LocalDirTransport(a.destino),
        invariants=_monta_invariantes(a),
        audit=AuditLog(a.log),
    )

    if a.cmd == 'check':
        falhas = [i for i in gate.invariants if not i.evaluate(conteudo)]
        for i in gate.invariants:
            marca = 'OK   ' if i.evaluate(conteudo) else 'FALHA'
            print(f'  {marca} {i.name}  {i.detail}')
        if falhas:
            print(f'\n{len(falhas)} invariante(s) fatal(is) não passaram — nada seria aplicado.')
            return 1
        print(f'\n{len(conteudo)} bytes, todos os invariantes passaram.')
        return 0

    r = gate.deploy(nome, conteudo, verify=_monta_verify(a.verify_cmd), apply=True)
    if a.json:
        import json
        print(json.dumps({'ok': r.ok, 'arquivo': r.name, 'sha256': r.sha256,
                          'aplicado': r.applied, 'rollback': r.rolled_back,
                          'erro': r.error or None}, ensure_ascii=False))
    else:
        print(f'  {r.summary}')
        for v in r.verify:
            print(f'    {"OK   " if v.ok else "FALHA"} {v.name}  {v.detail}')
    return 0 if r.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())