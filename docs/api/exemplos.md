# API: rotas, papéis e exemplos

Detalhe da seção [API](../../README.md#api) do README. O OpenAPI e a coleção Postman, nesta pasta, saem do próprio app pelo `make docs-api`.

## Rotas e papéis

| Método e rota | Papéis | O que faz |
|---|---|---|
| `GET /api/v1/diagnosticos?status=` | mecânico, admin | Fila de diagnósticos por ordem de chegada |
| `POST /api/v1/diagnosticos/{ordem_id}/inicio` | mecânico, admin | Assume o diagnóstico |
| `POST /api/v1/diagnosticos/{ordem_id}/conclusao` | mecânico, admin | Registra `itens` (`servico`/`peca`, `codigo`, `quantidade`) e `observacoes` |
| `GET /api/v1/fila` | mecânico, atendente, admin | Fila de execução com a posição de cada ordem |
| `POST /api/v1/execucoes/{ordem_id}/inicio` | mecânico, admin | Inicia o reparo |
| `POST /api/v1/execucoes/{ordem_id}/finalizacao` | mecânico, admin | Finaliza e baixa as peças reservadas |
| `GET /api/v1/estoque`, `GET /api/v1/estoque/{sku}` | mecânico, atendente, admin | Consulta o estoque |
| `POST /api/v1/estoque`, `PUT /api/v1/estoque/{sku}`, `DELETE /api/v1/estoque/{sku}` | admin | Cadastro, nome/situação e desativação |
| `PATCH /api/v1/estoque/{sku}/quantidade` | admin | Ajuste do saldo físico (nunca abaixo do reservado) |
| `GET /api/v1/saude` | — | Liveness: processo de pé, sem tocar dependências (HEALTHCHECK da imagem) |
| `GET /api/v1/saude/pronto` | — | Readiness: 200 só com o banco respondendo `SELECT 1` em até 2 s, senão 503 |
| `GET /metrics` | — | Métricas Prometheus: `http_request_duration_seconds{method,rota,status}`, `pytstop_circuit_breaker_aberto{dependencia}` (1 aberto, 0,5 em meia-abertura, 0 fechado; `billing` e `jwks`), `pytstop_jwks_falhas_total` e as três gauges do coletor da [Observabilidade](../observabilidade.md); fica fora da borda |

## Exemplos com `curl`

Um bloco por fluxo, com o método e a rota do OpenAPI. Os comandos rodam contra a stack completa no kind, pela borda (o Kong do `platform`, com certificado autoassinado, daí o `-k`), porque as rotas autenticadas validam o token no JWKS do OS Service; com o `make compose-up` sozinho só as sondas respondem, em `http://127.0.0.1:8003`.

Preparação: o token do admin, que pode agir em todas as rotas, pelo login do OS Service. O e-mail do admin é a chave `ADMIN_EMAIL` do ConfigMap do OS Service, achado pela chave e não pelo nome, como no [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md) do `platform`; a senha fica no Secret `os-admin`, e o `printf`, embutido no shell, a entrega ao `curl` pela entrada padrão, fora da lista de processos. Um mecânico cadastrado pelo admin no OS Service (`POST /api/v1/autenticacao/registrar`) usa o próprio token nas mesmas rotas de diagnóstico e execução.

```bash
export BASE_URL=https://localhost/execucao
email=$(kubectl --context kind-pytstop-p4 -n pytstop-os get configmaps -o json | jq -r 'first(.items[].data.ADMIN_EMAIL // empty)')
senha=$(kubectl --context kind-pytstop-p4 -n pytstop-os get secret os-admin -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d)
export TOKEN=$(printf '{"email": "%s", "senha": "%s"}' "$email" "$senha" \
  | curl -sk https://localhost/os/api/v1/autenticacao/login -H 'Content-Type: application/json' --data @- \
  | jq -r .access_token)
```

**Saúde**, sem token (o `/metrics` não está no OpenAPI e fica fora da borda: [Observabilidade](../observabilidade.md)):

```bash
curl -sk "$BASE_URL/api/v1/saude"
curl -sk "$BASE_URL/api/v1/saude/pronto"
```

**Estoque**: escrita só do admin; leitura também do mecânico e do atendente. O SKU novo precisa existir também na tabela de preços do Billing, dono dos códigos, e o `DELETE` desativa a peça sem apagar o histórico:

```bash
export SKU=PEC-CORREIA-DENTADA
curl -sk -X POST "$BASE_URL/api/v1/estoque" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sku": "PEC-CORREIA-DENTADA", "nome": "Correia dentada", "quantidade_disponivel": 8}'
curl -sk "$BASE_URL/api/v1/estoque?limit=20" -H "Authorization: Bearer $TOKEN"
curl -sk "$BASE_URL/api/v1/estoque/$SKU" -H "Authorization: Bearer $TOKEN"
curl -sk -X PATCH "$BASE_URL/api/v1/estoque/$SKU/quantidade" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"quantidade_disponivel": 12}'
curl -sk -X PUT "$BASE_URL/api/v1/estoque/$SKU" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"nome": "Correia dentada (kit)", "ativo": true}'
curl -sk -X DELETE "$BASE_URL/api/v1/estoque/$SKU" -H "Authorization: Bearer $TOKEN"
```

**Diagnóstico**, pelo mecânico ou pelo admin. A ordem chega pela saga: uma OS aberta no OS Service (`POST /os/api/v1/ordens-de-servico`, no [README dele](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service#readme)) aparece na fila do mecânico, de onde sai o `ORDEM_ID`. Os códigos da conclusão são os do seed do Billing:

```bash
export ORDEM_ID=$(curl -sk "$BASE_URL/api/v1/diagnosticos?status=AGUARDANDO" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.items[0].ordem_id')
curl -sk -X POST "$BASE_URL/api/v1/diagnosticos/$ORDEM_ID/inicio" -H "Authorization: Bearer $TOKEN"
curl -sk -X POST "$BASE_URL/api/v1/diagnosticos/$ORDEM_ID/conclusao" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"itens": [{"tipo": "servico", "codigo": "SRV-FREIOS", "quantidade": 1},
                 {"tipo": "peca", "codigo": "PEC-PASTILHA-FREIO", "quantidade": 1}],
       "observacoes": "Pastilhas dianteiras no limite"}'
```

**Execução**, pelo mecânico ou pelo admin (a fila, também pelo atendente). Com o orçamento aprovado, a saga reserva as peças e, com o pagamento confirmado, agenda a execução, e a ordem entra na fila. O início é o pivot da saga e exige a reserva ativa; a finalização dá baixa nas peças:

```bash
curl -sk "$BASE_URL/api/v1/fila" -H "Authorization: Bearer $TOKEN"
curl -sk -X POST "$BASE_URL/api/v1/execucoes/$ORDEM_ID/inicio" -H "Authorization: Bearer $TOKEN"
curl -sk -X POST "$BASE_URL/api/v1/execucoes/$ORDEM_ID/finalizacao" -H "Authorization: Bearer $TOKEN"
```

O `make test` confere que cada método e rota do OpenAPI tem um `curl` aqui, com o token nas rotas autenticadas e, nas rotas com corpo, o `Content-Type: application/json` e um corpo que o schema da rota aceita ([`test_documentacao.py`](../../tests/unitarios/test_documentacao.py)).

## Como a coleção é conferida

O `make docs-api` não sobe servidor nem banco; o `make test` reprova se a coleção perder o token bearer, o `noauth` das sondas, o `Content-Type` dos corpos, o `{{baseUrl}}` das URLs ou a resposta de exemplo de uma requisição.
