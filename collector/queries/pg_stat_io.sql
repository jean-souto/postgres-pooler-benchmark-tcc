SELECT
  backend_type,
  object,
  context,
  reads,
  read_bytes,
  read_time,
  writes,
  write_bytes,
  write_time,
  writebacks,
  writeback_time,
  extends,
  extend_bytes,
  extend_time,
  hits,
  evictions,
  reuses,
  fsyncs,
  fsync_time
FROM pg_stat_io
WHERE COALESCE(reads, 0) + COALESCE(writes, 0) + COALESCE(extends, 0)
    + COALESCE(hits, 0) + COALESCE(evictions, 0) + COALESCE(fsyncs, 0)
    + COALESCE(writebacks, 0) + COALESCE(reuses, 0) > 0;
