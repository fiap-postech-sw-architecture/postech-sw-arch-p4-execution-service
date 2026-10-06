"""Registra no ``metadata`` as tabelas de todos os contextos (Alembic e testes).

Cada ``mapping.py`` mapeia o proprio agregado no import; a API nao precisa
deste modulo porque os repositorios ja importam os mapeamentos que usam.
"""

import src.compartilhado.infraestrutura.outbox_mapping
import src.diagnostico.infraestrutura.mapping
import src.estoque.infraestrutura.mapping
import src.execucao.infraestrutura.mapping  # noqa: F401
