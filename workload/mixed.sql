-- mixed.sql — Workload MIXED (TPC-B-like, customizado)
--
-- Versão explícita do TPC-B do pgbench builtin. Existe pra:
--   - Documentar exatamente o que é executado (vs caixa preta do --builtin)
--   - Permitir variar o mix sem recompilar pgbench
--   - Servir de baseline comparável com a literatura (TPC-B é referência)
--
-- Cada transação faz: 3 UPDATEs + 1 SELECT + 1 INSERT.
-- Estressa de tudo um pouco: row locks, WAL, plan cache, leitura.

\set aid random(1, 100000 * :scale)
\set bid random(1, 1 * :scale)
\set tid random(1, 10 * :scale)
\set delta random(-5000, 5000)
BEGIN;
UPDATE pgbench_accounts SET abalance = abalance + :delta WHERE aid = :aid;
SELECT abalance FROM pgbench_accounts WHERE aid = :aid;
UPDATE pgbench_tellers SET tbalance = tbalance + :delta WHERE tid = :tid;
UPDATE pgbench_branches SET bbalance = bbalance + :delta WHERE bid = :bid;
INSERT INTO pgbench_history (tid, bid, aid, delta, mtime)
VALUES (:tid, :bid, :aid, :delta, CURRENT_TIMESTAMP);
END;
