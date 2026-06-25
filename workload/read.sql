-- read.sql — Workload READ-ONLY
--
-- Cada transação implícita = 1 SELECT por PK em pgbench_accounts.
-- Zero locks de escrita, zero contenção de WAL. Estressa puramente:
--   - Throughput de leitura
--   - Plan cache hit rate
--   - Latência cliente↔pooler↔postgres
--   - I/O de leitura (shared_buffers / disk)
--
-- Transações curtas (1 query) são propositais: maximizam o número de
-- hand-offs cliente↔backend que o pooler precisa coordenar (especialmente
-- relevante pra transaction-mode pooling).

\set aid random(1, 100000 * :scale)
SELECT abalance FROM pgbench_accounts WHERE aid = :aid;
