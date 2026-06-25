"""Sampler de métricas server-side do PostgreSQL e (opcionalmente) do pooler.

- Postgres: pg_stat_activity, pg_locks, pg_stat_statements, pg_stat_io
- PgBouncer (--pooler-type=pgbouncer): SHOW POOLS, SHOW STATS via admin DB 'pgbouncer'
- PgCat (--pooler-type=pgcat): mesmas queries (admin DB compatível com PgBouncer)
- Pgpool-II (--pooler-type=pgpool): SHOW POOL_NODES, SHOW POOL_PROCESSES (legacy)

Grava CSVs com timestamp + label do experimento. Conexões dedicadas; loop com
grade temporal fixa (sem drift cumulativo).
"""
from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg

log = logging.getLogger(__name__)

QUERY_DIR = Path(__file__).parent / "queries"

POSTGRES_QUERIES = {
    "pg_stat_activity": QUERY_DIR / "pg_stat_activity.sql",
    "pg_locks": QUERY_DIR / "pg_locks.sql",
    "pg_stat_statements": QUERY_DIR / "pg_stat_statements.sql",
    "pg_stat_io": QUERY_DIR / "pg_stat_io.sql",
}

POOLER_QUERIES = {
    "pgbouncer": {
        "pgbouncer_show_pools": QUERY_DIR / "pgbouncer_show_pools.sql",
        "pgbouncer_show_stats": QUERY_DIR / "pgbouncer_show_stats.sql",
    },
    # PgCat 1.2 implementa o mesmo wire protocol admin que PgBouncer (DB
    # alias 'pgbouncer'), então as MESMAS queries funcionam. Mantemos prefixo
    # 'pgcat_' nos nomes dos CSVs pra distinguir setups na análise.
    "pgcat": {
        "pgcat_show_pools": QUERY_DIR / "pgbouncer_show_pools.sql",
        "pgcat_show_stats": QUERY_DIR / "pgbouncer_show_stats.sql",
    },
    "pgpool": {
        "pgpool_show_pool_nodes": QUERY_DIR / "pgpool_show_pool_nodes.sql",
        "pgpool_show_pool_processes": QUERY_DIR / "pgpool_show_pool_processes.sql",
    },
}


@dataclass
class SamplerConfig:
    dsn: str
    output_dir: Path
    interval_s: float = 1.0
    duration_s: float = 60.0
    label: dict = field(default_factory=dict)
    reset_stats: bool = False
    pooler_dsn: Optional[str] = None  # quando definido, amostra também o pooler
    pooler_type: Optional[str] = None  # 'pgbouncer' | 'pgpool'


class CsvSink:
    """Escreve uma view em um CSV. Header é definido na primeira amostra."""

    def __init__(self, path: Path):
        self.path = path
        self._fp = None
        self._writer = None
        self._header: list[str] | None = None

    def write(self, sample_ts: str, label: dict, rows: list[dict]) -> int:
        if not rows:
            return 0
        if self._writer is None:
            self._header = ["sample_ts", *label.keys(), *rows[0].keys()]
            self._fp = self.path.open("w", newline="")
            self._writer = csv.DictWriter(self._fp, fieldnames=self._header)
            self._writer.writeheader()
        for r in rows:
            self._writer.writerow({"sample_ts": sample_ts, **label, **r})
        self._fp.flush()
        return len(rows)

    def close(self):
        if self._fp:
            self._fp.close()


def _load_queries(query_map: dict[str, Path]) -> dict[str, str]:
    # .strip() remove o newline final do arquivo. O admin do PgCat 1.2 rejeita
    # "SHOW POOLS;\n" com "Unsupported SHOW query" (PgBouncer tolera). Inofensivo
    # pras queries server-side (Postgres ignora whitespace nas bordas).
    return {name: path.read_text().strip() for name, path in query_map.items()}


def _safe_dsn(dsn: str) -> str:
    return dsn.split("@")[-1] if "@" in dsn else dsn


def _write_config(cfg: SamplerConfig):
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    config_path = cfg.output_dir / "config.json"
    config_path.write_text(json.dumps({
        "dsn": _safe_dsn(cfg.dsn),
        "pooler_dsn": _safe_dsn(cfg.pooler_dsn) if cfg.pooler_dsn else None,
        "pooler_type": cfg.pooler_type,
        "interval_s": cfg.interval_s,
        "duration_s": cfg.duration_s,
        "label": cfg.label,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def _sample_queries(cur, queries: dict[str, str], sinks: dict[str, CsvSink],
                    counts: dict[str, int], sample_ts: str, label: dict):
    """Executa cada query e grava resultado no sink correspondente."""
    for name, sql in queries.items():
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            counts[name] += sinks[name].write(sample_ts, label, rows)
        except Exception as e:
            log.warning("Falha em %s: %s", name, e)


def run(cfg: SamplerConfig) -> dict[str, int]:
    """Executa o loop de amostragem. Retorna contagem de linhas por view."""
    if cfg.pooler_dsn and not cfg.pooler_type:
        raise ValueError("pooler_dsn definido sem pooler_type")
    if cfg.pooler_type and cfg.pooler_type not in POOLER_QUERIES:
        raise ValueError(f"pooler_type inválido: {cfg.pooler_type} (use {list(POOLER_QUERIES)})")

    _write_config(cfg)

    pg_queries = _load_queries(POSTGRES_QUERIES)
    pooler_queries = _load_queries(POOLER_QUERIES[cfg.pooler_type]) if cfg.pooler_dsn else {}
    all_query_names = list(pg_queries) + list(pooler_queries)

    sinks = {name: CsvSink(cfg.output_dir / f"{name}.csv") for name in all_query_names}
    counts = {name: 0 for name in all_query_names}

    log.info("Conectando Postgres: %s", _safe_dsn(cfg.dsn))
    if cfg.pooler_dsn:
        log.info("Conectando pooler (%s): %s", cfg.pooler_type, _safe_dsn(cfg.pooler_dsn))
    log.info("Intervalo=%.2fs duração=%.1fs output=%s", cfg.interval_s, cfg.duration_s, cfg.output_dir)

    samples = 0
    pg_conn = None
    pooler_conn = None
    try:
        pg_conn = psycopg.connect(cfg.dsn, autocommit=True)

        if cfg.reset_stats:
            with pg_conn.cursor() as cur:
                for stmt, label in [
                    ("SELECT pg_stat_statements_reset();", "pg_stat_statements_reset()"),
                    ("SELECT pg_stat_reset_shared('io');", "pg_stat_reset_shared('io')"),
                ]:
                    try:
                        cur.execute(stmt)
                        log.info("%s OK", label)
                    except Exception as e:
                        log.warning("%s falhou: %s", label, e)

        if cfg.pooler_dsn:
            # PgBouncer admin DB e Pgpool não suportam extended query protocol.
            # prepare_threshold=None força psycopg a usar simple protocol.
            pooler_conn = psycopg.connect(
                cfg.pooler_dsn, autocommit=True, prepare_threshold=None,
            )

        # Grade temporal fixa: tick n acontece em start + n*interval.
        start = time.monotonic()
        deadline = start + cfg.duration_s
        n = 0
        while time.monotonic() < deadline:
            sample_ts = datetime.now(timezone.utc).isoformat()
            with pg_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                _sample_queries(cur, pg_queries, sinks, counts, sample_ts, cfg.label)
            if pooler_conn is not None:
                # Reconecta se a conexão admin caiu (PgBouncer fecha em alguns erros).
                if pooler_conn.closed:
                    log.warning("Pooler conn fechada — reconectando")
                    pooler_conn = psycopg.connect(
                        cfg.pooler_dsn, autocommit=True, prepare_threshold=None,
                    )
                try:
                    with pooler_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                        _sample_queries(cur, pooler_queries, sinks, counts, sample_ts, cfg.label)
                except psycopg.OperationalError as e:
                    log.warning("Pooler query falhou (vai reconectar próxima amostra): %s", e)
                    try:
                        pooler_conn.close()
                    except Exception:
                        pass
            samples += 1
            n += 1
            next_tick = start + n * cfg.interval_s
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                log.warning("Tick %d atrasado %.3fs (interval=%.2f)", n, -sleep_for, cfg.interval_s)
    finally:
        for conn in (pg_conn, pooler_conn):
            if conn is not None:
                conn.close()
        for sink in sinks.values():
            sink.close()
        log.info("Amostras=%d Linhas=%s", samples, counts)
    return counts
