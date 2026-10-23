# Mutação

Detalhe de [Mutação](../README.md#mutação), em Como testar, no README.

Cobertura diz que a linha rodou; mutação diz se a suíte percebe quando ela está errada (ADR-041). Não é gate de CI e não tem pontuação mínima.

O `mutmut` não roda em pacote chamado `src`, o caso deste serviço: a versão 3.8.0 espera o pacote em `src/<pacote>` e tira o prefixo `src.` do nome do módulo, e a primeira função mutada que os testes chamam reprova com

```text
AssertionError: Failed trampoline hit. Module name starts with `src.`, which is invalid
```

(`mutmut/stats.py`, `record_trampoline_hit`; visto no Billing, que tem o mesmo layout). Renomear o pacote ou remendar a ferramenta mudaria o que se mede, então a alternativa são mutantes escritos à mão: [`scripts/mutantes.toml`](../scripts/mutantes.toml) lista 98 defeitos plausíveis nos módulos críticos, e [`scripts/mutacao_manual.py`](../scripts/mutacao_manual.py), o mesmo script do Billing, aplica um por vez numa cópia do código (`mutants/`, fora do git) e roda só os testes que deveriam pegá-lo.

**Resultado** em 23/10/2026: 97 de 97 mutantes mortos (100,0%), com 1 equivalente de fora da conta.

| Área | Mutantes | Mortos | Equivalentes |
|---|---:|---:|---:|
| Status da execução | 9 | 9 | 0 |
| Status do diagnóstico | 10 | 10 | 0 |
| Reserva e saldo de peça | 15 | 15 | 0 |
| Saga (guardas, lápide e repetição do comando) | 10 | 10 | 0 |
| Idempotência e concorrência | 6 | 6 | 0 |
| Placa do veículo | 8 | 8 | 0 |
| Validação do JWT | 14 | 13 | 1 |
| Consumidor | 14 | 14 | 0 |
| Relay e outbox | 12 | 12 | 0 |
| **total** | **98** | **97** | **1** |

- **Limites que só a mutação mostrou, hoje com teste:**
  - corpo de 64 KiB cravados tratado como grande demais (`test_corpo_no_teto_exato_passa_do_limite_e_chega_ao_parse`);
  - recusa permanente do domínio que, sem o `ValorInvalidoError` em `_PERMANENTES`, ia para a DLQ do mesmo jeito, mas com o traceback e a mensagem do erro no log (`test_recusa_permanente_loga_so_a_classe_do_erro`);
  - JWK Set ainda fresco na hora exata dos 10 min e cópia velha valendo na hora exata de 1 h (`test_jwk_set_fica_fresco_10_minutos_e_renova_na_hora_exata` e `test_copia_velha_vale_ate_1_hora_e_nao_na_hora_exata`, que prendem as duas constantes);
  - memória da falha do JWKS diferente de 5 s e `Retry-After` do 503 arredondado para baixo (`test_falha_memorizada_responde_na_hora_sem_buscar_de_novo`, que prende a constante e espera 2,5 s);
  - placa aceita com um dígito a mais ou a menos, nos dois formatos, porque o regex casava só o começo ou um formato mais curto (casos em `test_placa_invalida_sem_ecoar_o_valor`);
  - execução iniciada fora de "em andamento", que dispara o aviso da eliminação LGPD com registro aberto (`test_em_andamento_vale_na_fila_e_em_execucao`);
  - janela de retenção trocada por uma maior: 30 por 31 dias nas mensagens processadas (C14) e nas `dead` da outbox (O11), 7 por 8 nas entregues (O12). A linha um dia além do limite (31 ou 8 dias) sai também com a janela maior, porque a limpeza roda alguns milissegundos depois da gravação. Só a de meio dia além (30,5 ou 7,5 dias) separa as duas janelas, e a de meio dia aquém (29,5 ou 6,5 dias) tem de ficar (`test_processada_de_30_dias_e_meio_sai_e_a_de_29_e_meio_fica`, `test_dead_de_30_dias_e_meio_sai_e_a_de_29_e_meio_fica` e `test_entregue_de_7_dias_e_meio_sai_e_a_de_6_e_meio_fica`).
- **Equivalente:** `HS256` na lista de algoritmos do JWT (J01), e só enquanto valem duas condições: o PyJWT amarra a chave ao algoritmo dela (na 2.15, o `decode` com um `PyJWK` recusa `Token algorithm 'HS256' does not match the key's algorithm 'RS256'`) e o validador passa um `PyJWK` ao `decode`. Com elas, a lista é a segunda barreira, e `test_algoritmo_simetrico_e_recusado` guarda o desfecho; sem elas, a lista `RS256` do ADR-039 é a única barreira, e nenhum teste a prende. O motivo fica no campo `equivalente` do `mutantes.toml`.
- **O que o percentual não é:** a lista é curada, então ele diz que a suíte pega esses 98 defeitos, e não é comparável ao de uma ferramenta que gera os mutantes sozinha.

Para refazer:

```bash
make mutacao
make mutacao MUTANTES="E01 C03"
```

Como o alvo se comporta:

- Para de iniciar mutantes depois de `MUTACAO_PRAZO` segundos (900); os que sobram saem `nao rodado`. A tabela vai para `reports/mutacao.md`.
- Antes do primeiro mutante de cada grupo de testes, roda o grupo na cópia sem mutante. Se ele já reprova ou estoura o tempo, os mutantes do grupo saem `erro`: um teste que já falha mataria qualquer um.
- A cópia leva também o `alembic.ini`, as `migrations/` e os `contratos/` (`[config]` do `mutantes.toml`), que os testes de integração leem a partir da raiz dela.
- Em cada grupo o teste unitário vem antes do de integração: o mutante que o unitário mata não sobe container. O que chega à integração sobe um PostgreSQL, e na mensageria também um RabbitMQ. Com `TEST_DATABASE_URL` apontando para um banco `*_test` no ar, o PostgreSQL não sobe.
- O script sai com 2 se um id de `MUTANTES` não existir (a mensagem lista os válidos) ou se o `.toml` não bater com o código.
- O script sai com 1 se algum mutante sair `erro`, que é medida inválida, como o Docker fora. Sobrevivente não reprova, porque não há gate (ADR-041).
- São os códigos do script. Com falha, o `make mutacao` mostra `Error 1` ou `Error 2` e sai com 2 (o `make` troca o código do script pelo dele); sem falha, sai com 0.
- Com `erro` ou `nao rodado`, o percentual vira `n/d`.
- Um mutante novo entra com o trecho exato (`de`, uma ocorrência no arquivo), e `tests/unitarios/test_mutacao_manual.py` reprova se a lista envelhecer.
