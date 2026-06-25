"""Renderiza configs parametrizadas (PgBouncer ini, PgCat toml, override docker-compose).

Os arquivos originais (pgbouncer/pgbouncer-transaction.ini, pgcat/pgcat-tx.toml, etc)
servem de TEMPLATE. Este módulo gera variantes com pool_size customizado em
pgbouncer/generated/ e pgcat/generated/, plus um docker-compose.override.yml em
.orchestrator/ que aponta os mounts pros arquivos gerados. Tudo limpável.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
PGBOUNCER_DIR = REPO_ROOT / "pgbouncer"
PGBOUNCER_GENERATED = PGBOUNCER_DIR / "generated"
PGCAT_DIR = REPO_ROOT / "pgcat"
PGCAT_GENERATED = PGCAT_DIR / "generated"
COMPOSE_OVERRIDE = REPO_ROOT / ".orchestrator" / "docker-compose.override.yml"

# Cloud: pooler-stack vive no infra-aws/. O orquestrador gera generated/ +
# userlist.txt aqui e faz rsync pra EC2 pooler.
POOLER_STACK_DIR = REPO_ROOT / "infra-aws" / "pooler-stack"
POOLER_STACK_TEMPLATES = POOLER_STACK_DIR / "templates"
POOLER_STACK_GENERATED = POOLER_STACK_DIR / "generated"

# Extensão do arquivo de config por setup (cloud).
_CLOUD_SETUP_EXT = {
    "pgbouncer-tx": "ini",
    "pgbouncer-session": "ini",
    "pgcat-tx": "toml",
    "pgcat-session": "toml",
}


def _substitute_pool_size(content: str, pool_size: int) -> str:
    """Substitui 'default_pool_size = N' pelo valor desejado."""
    pattern = re.compile(r"^(\s*default_pool_size\s*=\s*)\d+", re.MULTILINE)
    new, n = pattern.subn(rf"\g<1>{pool_size}", content)
    if n == 0:
        raise ValueError("default_pool_size não encontrado no template")
    return new


def render_pgbouncer_ini(setup: str, pool_size: int) -> Path:
    """Gera pgbouncer/generated/{setup}-pool{N}.ini a partir do template.

    Returns: caminho do arquivo gerado.
    """
    if setup == "pgbouncer-tx":
        template = PGBOUNCER_DIR / "pgbouncer-transaction.ini"
    elif setup == "pgbouncer-session":
        template = PGBOUNCER_DIR / "pgbouncer-session.ini"
    else:
        raise ValueError(f"setup PgBouncer inválido: {setup}")

    PGBOUNCER_GENERATED.mkdir(parents=True, exist_ok=True)
    content = template.read_text()
    new_content = _substitute_pool_size(content, pool_size)

    out = PGBOUNCER_GENERATED / f"{setup}-pool{pool_size}.ini"
    out.write_text(new_content)
    return out


def _substitute_pool_size_toml(content: str, pool_size: int) -> str:
    """Substitui o placeholder __POOL_SIZE__ no template TOML do PgCat."""
    if "__POOL_SIZE__" not in content:
        raise ValueError("__POOL_SIZE__ placeholder não encontrado no template")
    return content.replace("__POOL_SIZE__", str(pool_size))


def render_pgcat_toml(setup: str, pool_size: int) -> Path:
    """Gera pgcat/generated/{setup}-pool{N}.toml a partir do template.

    setup: 'pgcat-tx' ou 'pgcat-session'.
    Returns: caminho do arquivo gerado.
    """
    if setup == "pgcat-tx":
        template = PGCAT_DIR / "pgcat-tx.toml"
    elif setup == "pgcat-session":
        template = PGCAT_DIR / "pgcat-session.toml"
    else:
        raise ValueError(f"setup PgCat inválido: {setup}")

    PGCAT_GENERATED.mkdir(parents=True, exist_ok=True)
    content = template.read_text()
    new_content = _substitute_pool_size_toml(content, pool_size)

    out = PGCAT_GENERATED / f"{setup}-pool{pool_size}.toml"
    out.write_text(new_content)
    return out


def render_compose_override(setup: str, pool_size: Optional[int]) -> Optional[Path]:
    """Gera docker-compose.override.yml com configs parametrizadas.

    Pra setup=none, retorna None (não há override).
    Pra pgbouncer-*, override aponta pro .ini gerado.
    Pra pgcat-*, override aponta pro .toml gerado.
    """
    if setup == "none":
        # Garante que override antigo não vaza
        if COMPOSE_OVERRIDE.exists():
            COMPOSE_OVERRIDE.unlink()
        return None

    COMPOSE_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)

    if setup in ("pgbouncer-tx", "pgbouncer-session"):
        ini_path = render_pgbouncer_ini(setup, pool_size)
        ini_rel = ini_path.relative_to(REPO_ROOT)
        content = (
            "services:\n"
            f"  {setup}:\n"
            "    volumes:\n"
            f"      - ./{ini_rel}:/etc/pgbouncer/pgbouncer.ini:ro\n"
            "      - ./pgbouncer/userlist.txt:/etc/pgbouncer/userlist.txt:ro\n"
        )
    elif setup in ("pgcat-tx", "pgcat-session"):
        toml_path = render_pgcat_toml(setup, pool_size)
        toml_rel = toml_path.relative_to(REPO_ROOT)
        content = (
            "services:\n"
            f"  {setup}:\n"
            "    volumes:\n"
            f"      - ./{toml_rel}:/etc/pgcat/pgcat.toml:ro\n"
        )
    else:
        raise ValueError(f"Setup desconhecido: {setup}")

    COMPOSE_OVERRIDE.write_text(content)
    return COMPOSE_OVERRIDE


# ============================================================
# CLOUD: gera generated/ + userlist.txt da pooler-stack (rsync pro pooler)
# ============================================================

def _pgbouncer_md5(user: str, password: str) -> str:
    """Hash no formato do userlist do PgBouncer: md5<md5(password+user)>."""
    return "md5" + hashlib.md5((password + user).encode()).hexdigest()


def render_cloud_configs(
    rds_endpoint: str,
    db_user: str,
    db_password: str,
    pool_sizes: list[int] | None = None,
) -> Path:
    """Gera infra-aws/pooler-stack/generated/ + userlist.txt pra rsync no pooler.

    Pra cada pooler self-hosted × pool_size, substitui __RDS_ENDPOINT__ e
    __POOL_SIZE__ no template correspondente. O userlist.txt usa o hash MD5 da
    senha REAL do RDS (db_password ≥12 chars no cloud, != 'tcc' do local).

    Returns: POOLER_STACK_GENERATED (dir a ser rsynced).
    """
    pool_sizes = pool_sizes or [2, 8, 100]
    POOLER_STACK_GENERATED.mkdir(parents=True, exist_ok=True)

    for setup, ext in _CLOUD_SETUP_EXT.items():
        template = POOLER_STACK_TEMPLATES / f"{setup}.{ext}.tmpl"
        base = template.read_text()
        for ps in pool_sizes:
            content = (
                base.replace("__RDS_ENDPOINT__", rds_endpoint)
                    .replace("__POOL_SIZE__", str(ps))
                    .replace("__DB_PASSWORD__", db_password)
            )
            out = POOLER_STACK_GENERATED / f"{setup}-pool{ps}.{ext}"
            out.write_text(content)

    # userlist.txt na raiz da pooler-stack (montado pelos serviços pgbouncer).
    userlist = POOLER_STACK_DIR / "userlist.txt"
    userlist.write_text(f'"{db_user}" "{_pgbouncer_md5(db_user, db_password)}"\n')

    return POOLER_STACK_GENERATED


def cleanup():
    """Remove tudo o que foi gerado em runtime (local; cloud generated fica)."""
    for d in (PGBOUNCER_GENERATED, PGCAT_GENERATED):
        if d.exists():
            shutil.rmtree(d)
    if COMPOSE_OVERRIDE.exists():
        COMPOSE_OVERRIDE.unlink()
    if COMPOSE_OVERRIDE.parent.exists():
        try:
            COMPOSE_OVERRIDE.parent.rmdir()
        except OSError:
            pass  # diretório não vazio, OK
