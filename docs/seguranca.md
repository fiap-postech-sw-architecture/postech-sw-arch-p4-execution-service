# Segurança

Detalhe da seção [Segurança](../README.md#segurança) do README. O dado pessoal que o serviço guarda, a retenção e o que fica fora dos logs, dos spans e das métricas estão na [seção 6 de `arquitetura.md`](arquitetura.md#6-segurança-e-lgpd).

## Autenticação

JWT RS256 emitido pelo OS Service, validado pela chave pública do JWKS (`JWKS_URL`, timeout de 2 s, cópia fresca por 10 min; com o OS fora, a última cópia boa vale por até 1 h e 3 falhas seguidas abrem um circuit breaker por 30 s), conferindo assinatura, `iss=pytstop-os-service`, `aud=pytstop`, `exp`, `sub` e `type=access`, com 10 s de tolerância de relógio. Toda falha de credencial responde o mesmo 401; papel válido sem permissão, 403; JWKS indisponível e sem cópia, 503 com `Retry-After`. Nenhum segredo compartilhado entre serviços.

## Papéis e auditoria

Três papéis, conferidos por rota: mecânico, atendente e admin. Mecânico e admin agem no diagnóstico e na execução; o atendente lê a fila e o estoque; só o admin escreve no estoque. Só o mecânico que iniciou conclui o diagnóstico ou finaliza a execução; o admin pode fazê-lo em nome dele, com log de auditoria (quem, ação e ordem). A matriz por rota está em [API](api/exemplos.md#rotas-e-papéis).

## LGPD

O serviço guarda o retrato do veículo (placa, marca, modelo, ano e o `veiculo_id`), a descrição do problema e as observações; nome, documento e contato do cliente ficam no OS Service. O comando `AnonimizarVeiculo` elimina a placa e os textos livres ([Participação na saga](mensageria.md#participação-na-saga)), e o mascaramento dos logs e a limpeza dos spans estão em [Observabilidade](observabilidade.md#o-que-não-é-exportado-e-por-quê).

## Segredos e papéis do banco

Nenhum Secret fica em `k8s/`. O `make deploy` do `platform` grava o `rabbitmq`, com a `RABBITMQ_URL` do usuário `execucao`, e o `execucao-postgres`, com uma senha por papel; a `DATABASE_URL` se monta no pod, por expansão de variável, e o `kubectl describe` mostra o molde, não a senha. O superusuário `postgres` só inicializa o volume: o [`papeis.sql`](../k8s/base/papeis.sql), que a imagem roda uma vez, na primeira inicialização, cria o dono `execucao` (DDL, só o Job), o `execucao_app` (só DML nas tabelas do dono, pelos privilégios padrão, e só leitura na `alembic_version`, de que o Job tira a escrita depois de migrar: API, relay e consumidor) e o `execucao_exporter` (`pg_monitor`). O script tira do `PUBLIC` o `CONNECT` e o `TEMPORARY` dos bancos `execucao` e `postgres` (o padrão de todo banco novo), e cada papel fica só com o `CONNECT` de que precisa: a aplicação e o exporter não entram no banco `postgres` nem criam tabela temporária. O script lê as senhas do ambiente do container do banco (`\getenv`) e desliga, na própria sessão, todo parâmetro que grava o texto do comando antes de usá-las (`log_statement`, `log_min_error_statement`, `log_min_duration_statement`, `log_min_duration_sample` e `log_transaction_sample_rate`): o `psql` troca a variável pelo valor antes de enviar o comando, e um `log_statement` em `all`, um comando que falha ou o log por duração e por amostragem gravariam a senha no log do servidor. O banco não aceita conexão sem senha nem no loopback e no socket (`POSTGRES_INITDB_ARGS` com `scram-sha-256`): a imagem do PostgreSQL confia neles, e o exporter, no mesmo pod, entraria como `postgres`. O [`test_papeis_do_banco.py`](../tests/integracao/test_papeis_do_banco.py) sobe a imagem e o ambiente do StatefulSet com esse script e confere cada papel e o log do servidor.

## Imagens de terceiros

`postgres:16.15` e `prometheuscommunity/postgres-exporter:v0.20.1`, por tag fixa e as da tabela de versões do `platform`. O job `trivy` do `security.yml` lista no summary de cada PR e da execução semanal os HIGH e CRITICAL com correção disponível das duas ([`scripts/ci/relatorio-trivy-terceiros.sh`](../scripts/ci/relatorio-trivy-terceiros.sh)), sem reprovar o job: nem o achado nem a falha da varredura (pull recusado, banco do trivy fora) reprovam, e a falha vira aviso no summary. Risco aceito, medido em 12/10/2026: na `postgres`, os 25 achados (1 CRITICAL e 24 HIGH) estão no `gosu`, binário Go que o pod não executa (ele roda como 999, e o entrypoint da imagem só chama o `gosu` como root); no exporter, os 16 HIGH estão no binário dele (`stdlib`, `x/crypto`, `x/net` e `x/text`), mas só o Prometheus alcança a porta 9187 (NetworkPolicy) e ele só fala com o PostgreSQL do próprio pod. Fora desses números, a varredura de segredos da `postgres` marca 1 HIGH (`AsymmetricPrivateKey`) em `/etc/ssl/private/ssl-cert-snakeoil.key`: é a chave de teste pública do pacote `ssl-cert` do Debian, igual em toda imagem que o traz, e o banco não a usa (os manifestos não ligam TLS). A correção depende de um build novo do fornecedor, então a política é subir a versão a cada release do serviço, junto da tabela do `platform`.

## Troca de senha do banco

Com janela até o restart. São os passos da [troca de senha do banco](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#troca-de-senha-do-banco) do `platform`; o exemplo troca a do `execucao_app`, e para outro papel muda a chave e o nome do papel (`POSTGRES_EXPORTER_PASSWORD` e `execucao_exporter`, `POSTGRES_OWNER_PASSWORD` e `execucao`, `POSTGRES_PASSWORD` e `postgres`):

1. Grave a senha nova no Secret, para que ela não fique só numa variável do shell:

   ```bash
   senha=$(openssl rand -hex 24)
   kubectl --context kind-pytstop-p4 -n pytstop-execucao get secret execucao-postgres -o json \
     | SENHA="$senha" jq '.data["POSTGRES_APP_PASSWORD"] = (env.SENHA | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Aplique-a no banco, como `postgres`, pela entrada padrão do `kubectl exec -i`, nunca por argumento; os cinco `SET` a tiram do log do servidor, que de outro modo a gravaria pelo `log_statement`, pela linha `STATEMENT` de um comando que falha ou pelo log por duração e por amostragem. A saída esperada é cinco `SET` e um `ALTER ROLE`:

   ```bash
   { printf '%s\n' "$senha"; cat <<'SQL'
   SET log_statement = 'none';
   SET log_min_error_statement = 'panic';
   SET log_min_duration_statement = -1;
   SET log_min_duration_sample = -1;
   SET log_transaction_sample_rate = 0;
   \getenv senha SENHA_NOVA
   ALTER ROLE execucao_app PASSWORD :'senha';
   SQL
   } | kubectl --context kind-pytstop-p4 -n pytstop-execucao exec -i statefulset/execucao-postgres -c postgres -- \
       sh -c 'read -r SENHA_NOVA && [ -n "$SENHA_NOVA" ] && export SENHA_NOVA && PGPASSWORD=$POSTGRES_PASSWORD exec psql -w -U postgres -v ON_ERROR_STOP=1'
   ```

3. Reinicie quem usa o papel, que lê a senha só no start: `execucao_app`, os Deployments (`kubectl --context kind-pytstop-p4 -n pytstop-execucao rollout restart deployment`); `execucao_exporter` e `postgres`, o StatefulSet do banco (o exporter é sidecar dele, e o comando do passo 2 usa a senha do superusuário lida no start); `execucao`, ninguém: o Job a relê no deploy seguinte. Até o restart, conexão nova com a senha antiga é recusada.
