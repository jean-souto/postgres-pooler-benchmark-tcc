"""Utilitarios para carregar e processar dados de experimentos do collector.

Cada experimento eh um diretorio em data/{exp_id}/ com:
  - config.json
  - pg_stat_activity.csv  (snapshot por amostra)
  - pg_locks.csv          (snapshot por amostra)
  - pg_stat_statements.csv (CUMULATIVO -- precisa delta)
  - pg_stat_io.csv         (CUMULATIVO -- precisa delta)
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Colunas numericas conhecidas por view (best-effort: faltantes sao ignoradas).
NUMERIC_COLS: dict[str, list[str]] = {
    "activity": [
        "pid",
        "query_duration_s",
        "state_duration_s",
        "xact_duration_s",
    ],
    "locks": ["pid"],
    "statements": [
        # queryid: omitido propositalmente — SQL faz queryid::text
        "calls",
        "total_exec_time",
        "mean_exec_time",
        "rows",
        "shared_blks_hit",
        "shared_blks_read",
        "wal_records",
        "wal_bytes",
        "wal_buffers_full",
        "parallel_workers_launched",
    ],
    "io": [
        "reads",
        "read_bytes",
        "read_time",
        "writes",
        "write_bytes",
        "extends",
        "hits",
        "evictions",
        "fsyncs",
        "fsync_time",
    ],
}

# Colunas tipicamente cumulativas (uteis pra `compute_deltas`).
CUMULATIVE_COLS: dict[str, list[str]] = {
    "statements": [
        "calls",
        "total_exec_time",
        "rows",
        "shared_blks_hit",
        "shared_blks_read",
        "wal_records",
        "wal_bytes",
        "wal_buffers_full",
        "parallel_workers_launched",
    ],
    "io": [
        "reads",
        "read_bytes",
        "read_time",
        "writes",
        "write_bytes",
        "extends",
        "hits",
        "evictions",
        "fsyncs",
        "fsync_time",
    ],
}

CSV_FILES = {
    "activity": "pg_stat_activity.csv",
    "locks": "pg_locks.csv",
    "statements": "pg_stat_statements.csv",
    "io": "pg_stat_io.csv",
}


def _read_csv_safe(path: Path, numeric_cols: list[str]) -> pd.DataFrame:
    """Le CSV defensivamente. Retorna DataFrame vazio se ausente/invalido."""
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        log.warning("CSV ilegivel %s: %s", path, e)
        return pd.DataFrame()
    if "sample_ts" in df.columns:
        df["sample_ts"] = pd.to_datetime(df["sample_ts"], errors="coerce", utc=True)
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_experiment(exp_dir: Path) -> dict:
    """Carrega 1 experimento de `exp_dir`.

    Retorna {"config": dict, "activity": DF, "locks": DF, "statements": DF, "io": DF}.
    DataFrames vazios quando o CSV nao existe.
    """
    exp_dir = Path(exp_dir)
    config_path = exp_dir / "config.json"
    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as e:
            log.warning("config.json invalido em %s: %s", exp_dir, e)

    out: dict = {"config": config}
    for key, fname in CSV_FILES.items():
        out[key] = _read_csv_safe(exp_dir / fname, NUMERIC_COLS.get(key, []))
    return out


def load_experiments(
    data_dir: Path, glob_pattern: str = "*"
) -> dict[str, dict]:
    """Carrega multiplos experimentos em paralelo.

    Retorna {exp_id: experiment_dict}. exp_id eh o nome do diretorio.
    """
    data_dir = Path(data_dir)
    if not data_dir.exists():
        log.warning("data_dir inexistente: %s", data_dir)
        return {}

    exp_dirs = [p for p in data_dir.glob(glob_pattern) if p.is_dir()]
    if not exp_dirs:
        return {}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(exp_dirs))) as ex:
        futures = {ex.submit(load_experiment, d): d for d in exp_dirs}
        for fut, d in futures.items():
            try:
                results[d.name] = fut.result()
            except Exception as e:
                log.warning("Falha carregando %s: %s", d, e)
    return results


def compute_deltas(
    df: pd.DataFrame,
    group_by: list[str],
    cumulative_cols: list[str],
) -> pd.DataFrame:
    """Calcula delta entre amostras consecutivas dentro de cada grupo.

    Para cada coluna em `cumulative_cols`, cria `delta_<col>`. Tambem cria
    `interval_s` (gap entre amostras dentro do grupo). A primeira amostra
    de cada grupo perde os deltas (NaN -> filtrada).
    """
    if df.empty or "sample_ts" not in df.columns:
        return pd.DataFrame()

    # Mantem apenas colunas que de fato existem.
    cols_present = [c for c in cumulative_cols if c in df.columns]
    group_present = [c for c in group_by if c in df.columns]
    if not cols_present:
        return pd.DataFrame()

    sort_keys = [*group_present, "sample_ts"]
    sorted_df = df.sort_values(sort_keys).copy()

    if group_present:
        grouped = sorted_df.groupby(group_present, dropna=False, sort=False)
        for col in cols_present:
            sorted_df[f"delta_{col}"] = grouped[col].diff()
        sorted_df["interval_s"] = (
            grouped["sample_ts"].diff().dt.total_seconds()
        )
    else:
        for col in cols_present:
            sorted_df[f"delta_{col}"] = sorted_df[col].diff()
        sorted_df["interval_s"] = sorted_df["sample_ts"].diff().dt.total_seconds()

    delta_cols = [f"delta_{c}" for c in cols_present]
    return sorted_df.dropna(subset=delta_cols, how="all").reset_index(drop=True)


def wait_event_distribution(activity: pd.DataFrame) -> pd.DataFrame:
    """Distribuicao de wait events entre backends 'active'.

    Retorna DF com [wait_event_type, wait_event, count] ordenado desc.
    """
    if activity.empty or "state" not in activity.columns:
        return pd.DataFrame(columns=["wait_event_type", "wait_event", "count"])

    active = activity[activity["state"] == "active"]
    if active.empty:
        return pd.DataFrame(columns=["wait_event_type", "wait_event", "count"])

    # Trata NaN/None como 'CPU' (backend rodando, sem wait).
    cols = ["wait_event_type", "wait_event"]
    cols_present = [c for c in cols if c in active.columns]
    if not cols_present:
        return pd.DataFrame(columns=["wait_event_type", "wait_event", "count"])

    fill = {c: "CPU" for c in cols_present}
    grouped = (
        active[cols_present]
        .fillna(fill)
        .value_counts()
        .reset_index(name="count")
    )
    return grouped.sort_values("count", ascending=False).reset_index(drop=True)


def lock_summary(locks: pd.DataFrame) -> pd.DataFrame:
    """Agrupa locks por (locktype, mode) e conta. Ordenado desc."""
    if locks.empty:
        return pd.DataFrame(columns=["locktype", "mode", "count"])

    cols = [c for c in ("locktype", "mode") if c in locks.columns]
    if not cols:
        return pd.DataFrame(columns=["locktype", "mode", "count"])

    grouped = locks[cols].value_counts().reset_index(name="count")
    return grouped.sort_values("count", ascending=False).reset_index(drop=True)


# Regex para extrair metricas do stdout do pgbench.
_TPS_RE = re.compile(r"tps\s*=\s*([\d.]+)", re.IGNORECASE)
_LATENCY_RE = re.compile(r"latency average\s*=\s*([\d.]+)\s*ms", re.IGNORECASE)
_LATENCY_STDDEV_RE = re.compile(r"latency stddev\s*=\s*([\d.]+)\s*ms", re.IGNORECASE)
_CLIENTS_RE = re.compile(r"number of clients:\s*(\d+)", re.IGNORECASE)
_THREADS_RE = re.compile(r"number of threads:\s*(\d+)", re.IGNORECASE)
_DURATION_RE = re.compile(r"duration:\s*(\d+)\s*s", re.IGNORECASE)
_TXN_RE = re.compile(r"number of transactions actually processed:\s*(\d+)", re.IGNORECASE)


def tps_from_pgbench_log(log_path: Path) -> dict:
    """Parseia stdout do pgbench. Retorna dict com tps, latency_ms, etc.

    Campos ausentes ficam None. Nao crasha em log incompleto.
    """
    log_path = Path(log_path)
    out: dict = {
        "tps": None,
        "latency_avg_ms": None,
        "latency_stddev_ms": None,
        "clients": None,
        "threads": None,
        "duration_s": None,
        "transactions": None,
    }
    if not log_path.exists():
        return out

    text = log_path.read_text(errors="replace")

    def _grab(rx: re.Pattern, cast):
        m = rx.search(text)
        if not m:
            return None
        try:
            return cast(m.group(1))
        except (ValueError, TypeError):
            return None

    out["tps"] = _grab(_TPS_RE, float)
    out["latency_avg_ms"] = _grab(_LATENCY_RE, float)
    out["latency_stddev_ms"] = _grab(_LATENCY_STDDEV_RE, float)
    out["clients"] = _grab(_CLIENTS_RE, int)
    out["threads"] = _grab(_THREADS_RE, int)
    out["duration_s"] = _grab(_DURATION_RE, int)
    out["transactions"] = _grab(_TXN_RE, int)
    return out


# ============================================================
# Helpers de AGREGAÇÃO — empilham dados de múltiplos experimentos
# pra plots comparativos. Todos lidam com label vazio (run failed).
# ============================================================

def tps_dataframe(experiments: dict, data_root: Path) -> pd.DataFrame:
    """DataFrame com 1 linha por experimento: label + métricas pgbench.

    Pula experimentos sem label (run failed antes do collector ter config).
    Colunas: setup, workload, mode, pool_size, clients, rep, tps,
             latency_avg_ms, latency_stddev_ms, transactions, exp_id
    """
    rows = []
    for exp_id, exp in experiments.items():
        cfg = exp.get("config", {}) or {}
        label = cfg.get("label") or {}
        if not label:
            continue
        log_path = Path(data_root) / exp_id / "pgbench_stdout.log"
        tps_data = tps_from_pgbench_log(log_path)
        row = {**label, "exp_id": exp_id, **tps_data}
        # tipos numéricos: converte ou marca None (consistência pra groupby)
        for k in ("clients", "rep", "pool_size"):
            if k not in row:
                continue
            v = row[k]
            if v in (None, "", "none"):
                row[k] = None  # NaN no DataFrame; sortable
                continue
            try: row[k] = int(v)
            except (ValueError, TypeError): pass
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _stack_with_label(experiments: dict, key: str) -> pd.DataFrame:
    """Empilha CSVs de uma view (key) de todos os experimentos com label.

    Pula experimentos sem label (run failed sem dados).
    """
    parts = []
    for exp_id, exp in experiments.items():
        df = exp.get(key)
        if df is None or df.empty:
            continue
        cfg = exp.get("config", {}) or {}
        label = cfg.get("label") or {}
        if not label:
            continue
        # Adiciona colunas de label se ainda não estão (caso CSV não tinha)
        df = df.copy()
        df["exp_id"] = exp_id
        for lk, lv in label.items():
            if lk not in df.columns:
                df[lk] = lv
        parts.append(df)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True, sort=False)


def master_activity(experiments: dict) -> pd.DataFrame:
    """Empilha pg_stat_activity de todos os experimentos com label."""
    return _stack_with_label(experiments, "activity")


def master_locks(experiments: dict) -> pd.DataFrame:
    """Empilha pg_locks de todos os experimentos com label."""
    return _stack_with_label(experiments, "locks")


def master_statements_deltas(experiments: dict) -> pd.DataFrame:
    """Calcula deltas de pg_stat_statements e empilha com label.

    Cada linha: 1 amostra (delta) de uma queryid de um experimento.
    """
    parts = []
    for exp_id, exp in experiments.items():
        df = exp.get("statements")
        if df is None or df.empty or "queryid" not in df.columns:
            continue
        cfg = exp.get("config", {}) or {}
        label = cfg.get("label") or {}
        if not label:
            continue
        deltas = compute_deltas(df, group_by=["queryid"], cumulative_cols=CUMULATIVE_COLS["statements"])
        if deltas.empty:
            continue
        deltas["exp_id"] = exp_id
        for lk, lv in label.items():
            if lk not in deltas.columns:
                deltas[lk] = lv
        parts.append(deltas)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True, sort=False)


def master_io_deltas(experiments: dict) -> pd.DataFrame:
    """Calcula deltas de pg_stat_io e empilha com label."""
    parts = []
    for exp_id, exp in experiments.items():
        df = exp.get("io")
        if df is None or df.empty:
            continue
        cfg = exp.get("config", {}) or {}
        label = cfg.get("label") or {}
        if not label:
            continue
        gb = [c for c in ("backend_type", "object", "context") if c in df.columns]
        deltas = compute_deltas(df, group_by=gb, cumulative_cols=CUMULATIVE_COLS["io"])
        if deltas.empty:
            continue
        deltas["exp_id"] = exp_id
        for lk, lv in label.items():
            if lk not in deltas.columns:
                deltas[lk] = lv
        parts.append(deltas)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True, sort=False)


CUMULATIVE_COLS["statements"]  # ensure import works


# Pra usar nas células do notebook (compatibilidade backwards)
__all__ = [
    "load_experiment", "load_experiments", "compute_deltas",
    "wait_event_distribution", "lock_summary",
    "tps_from_pgbench_log", "tps_dataframe",
    "master_activity", "master_locks", "master_statements_deltas", "master_io_deltas",
    "CUMULATIVE_COLS", "NUMERIC_COLS",
]
