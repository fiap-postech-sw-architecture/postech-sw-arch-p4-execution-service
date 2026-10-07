-- Papeis do banco do Execution Service, um por uso (ADR-042). A imagem do
-- PostgreSQL roda este script uma vez, na primeira inicializacao do volume,
-- como o superusuario postgres e conectada ao banco execucao. As senhas vem do
-- ambiente do container do banco (Secret execucao-postgres) por \getenv, nunca
-- de argumento; sem a variavel, a referencia fica crua e o script para no erro
-- de sintaxe (ON_ERROR_STOP).
--
-- Cada variavel vai na linha de baixo do PASSWORD, porque a regra KSV-0109 do
-- trivy toma a referencia do psql na mesma linha por senha escrita no
-- ConfigMap; ignorar a regra deixaria os outros ConfigMaps sem conferencia.
\set ON_ERROR_STOP on
-- O psql troca :'var' pelo valor antes de enviar o comando, e o servidor recebe
-- a senha em claro. Estes dois SET, so desta sessao, a tiram do log do
-- servidor: sem eles, um log_statement all ou ddl grava o comando, e um comando
-- que falha grava a linha STATEMENT com a senha.
SET log_statement = none;
SET log_min_error_statement = panic;
\getenv dono POSTGRES_OWNER_PASSWORD
\getenv aplicacao POSTGRES_APP_PASSWORD
\getenv monitor POSTGRES_EXPORTER_PASSWORD

-- Dono do banco e das tabelas (DDL): so o Job de migracao.
CREATE ROLE execucao LOGIN PASSWORD
  :'dono';
ALTER DATABASE execucao OWNER TO execucao;
ALTER SCHEMA public OWNER TO execucao;

-- API, relay e consumidor: so DML. As tabelas nascem depois, no Job, como o
-- dono; os privilegios padrao cobrem cada uma, inclusive a alembic_version que o
-- aguarda-migracao le (um GRANT nas tabelas de agora nao alcancaria nenhuma).
CREATE ROLE execucao_app LOGIN PASSWORD
  :'aplicacao';
GRANT CONNECT ON DATABASE execucao TO execucao_app;
GRANT USAGE ON SCHEMA public TO execucao_app;
ALTER DEFAULT PRIVILEGES FOR ROLE execucao IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO execucao_app;
ALTER DEFAULT PRIVILEGES FOR ROLE execucao IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO execucao_app;

-- postgres_exporter (sidecar do banco): estatisticas, sem ler tabela.
CREATE ROLE execucao_exporter LOGIN PASSWORD
  :'monitor';
GRANT pg_monitor TO execucao_exporter;
