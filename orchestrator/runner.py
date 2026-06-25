"""Executa 1 run completo: warmup → reset → measure (pgbench + collector) → save.

Dois modos:
- LOCAL (cloud=None): pgbench via `docker compose exec postgres`, targets na rede
  docker, collector em localhost.
- CLOUD (cloud=CloudTargets): pgbench NATIVO na EC2 client apontando endpoints
  remotos (pooler EC2 / RDS), collector conecta endpoints remotos.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .configs import REPO_ROOT
from .matrix import Run
from .stack import reset_postgres_state

if TYPE_CHECKING:
    from .cloud_targets import CloudTargets

log = logging.getLogger(__name__)

# ---------------- LOCAL ----------------
# Conexão do collector (executa no host)
POSTGRES_DSN_HOST = "postgresql://tcc:tcc@localhost:5435/tcc"

POOLER_DSN_HOST = {
    "pgbouncer-tx":      "postgresql://tcc:tcc@localhost:6432/pgbouncer",
    "pgbouncer-session": "postgresql://tcc:tcc@localhost:6433/pgbouncer",
    "pgcat-tx":          "postgresql://pgcat_admin:pgcat_admin@localhost:6434/pgbouncer",
    "pgcat-session":     "postgresql://pgcat_admin:pgcat_admin@localhost:6435/pgbouncer",
}

# Hostname/porta DENTRO da rede docker (cliente pgbench roda no container postgres)
TARGET_BY_SETUP = {
    "none":              ("postgres", 5432),
    "pgbouncer-tx":      ("pgbouncer-tx", 6432),
    "pgbouncer-session": ("pgbouncer-session", 6432),
    "pgcat-tx":          ("pgcat-tx", 6432),
    "pgcat-session":     ("pgcat-session", 6432),
}

POOLER_TYPE_BY_SETUP = {
    "none":              None,
    "pgbouncer-tx":      "pgbouncer",
    "pgbouncer-session": "pgbouncer",
    "pgcat-tx":          "pgcat",
    "pgcat-session":     "pgcat",
    "rds-proxy":         None,   # gerenciado: sem admin DB (SHOW POOLS)
}

# Admin user/db por tipo de pooler (cloud — porta = a do próprio target).
_CLOUD_ADMIN_USER = {"pgbouncer": "tcc", "pgcat": "pgcat_admin"}
_CLOUD_ADMIN_PASS = {"pgbouncer": None, "pgcat": "pgcat_admin"}  # None → usa db_password


@dataclass
class RunResult:
    exp_id: str
    status: str                    # 'done', 'saturated', 'failed', 'skipped'
    started_at: str
    finished_at: str
    duration_s: float
    pgbench_exit_code: Optional[int] = None
    collector_exit_code: Optional[int] = None
    error: Optional[str] = None


# ============================================================
# Montagem do comando pgbench (local docker vs cloud nativo)
# ============================================================

def _pgbench_base(run: Run, host: str, port: int, user: str, db: str,
                  workload_path: str, log_pgbench: bool, log_prefix: str) -> list[str]:
    """Args do pgbench (sem wrapper docker). workload_path e log_prefix
    são passados explicitamente (diferem local docker vs cloud nativo)."""
    threads = min(run.clients, 8) if run.clients > 1 else 1
    args = [
        "pgbench",
        "-h", host, "-p", str(port),
        "-U", user, "-d", db,
        "-c", str(run.clients),
        "-j", str(threads),
        "-T", str(run.measure_s),
        "-M", run.mode,
        "-f", workload_path,
        "--no-vacuum",
    ]
    if log_pgbench:
        args.extend(["-l", "--aggregate-interval", "1", "--log-prefix", log_prefix])
    return args


def _pgbench_invocation(run: Run, exp_dir: Path, log_pgbench: bool,
                        cloud: Optional["CloudTargets"]) -> tuple[list[str], dict]:
    """Retorna (cmd, env) pro pgbench. Local = docker exec; cloud = nativo."""
    if cloud is None:
        host, port = TARGET_BY_SETUP[run.setup]
        base = _pgbench_base(
            run, host, port, "tcc", "tcc",
            workload_path=f"/workload/{run.workload}.sql",  # mount no container
            log_pgbench=log_pgbench,
            log_prefix=f"/data/{run.exp_id}/pgbench",        # path dentro do container
        )
        cmd = ["docker", "compose", "exec", "-T", "-e", "PGPASSWORD=tcc", "postgres", *base]
        return cmd, {}

    # Cloud: pgbench nativo na client. Workload e log-prefix são paths locais.
    host, port = cloud.target_host_port(run.setup)
    base = _pgbench_base(
        run, host, port, cloud.db_user, cloud.db_name,
        workload_path=str(REPO_ROOT / "workload" / f"{run.workload}.sql"),
        log_pgbench=log_pgbench,
        log_prefix=str(exp_dir / "pgbench"),
    )
    env = {**os.environ, "PGPASSWORD": cloud.db_password}
    return base, env


def _collector_cmd(run: Run, exp_dir: Path, cloud: Optional["CloudTargets"]) -> list[str]:
    """Monta o comando do collector (DSNs diferem local/cloud)."""
    label = ",".join(f"{k}={v}" for k, v in run.as_label_dict().items())

    if cloud is None:
        pg_dsn = POSTGRES_DSN_HOST
        pooler_dsn = POOLER_DSN_HOST.get(run.setup)
    else:
        pg_dsn = f"postgresql://{cloud.db_user}:{cloud.db_password}@{cloud.rds_endpoint}:5432/{cloud.db_name}"
        pooler_dsn = _cloud_pooler_admin_dsn(run.setup, cloud)

    cmd = [
        sys.executable, "-m", "collector",
        "--dsn", pg_dsn,
        "--interval", "1",
        "--duration", str(run.measure_s),
        "--output", str(exp_dir),
        "--label", label,
    ]
    pooler_type = POOLER_TYPE_BY_SETUP.get(run.setup)
    if pooler_dsn and pooler_type:
        cmd.extend(["--pooler-dsn", pooler_dsn, "--pooler-type", pooler_type])
    return cmd


def _cloud_pooler_admin_dsn(setup: str, cloud: "CloudTargets") -> Optional[str]:
    """DSN do admin DB do pooler em cloud (None pra none/rds-proxy)."""
    ptype = POOLER_TYPE_BY_SETUP.get(setup)
    if not ptype:
        return None
    host, port = cloud.target_host_port(setup)
    user = _CLOUD_ADMIN_USER[ptype]
    pwd = _CLOUD_ADMIN_PASS[ptype] or cloud.db_password
    return f"postgresql://{user}:{pwd}@{host}:{port}/pgbouncer"


def _kill_zombies(cloud: Optional["CloudTargets"]) -> None:
    """Mata pgbench órfãos. Local: dentro do container postgres. Cloud: na client."""
    if cloud is None:
        subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "pkill", "-9", "pgbench"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
    else:
        subprocess.run(["pkill", "-9", "pgbench"], capture_output=True, text=True, check=False)


def _phase_timeout(phase_s: int, clients: int) -> float:
    """Timeout do subprocess pgbench/collector pra uma fase de `phase_s` segundos.

    A folga (`+30`) é fixa pra setup/shutdown; o termo `clients*0.15` cobre dois
    custos que crescem com a concorrência:
      1. ABERTURA das conexões (medido: ~7s p/ 1000 clientes; modo prepared
         prepara o statement em cada conexão de servidor). O connect NÃO
         contamina a métrica (pgbench reporta TPS 'without initial connection
         time') — mas se o harness mata antes, perde-se o run inteiro.
      2. COLAPSO sob saturação: quando o caminho satura, o pgbench luta contra
         os timeouts internos do pooler (query_wait_timeout=120s no PgBouncer,
         connection_borrow_timeout=120s no RDS Proxy) antes de abortar. Medido:
         write/prepared/pool100/c=1000 leva ~147s até retornar AllServersDown.
    O timeout do harness precisa ser MAIOR que esse colapso pra que a saturação
    se manifeste como erro classificável (→ status 'saturated') e não como
    'pgbench timeout' (que mascararia o fenômeno). c=1000 → +150s; c=1 → +30s
    (a c baixa não há fila/colapso, então 30s basta pra detectar hang real).
    """
    return phase_s + 30 + clients * 0.15


def _warmup(run: Run, cloud: Optional["CloudTargets"]) -> int:
    """pgbench breve sem coletor pra aquecer cache. Retorna exit code.

    Tolera timeout/saturação: se o caminho JÁ satura no warmup (a fila do pooler
    estoura antes do timeout do harness do warmup), NÃO propaga exceção — mata os
    zombies e retorna -1, deixando o MEASURE seguir. O measure tem timeout maior
    (`measure_s=60` vs `warmup_s=30` → +30s de folga) que cruza o
    `query_wait_timeout=120s` do PgBouncer, então a saturação se manifesta lá como
    assinatura classificável (→ 'saturated'). Sem isso, nas concorrências
    intermediárias (c<400, onde o timeout do warmup `30+30+c*0.15` < 120s) o
    warmup-timeout derrubava o run inteiro como 'failed'. Anomalia de infra REAL
    continua virando 'failed' (o measure também quebra, sem assinatura). Ver
    _phase_timeout."""
    # Reusa _pgbench_invocation com measure_s=warmup_s e sem log.
    warm = Run(**{**asdict(run), "measure_s": run.warmup_s})
    cmd, env = _pgbench_invocation(warm, Path("/tmp"), log_pgbench=False, cloud=cloud)
    log.info("Warmup: %ds, %dc, %s", run.warmup_s, run.clients, run.workload)
    try:
        res = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=_phase_timeout(run.warmup_s, run.clients), check=False, env=env or None,
        )
    except subprocess.TimeoutExpired:
        log.warning("warmup TIMEOUT — caminho saturado; matando zombie e seguindo p/ measure")
        _kill_zombies(cloud)
        return -1
    if res.returncode != 0:
        log.warning("warmup retornou %d: %s", res.returncode, res.stderr.strip()[:200])
    return res.returncode


def _start_collector(run: Run, exp_dir: Path, cloud: Optional["CloudTargets"]) -> subprocess.Popen:
    cmd = _collector_cmd(run, exp_dir, cloud)
    log.debug("Collector cmd: %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.Popen(
        cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _start_pgbench(run: Run, exp_dir: Path, cloud: Optional["CloudTargets"]) -> subprocess.Popen:
    cmd, env = _pgbench_invocation(run, exp_dir, log_pgbench=run.pgbench_log, cloud=cloud)
    log.debug("pgbench cmd: %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.Popen(
        cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env or None,
    )


def _ensure_writable(exp_dir: Path):
    """Garante exp_dir gravável. Local: 777 (container postgres uid). Cloud: default."""
    exp_dir.mkdir(parents=True, exist_ok=True)
    exp_dir.chmod(0o777)


# Assinaturas de SATURAÇÃO no stderr do pgbench: o caminho (pooler/banco) recusou
# conexões/queries sob a carga. NÃO é bug do harness — é o COLAPSO que o TCC quer
# medir (o "lado de cima" do break-even). Cada string é o erro que o respectivo
# componente emite quando a fila/pool estoura sob alta concorrência.
_SATURATION_SIGNATURES = (
    "query_wait_timeout",                                 # PgBouncer: fila estourou
    "could not get connection from the pool",            # PgCat
    "AllServersDown",                                    # PgCat: backends banidos sob carga
    "Timed-out waiting to acquire database connection",  # RDS Proxy: borrow timeout
    "server_login_retry",                                # PgBouncer: burst de logins
)

_PROCESSED_RE = re.compile(r"actually processed:\s*(\d+)")


def _classify_status(pgbench_rc: Optional[int], collector_rc: Optional[int],
                     error: Optional[str], pgbench_out: str, pgbench_err: str) -> str:
    """Classifica um run em done / saturated / failed.

    - done:      pgbench e collector OK, sem erro do harness, e TPS medido com
                 transações > 0.
    - saturated: o caminho COLAPSOU sob a carga — resultado VÁLIDO p/ o TCC,
                 não falha. Três manifestações:
                 (a) assinatura de saturação no stderr do pooler;
                 (b) 0 transações processadas (conectou mas a carga impediu
                     qualquer commit);
                 (c) "colapso mudo" — o pgbench congela tão forte sob a carga que
                     o harness o mata por timeout (rc=-1, "pgbench timeout") SEM
                     ele emitir stdout/stderr, MAS o collector completou normal
                     (collector_rc=0). Se o collector rodou os ~20s e saiu OK, o
                     banco estava vivo respondendo o tempo todo → o que travou foi
                     o pgbench contra o pooler saturado, não anomalia de infra.
                     O dado server-side (pg_stat_*, admin do pooler) FOI coletado.
                 Conta como "coberto".
    - failed:    qualquer outra coisa — timeout do harness COM collector também
                 quebrado (anomalia de infra), erro inesperado. Precisa rerun.
    """
    blob = (pgbench_err or "") + (pgbench_out or "")
    saturated_sig = any(s in blob for s in _SATURATION_SIGNATURES)

    m = _PROCESSED_RE.search(pgbench_out or "")
    zero_txns = m is not None and int(m.group(1)) == 0

    if pgbench_rc == 0 and collector_rc == 0 and not error and not zero_txns:
        return "done"

    # (a) assinatura explícita de pooler; (b) pgbench terminou por conta própria
    # (rc não-nulo, NÃO morto pelo harness) reportando 0 transações.
    if saturated_sig or (zero_txns and pgbench_rc is not None and pgbench_rc >= 0):
        return "saturated"

    # (c) colapso mudo: harness matou o pgbench por timeout, mas o collector
    # completou OK → banco vivo, pooler saturado. Distingue de anomalia de infra
    # (onde o collector TAMBÉM falharia, caindo em 'failed' abaixo).
    timed_out = pgbench_rc == -1 and bool(error) and "pgbench timeout" in error
    if timed_out and collector_rc == 0:
        return "saturated"

    return "failed"


def execute(run: Run, output_root: Path, cloud: Optional["CloudTargets"] = None) -> RunResult:
    """Executa 1 run end-to-end. cloud=None → local docker; cloud setado → AWS.

    Skip de runs já feitos é responsabilidade do caller (via manifest).
    """
    import shutil

    exp_dir = output_root / run.exp_id
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    log.info("RUN: %s", run.exp_id)
    shutil.rmtree(exp_dir, ignore_errors=True)
    _ensure_writable(exp_dir)
    _kill_zombies(cloud)

    pgbench_rc: Optional[int] = None
    collector_rc: Optional[int] = None
    error: Optional[str] = None
    pgbench_out: str = ""
    pgbench_err: str = ""

    # DSN pro reset de stats: local = Postgres docker; cloud = RDS remoto.
    reset_dsn = (
        POSTGRES_DSN_HOST if cloud is None
        else f"postgresql://{cloud.db_user}:{cloud.db_password}@{cloud.rds_endpoint}:5432/{cloud.db_name}"
    )

    try:
        # 1. Warmup
        _warmup(run, cloud)
        # 2. Reset entre warmup e measure
        reset_postgres_state(reset_dsn)
        # 3. Measure: collector + pgbench em paralelo
        collector_proc = _start_collector(run, exp_dir, cloud)
        time.sleep(0.5)
        pgbench_proc = _start_pgbench(run, exp_dir, cloud)

        measure_timeout = _phase_timeout(run.measure_s, run.clients)
        try:
            pgbench_out, pgbench_err = pgbench_proc.communicate(timeout=measure_timeout)
            pgbench_rc = pgbench_proc.returncode
        except subprocess.TimeoutExpired:
            log.warning("pgbench (measure) TIMEOUT — matando subprocess + zombies")
            pgbench_proc.kill()
            pgbench_out, pgbench_err = pgbench_proc.communicate()
            _kill_zombies(cloud)
            pgbench_rc = -1
            error = (error or "") + "pgbench timeout; "

        try:
            collector_out, collector_err = collector_proc.communicate(timeout=measure_timeout)
            collector_rc = collector_proc.returncode
        except subprocess.TimeoutExpired:
            collector_proc.kill()
            collector_out, collector_err = collector_proc.communicate()
            collector_rc = -1
            error = (error or "") + "collector timeout; "

        # 4. Persistir output
        (exp_dir / "pgbench_stdout.log").write_text(pgbench_out or "")
        if pgbench_err:
            (exp_dir / "pgbench_stderr.log").write_text(pgbench_err)
        if collector_err:
            (exp_dir / "collector_stderr.log").write_text(collector_err)

    except Exception as e:
        log.exception("Falha em %s: %s", run.exp_id, e)
        error = str(e)

    finished_at = datetime.now(timezone.utc).isoformat()
    duration = time.monotonic() - t0

    status = _classify_status(pgbench_rc, collector_rc, error, pgbench_out, pgbench_err)

    meta = {
        "exp_id": run.exp_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": round(duration, 2),
        "pgbench_exit_code": pgbench_rc,
        "collector_exit_code": collector_rc,
        "error": error,
        "mode": "cloud" if cloud is not None else "local",
        "run": {**asdict(run), "pool_size": run.pool_size},
    }
    (exp_dir / "run_meta.json").write_text(json.dumps(meta, indent=2))

    log.info("RUN END: %s status=%s duration=%.1fs", run.exp_id, status, duration)
    return RunResult(
        exp_id=run.exp_id, status=status,
        started_at=started_at, finished_at=finished_at, duration_s=duration,
        pgbench_exit_code=pgbench_rc, collector_exit_code=collector_rc, error=error,
    )
