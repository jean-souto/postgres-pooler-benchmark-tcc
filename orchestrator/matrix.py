"""Expansão da matriz de experimentos a partir de experiments.yaml.

Não executa nada — só gera lista de Run + utilitários (shuffle, estimativa de tempo).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import yaml

VALID_SETUPS = {"none", "pgbouncer-tx", "pgbouncer-session", "pgcat-tx",
                "pgcat-session", "rds-proxy"}
VALID_WORKLOADS = {"read", "write", "mixed"}
VALID_MODES = {"simple", "prepared"}


@dataclass(frozen=True)
class Run:
    setup: str
    workload: str
    mode: str
    pool_size: Optional[int]   # None quando setup=none
    clients: int
    rep: int
    warmup_s: int
    measure_s: int
    pgbench_log: bool
    scale: int
    feasible: bool = True   # False quando arquitetonicamente garantido falhar

    @property
    def exp_id(self) -> str:
        pool = f"pool{self.pool_size}" if self.pool_size is not None else "nopool"
        return f"{self.setup}_{self.workload}_{self.mode}_{pool}_c{self.clients}_r{self.rep}"

    @property
    def estimated_seconds(self) -> int:
        return self.warmup_s + self.measure_s + 15  # 15s overhead estimado

    def as_label_dict(self) -> dict:
        """Dict pra label do collector (k=v vira coluna nos CSVs)."""
        return {
            "setup": self.setup,
            "workload": self.workload,
            "mode": self.mode,
            "pool_size": str(self.pool_size) if self.pool_size is not None else "none",
            "clients": str(self.clients),
            "rep": str(self.rep),
        }


def _pool_sizes_for(setup: str, pool_sizes_cfg: dict) -> list[Optional[int]]:
    if setup == "none":
        return [None]
    if setup == "rds-proxy":
        # RDS Proxy é gerenciado: pool dimensionado por max_connections_percent
        # no Terraform, não variável. 1 config (pool_size None, vira 'nopool').
        return [None]
    if setup.startswith("pgbouncer"):
        return list(pool_sizes_cfg["pgbouncer"])
    if setup.startswith("pgcat"):
        # PgCat e PgBouncer usam mesmas faixas {2, 8, 100} pra paridade direta.
        return list(pool_sizes_cfg.get("pgcat", pool_sizes_cfg["pgbouncer"]))
    raise ValueError(f"Setup desconhecido: {setup}")


def _validate_preset(preset: dict, name: str):
    for s in preset["setups"]:
        if s not in VALID_SETUPS:
            raise ValueError(f"preset '{name}': setup inválido '{s}' (válidos: {VALID_SETUPS})")
    for w in preset["workloads"]:
        if w not in VALID_WORKLOADS:
            raise ValueError(f"preset '{name}': workload inválido '{w}' (válidos: {VALID_WORKLOADS})")
    for m in preset["modes"]:
        if m not in VALID_MODES:
            raise ValueError(f"preset '{name}': mode inválido '{m}' (válidos: {VALID_MODES})")


def load_yaml(yaml_path: Path) -> dict:
    return yaml.safe_load(Path(yaml_path).read_text())


def is_feasible(setup: str, pool_size: Optional[int], clients: int,
                max_backend_connections: int) -> bool:
    """Decide se um run pode FISICAMENTE rodar (vs garantido falhar arquiteturalmente).

    - setup=none: bloqueado se clients > max_backend_connections (Postgres rejeita)
    - setup=*-session: bloqueado se clients > pool_size (1:1 client:backend → trava)
    - setup=*-tx: sempre viável (enfileira em vez de bloquear)
    - setup=rds-proxy: sempre viável (transaction pooling gerenciado, multiplexa
      sobre < max_connections backends — enfileira em vez de rejeitar)
    """
    if setup == "none" and clients > max_backend_connections:
        return False
    if setup.endswith("-session") and pool_size is not None and clients > pool_size:
        return False
    return True


def expand_preset(cfg: dict, preset_name: str) -> list[Run]:
    """Expande um preset em lista de Run.

    Runs arquitetonicamente garantidos a falhar (clients > capacidade) recebem
    feasible=False — orquestrador NÃO os executa, só registra no manifest como
    'expected_failure' pra documentar.
    """
    if preset_name not in cfg.get("presets", {}):
        available = list(cfg.get("presets", {}).keys())
        raise KeyError(f"preset '{preset_name}' não existe. Disponíveis: {available}")

    defaults = cfg.get("defaults", {})
    preset = cfg["presets"][preset_name]
    _validate_preset(preset, preset_name)

    overrides = preset.get("overrides", {})
    params = {**defaults, **overrides}
    max_backend = int(params.get("max_backend_connections", 415))

    runs: list[Run] = []
    for setup in preset["setups"]:
        for workload in preset["workloads"]:
            for mode in preset["modes"]:
                for pool_size in _pool_sizes_for(setup, preset["pool_sizes"]):
                    for clients in preset["concurrencies"]:
                        for rep in range(1, preset["repetitions"] + 1):
                            runs.append(Run(
                                setup=setup,
                                workload=workload,
                                mode=mode,
                                pool_size=pool_size,
                                clients=clients,
                                rep=rep,
                                warmup_s=params["warmup_s"],
                                measure_s=params["measure_s"],
                                pgbench_log=bool(params["pgbench_log"]),
                                scale=int(params["scale"]),
                                feasible=is_feasible(setup, pool_size, clients, max_backend),
                            ))
    return runs


def shuffle_within_setup(runs: list[Run], seed: int) -> list[Run]:
    """Agrupa por setup (preserva ordem dos setups), aleatoriza dentro.

    Reduz overhead de troca de stack mas mantém proteção contra viés temporal.
    """
    rng = random.Random(seed)
    by_setup: dict[str, list[Run]] = {}
    setup_order: list[str] = []
    for r in runs:
        if r.setup not in by_setup:
            by_setup[r.setup] = []
            setup_order.append(r.setup)
        by_setup[r.setup].append(r)

    out: list[Run] = []
    for s in setup_order:
        bucket = by_setup[s]
        rng.shuffle(bucket)
        out.extend(bucket)
    return out


def estimate_duration_s(runs: list[Run]) -> int:
    return sum(r.estimated_seconds for r in runs)


def summary(runs: list[Run]) -> dict:
    """Resumo da bateria — n por dimensão."""
    from collections import Counter
    return {
        "total_runs": len(runs),
        "estimated_hours": round(estimate_duration_s(runs) / 3600, 2),
        "by_setup": dict(Counter(r.setup for r in runs)),
        "by_workload": dict(Counter(r.workload for r in runs)),
        "by_mode": dict(Counter(r.mode for r in runs)),
        "concurrencies": sorted(set(r.clients for r in runs)),
        "pool_sizes": sorted(set(r.pool_size for r in runs if r.pool_size is not None)),
        "repetitions": max(r.rep for r in runs),
    }
