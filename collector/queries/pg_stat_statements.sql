SELECT
  queryid::text AS queryid,
  calls,
  total_exec_time,
  mean_exec_time,
  stddev_exec_time,
  total_plan_time,
  mean_plan_time,
  rows,
  shared_blks_hit,
  shared_blks_read,
  shared_blks_dirtied,
  shared_blks_written,
  temp_blks_read,
  temp_blks_written,
  shared_blk_read_time,
  shared_blk_write_time,
  wal_records,
  wal_fpi,
  wal_bytes,
  wal_buffers_full,
  parallel_workers_to_launch,
  parallel_workers_launched,
  substring(query, 1, 500) AS query
FROM pg_stat_statements
WHERE query NOT LIKE '%pg_stat%'
ORDER BY total_exec_time DESC
LIMIT 50;
