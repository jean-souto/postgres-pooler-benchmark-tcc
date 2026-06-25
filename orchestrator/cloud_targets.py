"""Descoberta de endpoints da infra cloud — lidos do ambiente da EC2 client.

O orquestrador roda NA EC2 client (pgbench nativo precisa estar perto do
pooler/RDS). O tfstate fica no laptop de quem rodou `terraform apply`, NÃO na
client — então não dá pra usar `terraform output` aqui. Em vez disso, o
user_data da client (client.sh.tftpl) injeta todos os endpoints em
`~/.tcc/env`, que o .bashrc carrega. Esta função lê dessas env vars.

Vars esperadas (populadas pelo Terraform via templatefile):
  TCC_DB_ENDPOINT, TCC_DB_USER, TCC_DB_PASSWORD, TCC_DB_NAME,
  TCC_POOLER_HOST, TCC_RDS_PROXY_ENDPOINT (vazio se proxy desligado).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# Portas fixas dos poolers na EC2 pooler (host network). Batem com
# pooler-stack/templates/*.tmpl, SG (main.tf for_each) e o orquestrador.
_POOLER_PORTS = {
    "pgbouncer-tx": 6432,
    "pgbouncer-session": 6433,
    "pgcat-tx": 6434,
    "pgcat-session": 6435,
}


@dataclass
class CloudTargets:
    """Endpoints resolvidos da infra cloud (lidos de ~/.tcc/env)."""
    targets: dict[str, Optional[str]]   # setup -> "host:porta" (None se indisponível)
    rds_endpoint: str
    rds_proxy_endpoint: Optional[str]
    pooler_private_ip: str
    pooler_ssh_alias: str               # alias no ~/.ssh/config da client ("pooler")
    db_user: str
    db_password: str
    db_name: str

    def target_host_port(self, setup: str) -> tuple[str, int]:
        """Retorna (host, port) pro pgbench de um setup. Lança se indisponível."""
        t = self.targets.get(setup)
        if not t:
            raise ValueError(f"target indisponível pro setup '{setup}'")
        host, port = t.rsplit(":", 1)
        return host, int(port)


def load_cloud_targets() -> CloudTargets:
    """Lê endpoints/credenciais do ambiente (~/.tcc/env via .bashrc).

    Falha cedo e claro se faltar var essencial.
    """
    def need(key: str) -> str:
        v = os.environ.get(key)
        if not v:
            raise RuntimeError(
                f"{key} ausente no ambiente. Carregue ~/.tcc/env "
                "(`set -a; . ~/.tcc/env; set +a`) — populado pelo user_data da EC2 client."
            )
        return v

    rds = need("TCC_DB_ENDPOINT")
    pooler = need("TCC_POOLER_HOST")
    user = os.environ.get("TCC_DB_USER", "tcc")
    pwd = need("TCC_DB_PASSWORD")
    name = os.environ.get("TCC_DB_NAME", "tcc")
    proxy = os.environ.get("TCC_RDS_PROXY_ENDPOINT") or None

    targets: dict[str, Optional[str]] = {"none": f"{rds}:5432"}
    for setup, port in _POOLER_PORTS.items():
        targets[setup] = f"{pooler}:{port}"
    targets["rds-proxy"] = f"{proxy}:5432" if proxy else None

    return CloudTargets(
        targets=targets,
        rds_endpoint=rds,
        rds_proxy_endpoint=proxy,
        pooler_private_ip=pooler,
        pooler_ssh_alias="pooler",
        db_user=user,
        db_password=pwd,
        db_name=name,
    )
