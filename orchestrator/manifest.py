"""Manifest CSV — estado/checkpoint da bateria de experimentos.

Schema: exp_id, status, started_at, finished_at, duration_s, error
Statuses:
  - pending: criado, não rodado ainda
  - running: rodando agora (interrompido se ficar nesse estado entre execuções)
  - done: rodou com sucesso (ambos rc=0)
  - failed: rodou e falhou (rc != 0 ou exceção)
  - skipped: pulado pelo orquestrador (já estava done)
  - expected_failure: arquitetonicamente garantido falhar (clients > capacidade);
    NÃO é executado, registrado pra documentar o cliff (none > max_connections,
    pgpool > num_init_children)
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FIELDS = [
    "exp_id", "status", "started_at", "finished_at", "duration_s",
    "pgbench_exit_code", "collector_exit_code", "error",
    "setup", "workload", "mode", "pool_size", "clients", "rep",
]


def load(manifest_path: Path) -> dict[str, dict]:
    """Carrega manifest.csv. Retorna {exp_id: row_dict}. Vazio se ausente."""
    if not manifest_path.exists():
        return {}
    out: dict[str, dict] = {}
    with manifest_path.open() as f:
        for row in csv.DictReader(f):
            out[row["exp_id"]] = row
    return out


def save(manifest_path: Path, rows: dict[str, dict]):
    """Persiste manifest. Sobrescreve atomically (tmp + rename)."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for exp_id in sorted(rows):
            w.writerow(rows[exp_id])
    tmp.replace(manifest_path)


def upsert(manifest_path: Path, exp_id: str, **fields):
    """Atualiza/insere 1 linha do manifest, persiste atomically."""
    rows = load(manifest_path)
    existing = rows.get(exp_id, {"exp_id": exp_id})
    existing.update({k: v for k, v in fields.items() if v is not None})
    existing["exp_id"] = exp_id
    rows[exp_id] = existing
    save(manifest_path, rows)


def merge_runs(manifest_path: Path, runs) -> dict[str, dict]:
    """Garante linha 'pending' pra cada run; preserva linhas existentes.

    Retorna manifest atualizado.
    """
    rows = load(manifest_path)
    for r in runs:
        if r.exp_id not in rows:
            rows[r.exp_id] = {
                "exp_id": r.exp_id, "status": "pending",
                "setup": r.setup, "workload": r.workload, "mode": r.mode,
                "pool_size": r.pool_size if r.pool_size is not None else "",
                "clients": r.clients, "rep": r.rep,
                "started_at": "", "finished_at": "", "duration_s": "",
                "pgbench_exit_code": "", "collector_exit_code": "", "error": "",
            }
    save(manifest_path, rows)
    return rows


def status_summary(rows: dict[str, dict]) -> dict[str, int]:
    from collections import Counter
    return dict(Counter(r.get("status", "?") for r in rows.values()))
