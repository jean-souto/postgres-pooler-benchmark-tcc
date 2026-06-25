-- Inicialização do PostgreSQL para os experimentos do TCC
-- Executa apenas na primeira criação do volume (docker-entrypoint-initdb.d)

-- Extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Reset de estatísticas (boas-vindas limpas)
SELECT pg_stat_statements_reset();
SELECT pg_stat_reset();

-- ============================================================
-- Auth setup pra PgCat (issue postgresml/pgcat#255)
-- ============================================================
-- PgCat só aceita MD5 client-side, mas o `auth_query` precisa ler `pg_shadow`,
-- que é restrito a superusers. Solução: função SECURITY DEFINER que retorna
-- (usename, passwd) limitado ao usuário consultado, com EXECUTE concedido só
-- pra `pgcat_auth`. Padrão recomendado pela documentação do PgBouncer/PgCat.

-- Re-encripta o usuário tcc com MD5 (postgresql.conf já seta password_encryption=md5,
-- mas o user pode ter sido criado pelo POSTGRES_PASSWORD do compose ANTES dessa
-- diretiva entrar em vigor — re-aplica explicitamente).
ALTER USER tcc WITH PASSWORD 'tcc';

-- Usuário dedicado pra auth_query do PgCat
CREATE USER pgcat_auth WITH PASSWORD 'pgcat_auth_pass';

-- Função wrapper SECURITY DEFINER (executa como dono = postgres superuser)
CREATE OR REPLACE FUNCTION user_lookup(in_user text, OUT uname text, OUT phash text)
RETURNS record AS $$
BEGIN
  SELECT usename, passwd
    FROM pg_catalog.pg_shadow
    WHERE usename = in_user
    INTO uname, phash;
  RETURN;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE ALL ON FUNCTION user_lookup(text) FROM public;
GRANT EXECUTE ON FUNCTION user_lookup(text) TO pgcat_auth;
