SELECT
  locktype,
  database,
  relation::regclass::text AS relation_name,
  page,
  tuple,
  virtualxid,
  transactionid::text AS transactionid,
  classid,
  mode,
  granted,
  fastpath,
  pid
FROM pg_locks
WHERE pid <> pg_backend_pid();
