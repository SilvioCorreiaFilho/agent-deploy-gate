"""Testes do gate. O foco é o caminho de FALHA: um gate que só funciona no caminho
feliz não é um gate, é um `cp` com mais passos."""
import json

import pytest

from agent_deploy_gate import (
    AuditLog, DeployGate, Invariant, LocalDirTransport, VerifyResult, min_size, contains,
)
from agent_deploy_gate.gate import not_contains


@pytest.fixture
def destino(tmp_path):
    d = tmp_path / 'prod'
    d.mkdir()
    return d


@pytest.fixture
def log(tmp_path):
    return AuditLog(tmp_path / 'audit.jsonl')


def gate(destino, log, invs=()):
    return DeployGate(transport=LocalDirTransport(destino), invariants=list(invs), audit=log)


# ------------------------------------------------------------------ invariantes
def test_invariante_fatal_aborta_sem_tocar_no_destino(destino, log):
    (destino / 'x.php').write_bytes(b'versao antiga que nao pode ser perdida')
    g = gate(destino, log, [min_size(1000)])

    r = g.deploy('x.php', b'curto')

    assert not r.ok and not r.applied
    assert 'invariante fatal' in r.error
    # o ponto: o arquivo em produção continua intacto
    assert (destino / 'x.php').read_bytes() == b'versao antiga que nao pode ser perdida'


def test_invariante_nao_fatal_avisa_mas_deixa_passar(destino, log):
    g = gate(destino, log, [Invariant('aviso', lambda c: False, fatal=False, detail='só um aviso')])
    r = g.deploy('x.txt', b'conteudo valido')
    assert r.ok
    assert [v.ok for v in r.invariants] == [False]


def test_invariante_que_explode_nao_derruba_o_gate(destino, log):
    """Invariante é código do usuário: se ele levantar exceção, o gate trata como
    falha do invariante, não como crash do deploy."""
    g = gate(destino, log, [Invariant('boom', lambda c: 1 / 0)])
    r = g.deploy('x.txt', b'oi')
    assert not r.ok and 'boom' in r.error


# ------------------------------------------------------------------ caminho feliz
def test_deploy_ok_aplica_verifica_e_registra(destino, log):
    g = gate(destino, log, [min_size(3), contains(b'ok')])
    r = g.deploy('x.txt', b'conteudo ok', verify=lambda: [VerifyResult('http 200', True)])

    assert r.ok and r.applied and not r.rolled_back
    assert (destino / 'x.txt').read_bytes() == b'conteudo ok'
    hist = log.historico('x.txt')
    assert hist[-1]['ok'] is True and hist[-1]['verify'][0]['nome'] == 'http 200'


def test_dry_run_nao_aplica(destino, log):
    g = gate(destino, log)
    r = g.deploy('x.txt', b'nada', apply=False)
    assert r.ok and not r.applied
    assert not (destino / 'x.txt').exists()


# ------------------------------------------------------------------ rollback (o que importa)
def test_verify_falha_dispara_rollback_para_a_versao_anterior(destino, log):
    (destino / 'app.php').write_bytes(b'VERSAO BOA EM PRODUCAO')
    g = gate(destino, log)

    r = g.deploy('app.php', b'VERSAO NOVA QUEBRADA',
                 verify=lambda: [VerifyResult('sem token -> 401', False, 'obtido 200')])

    assert not r.ok and r.rolled_back
    assert (destino / 'app.php').read_bytes() == b'VERSAO BOA EM PRODUCAO'
    assert log.historico('app.php')[-1]['rollback'] is True


def test_verify_falha_sem_versao_anterior_remove_o_artefato(destino, log):
    """Primeira publicação que não passa na verificação: melhor não deixar nada
    no ar do que deixar algo não verificado."""
    g = gate(destino, log)
    r = g.deploy('novo.txt', b'primeira vez',
                 verify=lambda: [VerifyResult('health', False, 'sem resposta')])

    assert not r.ok and r.rolled_back
    assert not (destino / 'novo.txt').exists()


def test_verify_que_levanta_excecao_conta_como_falha(destino, log):
    (destino / 'a.txt').write_bytes(b'anterior')
    g = gate(destino, log)

    def verify_ruim():
        raise ConnectionError('servidor não respondeu')

    r = g.deploy('a.txt', b'novo', verify=verify_ruim)
    assert not r.ok and r.rolled_back
    assert (destino / 'a.txt').read_bytes() == b'anterior'


def test_assert_pos_apply_pega_transporte_mentiroso(destino, log):
    """Transporte que aceita a escrita mas grava truncado (proxy que come corpo,
    encoding errado). O gate compara o que está no destino com o que ele enviou.
    Aqui a mentira é transitória: só a primeira escrita falha, então o rollback
    consegue restaurar de verdade."""

    class TransporteMentiroso(LocalDirTransport):
        def __init__(self, root):
            super().__init__(root)
            self.mentiu = False

        def write(self, name, content):
            if not self.mentiu:
                self.mentiu = True
                super().write(name, content[:4])      # grava truncado, uma vez só
            else:
                super().write(name, content)

    (destino / 'a.txt').write_bytes(b'original')
    g = DeployGate(transport=TransporteMentiroso(destino), audit=log)

    r = g.deploy('a.txt', b'conteudo completo que deveria chegar inteiro')
    assert not r.ok and r.rolled_back
    assert 'assert pós-apply falhou' in r.error
    assert (destino / 'a.txt').read_bytes() == b'original'


def test_rollback_nao_confirmado_e_reportado_em_voz_alta(destino, log):
    """Se o transporte mente SEMPRE, a restauração também sai errada. O gate não pode
    dizer 'voltei ao estado anterior' sem conferir — rollback que falha em silêncio é
    pior que não ter rollback."""

    class SempreMente(LocalDirTransport):
        def write(self, name, content):
            super().write(name, content[:3])

    (destino / 'a.txt').write_bytes(b'versao boa em producao')
    g = DeployGate(transport=SempreMente(destino), audit=log)

    r = g.deploy('a.txt', b'versao nova')
    assert not r.ok
    assert r.rolled_back is False
    assert 'ROLLBACK NÃO CONFIRMADO' in r.error


def test_transporte_sem_remove_reporta_rollback_impossivel(destino, log):
    """Transporte que não sabe remover + primeira publicação = o artefato não
    verificado fica no ar. O gate precisa DIZER isso, não fingir sucesso."""

    class SoEscrita(LocalDirTransport):
        def remove(self, name):
            raise NotImplementedError('este transporte não remove')

    g = DeployGate(transport=SoEscrita(destino), audit=log)
    r = g.deploy('novo.txt', b'primeira vez',
                 verify=lambda: [VerifyResult('health', False, 'sem resposta')])

    assert not r.ok and r.rolled_back is False
    assert 'ROLLBACK TAMBÉM FALHOU' in r.error or 'ROLLBACK IMPOSSÍVEL' in r.error


# ------------------------------------------------------------------ snapshot e auditoria
def test_snapshot_guardado_em_disco_com_hash(destino, log):
    (destino / 'a.txt').write_bytes(b'versao um')
    g = gate(destino, log)
    g.deploy('a.txt', b'versao dois')

    backups = list(log.snap_dir.glob('*.bak'))
    assert len(backups) == 1 and backups[0].read_bytes() == b'versao um'
    assert log.historico('a.txt')[0]['evento'] == 'snapshot'


def test_retencao_poda_snapshots_antigos(destino, tmp_path):
    log = AuditLog(tmp_path / 'audit.jsonl', keep_snapshots=2)
    g = DeployGate(transport=LocalDirTransport(destino), audit=log)
    for i in range(5):
        g.deploy('a.txt', f'versao {i}'.encode())
    assert len(list(log.snap_dir.glob('*.bak'))) == 2


def test_log_registra_falha_tambem(destino, log):
    g = gate(destino, log, [min_size(9999)])
    g.deploy('a.txt', b'curto')
    evento = log.historico('a.txt')[-1]
    assert evento['ok'] is False and evento['erro']
    assert json.dumps(evento)          # serializável


def test_invariante_de_segredo_aborta_antes_de_publicar(destino, log):
    g = gate(destino, log, [not_contains(b'API_KEY', b'password')])
    r = g.deploy('config.py', b'API_KEY = "sk-vazou"')
    assert not r.ok and not (destino / 'config.py').exists()