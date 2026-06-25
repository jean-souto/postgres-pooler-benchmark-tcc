SELECT
  datname,
  pid,
  usename,
  application_name,
  state,
  wait_event_type,
  wait_event,
  backend_type,
  backend_start,
  xact_start,
  query_start,
  state_change,
  EXTRACT(EPOCH FROM (now() - query_start))::float AS query_duration_s,
  EXTRACT(EPOCH FROM (now() - state_change))::float AS state_duration_s,
  EXTRACT(EPOCH FROM (now() - xact_start))::float AS xact_duration_s,
  substring(query, 1, 500) AS query
FROM pg_stat_activity
WHERE pid <> pg_backend_pid();
