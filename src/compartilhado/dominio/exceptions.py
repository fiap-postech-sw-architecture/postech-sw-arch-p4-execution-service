from __future__ import annotations


class ValorInvalidoError(ValueError):
    """Invariante de value object ou de agregado violada pela entrada (422).

    Classe propria, e nao ``ValueError`` puro, para a API devolver 422 so para
    dado invalido do chamador: um ``ValueError`` de biblioteca (ex.: validacao
    de resposta do Pydantic) e defeito do servidor e vira 500.
    """


class DomainException(Exception):
    def __init__(self, codigo: str, mensagem: str) -> None:
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(mensagem)


class EntidadeNaoEncontradaException(DomainException):
    def __init__(self, mensagem: str = "Entidade nao encontrada") -> None:
        super().__init__(codigo="ENTIDADE_NAO_ENCONTRADA", mensagem=mensagem)


class ViolacaoRegraDeNegocioException(DomainException):
    def __init__(self, mensagem: str = "Violacao de regra de negocio") -> None:
        super().__init__(codigo="VIOLACAO_REGRA_NEGOCIO", mensagem=mensagem)


class TransicaoStatusInvalidaException(DomainException):
    def __init__(self, mensagem: str = "Transicao de status invalida") -> None:
        super().__init__(codigo="TRANSICAO_STATUS_INVALIDA", mensagem=mensagem)


class EstoqueInsuficienteException(DomainException):
    def __init__(self, mensagem: str = "Estoque insuficiente") -> None:
        super().__init__(codigo="ESTOQUE_INSUFICIENTE", mensagem=mensagem)


class EntidadeDuplicadaException(DomainException):
    def __init__(self, mensagem: str = "Entidade duplicada") -> None:
        super().__init__(codigo="ENTIDADE_DUPLICADA", mensagem=mensagem)


class OperacaoNaoPermitidaException(DomainException):
    """O usuario autenticado nao e o responsavel pelo objeto (ex.: outro mecanico)."""

    def __init__(self, mensagem: str = "Operacao nao permitida") -> None:
        super().__init__(codigo="OPERACAO_NAO_PERMITIDA", mensagem=mensagem)


class DadosInvalidosException(DomainException):
    """Entrada bem formada, mas recusada por regra que depende de dado externo."""

    def __init__(self, mensagem: str, codigo: str = "DADOS_INVALIDOS") -> None:
        super().__init__(codigo=codigo, mensagem=mensagem)


class DependenciaIndisponivelException(DomainException):
    """Servico externo necessario ao caso de uso nao respondeu a tempo.

    ``retry_after``: segundos ate valer a pena tentar de novo (ex.: o circuito
    aberto), quando conhecidos; a API os devolve no header ``Retry-After``.
    """

    def __init__(self, mensagem: str, *, retry_after: int | None = None) -> None:
        super().__init__(codigo="DEPENDENCIA_INDISPONIVEL", mensagem=mensagem)
        self.retry_after = retry_after


class RespostaInvalidaDaDependenciaException(DomainException):
    """O servico externo respondeu, mas recusou o pedido (4xx) ou saiu do contrato.

    Diferente da indisponibilidade (503), repetir nao muda o resultado: a API
    responde 502 e quem opera confere token, contrato ou configuracao.
    """

    def __init__(self, mensagem: str) -> None:
        super().__init__(codigo="RESPOSTA_INVALIDA_DA_DEPENDENCIA", mensagem=mensagem)
