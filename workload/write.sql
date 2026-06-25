-- write.sql — Workload WRITE-HEAVY
--
-- UPDATE em pgbench_branches (apenas `scale` linhas — gargalo de row lock
-- conhecido) + INSERT em pgbench_history (estresse de WAL).
--
-- Estressa principalmente:
--   - Lock:transactionid (contenção de row lock)
--   - LWLock:WALWrite (escrita no WAL buffer)
--   - IO:WalSync (commit/fsync)
--   - WAL throughput
--
-- A contenção em pgbench_branches é PROPOSITAL: com `scale=10`, só existem
-- 10 branches, então N clientes disputando 10 linhas amplifica o efeito do
-- pooler na contenção (objeto de estudo do TCC).

\set bid random(1, 1 * :scale)
\set tid random(1, 10 * :scale)
\set aid random(1, 100000 * :scale)
\set delta random(-5000, 5000)
BEGIN;
UPDATE pgbench_branches SET bbalance = bbalance + :delta WHERE bid = :bid;
INSERT INTO pgbench_history (tid, bid, aid, delta, mtime)
VALUES (:tid, :bid, :aid, :delta, CURRENT_TIMESTAMP);
END;
