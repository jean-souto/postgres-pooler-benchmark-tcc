"""Gerenciamento do docker compose: up/down + healthcheck wait.

Usa docker-compose.override.yml gerado por configs.py quando aplicável.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from .configs import (
    COMPOSE_OVERRIDE, REPO_ROOT, POOLER_STACK_DIR, render_compose_override,
)

log = logging.getLogger(__name__)

# Setups que NÃO sobem pooler (target direto): controle + gerenciado.
NO_POOLER_SETUPS = {"none", "rds-proxy"}

PROFILES_BY_SETUP = {
    "none": [],
    "pgbouncer-tx": ["--profile", "pgbouncer-tx"],
    "pgbouncer-session": ["--profile", "pgbouncer-session"],
    "pgcat-tx": ["--profile", "pgcat-tx"],
    "pgcat-session": ["--profile", "pgcat-session"],
}

# Service que precisa estar healthy pra considerar setup pronto
POOLER_SERVICE = {
    "none": None,
    "pgbouncer-tx": "pgbouncer-tx",
    "pgbouncer-session": "pgbouncer-session",
    "pgcat-tx": "pgcat-tx",
    "pgcat-session": "pgcat-session",
}


def _compose_cmd(setup: str, *extra: str) -> list[str]:
    """Monta `docker compose [-f override] [--profile X] <extra...>`."""
    cmd = ["docker", "compose", "-f", "docker-compose.yml"]
    if COMPOSE_OVERRIDE.exists():
        cmd.extend(["-f", str(COMPOSE_OVERRIDE.relative_to(REPO_ROOT))])
    cmd.extend(PROFILES_BY_SETUP[setup])
    cmd.extend(extra)
    return cmd


def _run(cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    log.debug("EXEC: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=capture,
        text=True,
        check=False,
    )


def up(setup: str, pool_size: Optional[int], healthcheck_timeout_s: int = 60) -> None:
    """Sobe stack pro setup dado, aguarda healthcheck.

    Raises:
        RuntimeError se docker compose falha ou healthcheck timeout.
    """
    log.info("Subindo stack: setup=%s pool_size=%s", setup, pool_size)
    render_compose_override(setup, pool_size)

    res = _run(_compose_cmd(setup, "up", "-d"))
    if res.returncode != 0:
        raise RuntimeError(f"docker compose up falhou: {res.stderr}")

    services = ["postgres"]
    pooler = POOLER_SERVICE[setup]
    if pooler:
        services.append(pooler)
    _wait_healthy(setup, services, healthcheck_timeout_s)
    log.info("Stack pronta (setup=%s, services=%s)", setup, services)


def down(setup: str, remove_volumes: bool = False) -> None:
    """Derruba stack. Por default preserva volume pgdata pra reuso entre runs."""
    log.info("Derrubando stack: setup=%s", setup)
    extra = ["down"]
    if remove_volumes:
        extra.append("-v")
    res = _run(_compose_cmd(setup, *extra))
    if res.returncode != 0:
        log.warning("docker compose down retornou %d: %s", res.returncode, res.stderr)


def _wait_healthy(setup: str, services: list[str], timeout_s: int) -> None:
    """Poll docker compose ps até todos os services healthy."""
    deadline = time.monotonic() + timeout_s
    last_status: dict = {}
    while time.monotonic() < deadline:
        res = _run(_compose_cmd(setup, "ps", "--format", "json"))
        if res.returncode != 0:
            time.sleep(1)
            continue

        # docker compose ps --format json emite uma linha JSON por service
        statuses = {}
        for line in res.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                obj = json.loads(line)
                statuses[obj["Service"]] = obj.get("Health") or obj.get("State")
            except (json.JSONDecodeError, KeyError):
                continue

        last_status = statuses
        unhealthy = [s for s in services if statuses.get(s) != "healthy"]
        if not unhealthy:
            return
        time.sleep(2)

    raise RuntimeError(
        f"healthcheck timeout após {timeout_s}s. Esperado healthy: {services}. "
        f"Último estado: {last_status}"
    )


def reset_postgres_state(dsn: str) -> None:
    """Reset de pg_stat_statements e pg_stat_io (entre runs)."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for stmt in (
            "SELECT pg_stat_statements_reset();",
            "SELECT pg_stat_reset_shared('io');",
        ):
            try:
                cur.execute(stmt)
            except Exception as e:
                log.warning("Falha em %s: %s", stmt, e)


# ============================================================
# CLOUD: controla a pooler-stack na EC2 pooler via SSH (decisão 2b).
# Tudo roda NO pooler (compose file, bind mounts, configs locais a ele).
# O canal SSH é só controle — o caminho de dados (pgbench→pooler→RDS) não passa.
# ============================================================

_CLOUD_COMPOSE = "docker-compose.cloud.yml"
_CLOUD_STACK_DIR = "/opt/pooler-stack"


def _ssh(alias: str, remote_cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Roda um comando no host pooler via SSH (alias do ~/.ssh/config)."""
    return subprocess.run(
        ["ssh", alias, remote_cmd],
        capture_output=True, text=True, check=False, timeout=timeout,
    )


def rsync_pooler_stack(alias: str) -> None:
    """Envia infra-aws/pooler-stack/ (com generated/ + userlist.txt) pro pooler.

    Chamado uma vez no início da bateria, após render_cloud_configs().
    """
    src = f"{POOLER_STACK_DIR}/"
    dst = f"{alias}:{_CLOUD_STACK_DIR}/"
    log.info("rsync pooler-stack → %s", dst)
    res = subprocess.run(
        ["rsync", "-az", "--delete",
         "-e", "ssh -o StrictHostKeyChecking=no",
         src, dst],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"rsync pooler-stack falhou: {res.stderr}")


def up_cloud(setup: str, pool_size: Optional[int], alias: str,
             healthcheck_timeout_s: int = 60) -> None:
    """Sobe um pooler na EC2 pooler via SSH. none/rds-proxy: no-op (target direto)."""
    if setup in NO_POOLER_SETUPS:
        log.info("setup=%s não usa pooler (target direto)", setup)
        return

    ps = pool_size if pool_size is not None else 8
    cmd = (
        f"cd {_CLOUD_STACK_DIR} && "
        f"POOL_SIZE={ps} docker compose -f {_CLOUD_COMPOSE} --profile {setup} up -d"
    )
    log.info("up_cloud: setup=%s pool_size=%s", setup, ps)
    res = _ssh(alias, cmd)
    if res.returncode != 0:
        raise RuntimeError(f"up_cloud falhou ({setup}): {res.stderr.strip()}")
    _wait_healthy_cloud(setup, alias, healthcheck_timeout_s)


def down_cloud(setup: str, alias: str) -> None:
    """Derruba o pooler na EC2 pooler. none/rds-proxy: no-op."""
    if setup in NO_POOLER_SETUPS:
        return
    cmd = (
        f"cd {_CLOUD_STACK_DIR} && "
        f"docker compose -f {_CLOUD_COMPOSE} --profile {setup} down"
    )
    res = _ssh(alias, cmd)
    if res.returncode != 0:
        log.warning("down_cloud %s retornou %d: %s", setup, res.returncode, res.stderr)


def _wait_healthy_cloud(setup: str, alias: str, timeout_s: int) -> None:
    """Poll docker inspect no pooler até o container ficar healthy."""
    container = f"pooler-{setup}"
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        res = _ssh(
            alias,
            f"docker inspect --format '{{{{.State.Health.Status}}}}' {container} 2>/dev/null",
        )
        last = res.stdout.strip()
        if last == "healthy":
            log.info("pooler %s healthy", setup)
            return
        time.sleep(2)
    raise RuntimeError(
        f"healthcheck cloud timeout após {timeout_s}s ({container}). Último: '{last}'"
    )


def vacuum_checkpoint(dsn: str) -> None:
    """VACUUM ANALYZE + CHECKPOINT no RDS — estado consistente entre setups.

    Mitigação de validade: evita que cache quente / dead tuples de um setup
    contaminem o próximo. Chamado ao trocar de setup (não por run).
    """
    import psycopg
    tables = "pgbench_accounts, pgbench_branches, pgbench_tellers, pgbench_history"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            log.info("VACUUM ANALYZE + CHECKPOINT (entre setups)")
            cur.execute(f"VACUUM (ANALYZE) {tables};")
            cur.execute("CHECKPOINT;")
    except Exception as e:
        log.warning("vacuum_checkpoint falhou (segue mesmo assim): %s", e)


def pgbench_init_if_needed(dsn: str, scale: int, cloud=None) -> bool:
    """Inicializa pgbench se ainda não houver tabelas. Retorna True se inicializou.

    Local (cloud=None): pgbench -i via `docker compose exec postgres`.
    Cloud (cloud=CloudTargets): pgbench -i NATIVO na client apontando o RDS.
    """
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='pgbench_accounts';"
        )
        exists = cur.fetchone()[0] > 0

    if exists:
        log.info("pgbench tables já existem — pulando init")
        return False

    log.info("Inicializando pgbench (scale=%d, mode=%s)", scale, "cloud" if cloud else "local")
    if cloud is None:
        cmd = ["docker", "compose", "exec", "-T", "-e", "PGPASSWORD=tcc", "postgres",
               "pgbench", "-h", "postgres", "-p", "5432", "-U", "tcc", "-d", "tcc",
               "-i", "-s", str(scale)]
        env = None
    else:
        import os
        cmd = ["pgbench", "-h", cloud.rds_endpoint, "-p", "5432",
               "-U", cloud.db_user, "-d", cloud.db_name, "-i", "-s", str(scale)]
        env = {**os.environ, "PGPASSWORD": cloud.db_password}

    res = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                         check=False, env=env)
    if res.returncode != 0:
        raise RuntimeError(f"pgbench init falhou: {res.stderr}")
    return True
