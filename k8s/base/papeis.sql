-- Papeis do banco do Execution Service, um por uso (ADR-042). A imagem do
-- PostgreSQL roda este script uma vez, na primeira inicializacao do volume,
-- como o superusuario postgres e conectada ao banco execucao. As senhas vem do
-- ambiente do container do banco (Secret execucao-postgres) por \getenv, nunca
-- de argumento; sem a variavel, a referencia fica crua e o script para no erro
-- de sintaxe (ON_ERROR_STOP). O trivy le PASSWORD :'var' como senha escrita no
-- ConfigMap (KSV-0109): a excecao, so para este ConfigMap, esta em
-- k8s/trivy-ignore.rego, e scripts/ci/confere-senhas-do-script.sh (make
-- manifests) reprova PASSWORD com valor literal ou com variavel que nao vem do
-- \getenv.
\set ON_ERROR_STOP on
-- O psql troca :'var' pelo valor antes de enviar o comando, e o servidor recebe
-- a senha em claro. Estes cinco SET, so desta sessao, desligam todo parametro que
-- grava o texto do comando e a tiram do log do servidor: o log_statement em all
-- ou ddl, a linha STATEMENT de um comando que falha e o log por duracao e por
-- amostragem.
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SET log_min_duration_statement = -1;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;
\getenv dono POSTGRES_OWNER_PASSWORD
\getenv aplicacao POSTGRES_APP_PASSWORD
\getenv monitor POSTGRES_EXPORTER_PASSWORD

-- Dono do banco e das tabelas (DDL): so o Job de migracao.
CREATE ROLE execucao LOGIN PASSWORD :'dono';
ALTER DATABASE execucao OWNER TO execucao;
ALTER SCHEMA public OWNER TO execucao;

-- O PUBLIC tem CONNECT e TEMPORARY em todo banco novo: sem tirar, os GRANT
-- CONNECT abaixo nao dizem nada, e qualquer papel entra no banco postgres e cria
-- tabela temporaria. Cada papel recebe so o CONNECT de que precisa.
REVOKE ALL ON DATABASE execucao FROM PUBLIC;
REVOKE ALL ON DATABASE postgres FROM PUBLIC;

-- API, relay e consumidor: so DML. As tabelas nascem depois, no Job, como o
-- dono; os privilegios padrao cobrem cada uma, inclusive a alembic_version que o
-- aguarda-migracao le (um GRANT nas tabelas de agora nao alcancaria nenhuma).
CREATE ROLE execucao_app LOGIN PASSWORD :'aplicacao';
GRANT CONNECT ON DATABASE execucao TO execucao_app;
GRANT USAGE ON SCHEMA public TO execucao_app;
ALTER DEFAULT PRIVILEGES FOR ROLE execucao IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO execucao_app;
ALTER DEFAULT PRIVILEGES FOR ROLE execucao IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO execucao_app;

-- postgres_exporter (sidecar do banco): estatisticas, sem ler tabela.
CREATE ROLE execucao_exporter LOGIN PASSWORD :'monitor';
GRANT CONNECT ON DATABASE execucao TO execucao_exporter;
GRANT pg_monitor TO execucao_exporter;
