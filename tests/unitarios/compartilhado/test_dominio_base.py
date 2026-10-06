from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

import pytest

from src.compartilhado.dominio.entity import Entity
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException
from src.compartilhado.dominio.maquina_de_estados import validar_transicao


@dataclass(eq=False)
class _Coisa(Entity):
    nome: str = ""


@dataclass(eq=False)
class _Outra(Entity):
    pass


class _Status(StrEnum):
    A = "A"
    B = "B"
    C = "C"


_TRANSICOES = {
    _Status.A: frozenset({_Status.B, _Status.C}),
    _Status.B: frozenset({_Status.C}),
    _Status.C: frozenset(),
}


class TestEntity:
    def test_identidade_nao_muda_depois_de_criada(self) -> None:
        coisa = _Coisa(nome="x")
        with pytest.raises(AttributeError, match="Identidade"):
            coisa.id = uuid4()

    def test_igualdade_e_hash_pela_identidade(self) -> None:
        identidade = uuid4()
        assert _Coisa(id=identidade, nome="a") == _Coisa(id=identidade, nome="b")
        assert hash(_Coisa(id=identidade)) == hash(identidade)
        uma, outra = _Coisa(), _Coisa()
        assert uma != outra

    def test_tipos_diferentes_com_mesmo_id_nao_sao_iguais(self) -> None:
        identidade = uuid4()
        assert _Coisa(id=identidade) != _Outra(id=identidade)

    def test_outros_atributos_continuam_mutaveis(self) -> None:
        coisa = _Coisa(nome="a")
        coisa.nome = "b"
        assert coisa.nome == "b"


class TestValidarTransicao:
    def test_transicao_permitida_passa(self) -> None:
        validar_transicao(_TRANSICOES, _Status.A, _Status.B, agregado="Coisa")

    def test_transicao_proibida_lista_as_validas(self) -> None:
        with pytest.raises(TransicaoStatusInvalidaException) as erro:
            validar_transicao(_TRANSICOES, _Status.B, _Status.A, agregado="Coisa")
        assert erro.value.mensagem == (
            "Coisa em B nao pode passar para A; transicoes validas: C"
        )
        assert erro.value.codigo == "TRANSICAO_STATUS_INVALIDA"

    def test_estado_final_nao_tem_saida(self) -> None:
        with pytest.raises(TransicaoStatusInvalidaException, match="estado final"):
            validar_transicao(_TRANSICOES, _Status.C, _Status.A, agregado="Coisa")
