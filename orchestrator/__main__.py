"""CLI do orquestrador.

Uso:
    python -m orchestrator --preset smoke --output data/
    python -m orchestrator --preset cirurgica --output data/ --rerun-failed
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import configs, manifest, stack
from .matrix import (
    expand_preset, load_yaml, shuffle_within_setup, summary,
)
from .runner import POSTGRES_DSN_HOST, execute
from analysis.loader import tps_from_pgbench_log

log = logging.getLogger("orch")

# Sentinela pra "nenhum pool subido ainda" (distingue de pool_size=None do setup
# none/rds-proxy). Comparação por identidade — NÃO usar object() inline, que cria
# instância nova a cada chamada e nunca casa.
_UNSET = object()


def _setup_logging(level: str, log_file: Path | None):
    """Logging compacto pro orquestrador, verbose só pra warnings dos submodules."""
    handlers: list = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    fmt = "%(asctime)s %(levelname).1s %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )

    # Submódulos: só WARNING+ no console (chatos demais em INFO).
    # No DEBUG global, libera tudo.
    if level != "DEBUG":
        for name in ("orchestrator.stack", "orchestrator.runner", "orchestrator.configs",
                     "collector.sampler"):
            logging.getLogger(name).setLevel(logging.WARNING)


def _group_by_setup(runs):
    """Preserva ordem (já aleatorizada) e agrupa runs adjacentes por setup."""
    groups: list[tuple[str, list]] = []
    current_setup: str | None = None
    current_bucket: list = []
    for r in runs:
        if r.setup != current_setup:
            if current_bucket:
                groups.append((current_setup, current_bucket))
            current_setup = r.setup
            current_bucket = []
        current_bucket.append(r)
    if current_bucket:
        groups.append((current_setup, current_bucket))
    return groups


def _fmt_run_id(r) -> str:
    """Identificador compacto pra log: setup/wl/c=N/r=R [pool=P]."""
    pool = f"/p={r.pool_size}" if r.pool_size is not None else ""
    return f"{r.setup}/{r.workload}/{r.mode}{pool}/c={r.clients}/r={r.rep}"


def _fmt_status(status: str) -> str:
    return {"done": "OK    ", "failed": "FAIL  ", "skipped": "SKIP  ",
            "saturated": "SATUR ", "expected_failure": "EXPECT"}.get(status, status[:6].upper())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument("--matrix", type=Path, default=Path("experiments.yaml"))
    parser.add_argument("--preset", required=True, help="Nome do preset (smoke, cirurgica, full)")
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--manifest", type=Path, default=None,
                        help="Default: <output>/manifest.csv")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--rerun-all", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="Pra debug: limita ao primeiro N runs após shuffle")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--mode", default="local", choices=["local", "cloud"],
                        help="local=docker compose; cloud=AWS (pgbench nativo + pooler EC2 via SSH)")
    args = parser.parse_args(argv)

    _setup_logging(args.log_level, args.log_file)

    # Modo cloud: lê endpoints de ~/.tcc/env (injetado pelo user_data) e gera/
    # envia configs pro pooler. Roda NA EC2 client (não precisa de tfstate aqui).
    cloud = None
    if args.mode == "cloud":
        from .cloud_targets import load_cloud_targets
        cloud = load_cloud_targets()
        log.info("CLOUD: RDS=%s pooler=%s proxy=%s",
                 cloud.rds_endpoint, cloud.pooler_private_ip,
                 cloud.rds_proxy_endpoint or "—")
        configs.render_cloud_configs(cloud.rds_endpoint, cloud.db_user, cloud.db_password)
        stack.rsync_pooler_stack(cloud.pooler_ssh_alias)

    cfg = load_yaml(args.matrix)
    seed = cfg.get("defaults", {}).get("seed", 42)
    runs = expand_preset(cfg, args.preset)
    runs = shuffle_within_setup(runs, seed=seed)
    if args.limit:
        runs = runs[:args.limit]

    s = summary(runs)

    log.info("")
    log.info("======================================================================")
    log.info("  BATERIA: %s   |   total=%d   est=%.1fh   seed=%d",
             args.preset, s["total_runs"], s["estimated_hours"], seed)
    log.info("  setups: %s", ", ".join(f"{k}={v}" for k, v in s["by_setup"].items()))
    log.info("======================================================================")

    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest or (args.output / "manifest.csv")
    manifest.merge_runs(manifest_path, runs)

    # Pre-marca runs INVIÁVEIS como expected_failure (sem executar)
    n_expected = 0
    for r in runs:
        if not r.feasible:
            manifest.upsert(
                manifest_path, r.exp_id,
                status="expected_failure",
                error=f"clients={r.clients} excede capacidade ({r.setup})",
            )
            n_expected += 1

    # Decide quais runs precisam executar (status do manifest, não filesystem)
    rows = manifest.load(manifest_path)
    todo = []
    for r in runs:
        if not r.feasible:
            continue
        st = rows.get(r.exp_id, {}).get("status", "pending")
        if st == "done" and not args.rerun_all:
            continue
        # 'saturated' é resultado VÁLIDO (colapso medido), não falha: só re-executa
        # com --rerun-all, nunca com --rerun-failed.
        if st == "saturated" and not args.rerun_all:
            continue
        if st == "failed" and not (args.rerun_failed or args.rerun_all):
            continue
        if st == "expected_failure" and not args.rerun_all:
            continue
        todo.append(r)

    log.info("  a executar: %d   |   expected_failure: %d   |   já done/failed: %d",
             len(todo), n_expected, len(runs) - len(todo) - n_expected)
    log.info("")

    groups = _group_by_setup(todo)

    n_total = len(todo)
    n_done = 0
    counts = {"done": 0, "failed": 0, "skipped": 0}
    bateria_t0 = time.monotonic()
    pgbench_initialized = False
    final_setup: str | None = None

    # DSN do RDS/Postgres pra init e VACUUM/CHECKPOINT (computado uma vez).
    reset_dsn = (
        f"postgresql://{cloud.db_user}:{cloud.db_password}@{cloud.rds_endpoint}:5432/{cloud.db_name}"
        if cloud is not None else POSTGRES_DSN_HOST
    )

    try:
        for setup, bucket in groups:
            log.info("---------- Setup: %s   (%d runs neste bloco) ----------",
                     setup, len(bucket))
            current_pool: object = _UNSET

            for r in bucket:
                if r.pool_size != current_pool:
                    if current_pool is not _UNSET and final_setup:
                        (stack.down_cloud(final_setup, cloud.pooler_ssh_alias)
                         if cloud is not None else stack.down(final_setup))

                    # Mitigação de validade (cloud): VACUUM ANALYZE + CHECKPOINT no
                    # RDS a CADA troca de (setup, pool_size) — evita que cache quente
                    # / dead tuples de uma config contaminem a próxima.
                    # No cloud, o `up` do pooler NÃO acontece aqui: o restart
                    # per-run (abaixo) sobe um container fresco pra cada run. No
                    # local (sanity-check), mantém-se o up por bloco de pool.
                    if cloud is not None:
                        stack.vacuum_checkpoint(reset_dsn)
                    else:
                        stack.up(setup, r.pool_size)
                    final_setup = setup
                    current_pool = r.pool_size

                    if not pgbench_initialized:
                        stack.pgbench_init_if_needed(reset_dsn, scale=r.scale, cloud=cloud)
                        pgbench_initialized = True

                # Pooler FRESCO a cada run (cloud, setups com pooler). O PgCat 1.2
                # trava com "AllServersDown" após um run saturante e NÃO recupera
                # sozinho (testado: nem após ban_time; só down/up do container
                # resolve) — sem isto, contaminaria todos os runs seguintes do mesmo
                # bloco de pool_size. Restart incondicional garante isolamento e
                # SIMETRIA entre os 4 poolers self-hosted. No-op p/ none/rds-proxy
                # (sem container — não dá pra reiniciar o RDS Proxy gerenciado, que
                # de qualquer forma se recupera sozinho da saturação). VACUUM/CHECKPOINT
                # do RDS segue na troca de pool_size (bloco acima), não por run.
                if cloud is not None and setup not in stack.NO_POOLER_SETUPS:
                    stack.down_cloud(setup, cloud.pooler_ssh_alias)
                    stack.up_cloud(setup, r.pool_size, cloud.pooler_ssh_alias)

                manifest.upsert(manifest_path, r.exp_id, status="running")

                t0 = time.monotonic()
                result = execute(r, args.output, cloud=cloud)
                dur = time.monotonic() - t0
                n_done += 1
                counts[result.status] = counts.get(result.status, 0) + 1

                # Tenta extrair TPS do log (se done)
                tps_str = ""
                if result.status == "done":
                    tps_data = tps_from_pgbench_log(args.output / r.exp_id / "pgbench_stdout.log")
                    if tps_data["tps"]:
                        tps_str = f"  tps={tps_data['tps']:>9.1f}"
                        if tps_data["latency_avg_ms"]:
                            tps_str += f"  lat={tps_data['latency_avg_ms']:>6.1f}ms"

                # ETA conservador baseado em tempo médio até agora
                avg = (time.monotonic() - bateria_t0) / n_done
                eta_min = (n_total - n_done) * avg / 60

                log.info("[%3d/%d] %s  %s  %5.1fs%s   ETA %4.0fmin",
                         n_done, n_total, _fmt_status(result.status),
                         _fmt_run_id(r), dur, tps_str, eta_min)

                manifest.upsert(
                    manifest_path, r.exp_id,
                    status=result.status,
                    started_at=result.started_at,
                    finished_at=result.finished_at,
                    duration_s=round(result.duration_s, 2),
                    pgbench_exit_code=result.pgbench_exit_code,
                    collector_exit_code=result.collector_exit_code,
                    error=result.error or "",
                )

            if final_setup:
                (stack.down_cloud(final_setup, cloud.pooler_ssh_alias)
                 if cloud is not None else stack.down(final_setup))
                final_setup = None
                current_pool = object()
            log.info("")
    finally:
        configs.cleanup()

    elapsed_min = (time.monotonic() - bateria_t0) / 60
    final_rows = manifest.load(manifest_path)
    final_summary = manifest.status_summary(final_rows)

    log.info("======================================================================")
    log.info("  BATERIA FINALIZADA em %.1fmin", elapsed_min)
    log.info("  resultados: %s",
             "  ".join(f"{k}={v}" for k, v in sorted(final_summary.items())))
    log.info("  manifest: %s", manifest_path)
    log.info("======================================================================")

    return 0 if final_summary.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
