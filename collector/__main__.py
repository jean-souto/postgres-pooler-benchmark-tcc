"""CLI do collector.

Uso:
    python -m collector \\
        --dsn "postgresql://tcc:tcc@localhost:5435/tcc" \\
        --interval 1 \\
        --duration 60 \\
        --output data/exp001 \\
        --label "setup=none,workload=read,clients=10"
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .sampler import SamplerConfig, run


def parse_label(s: str) -> dict:
    """Converte 'k1=v1,k2=v2' em dict."""
    if not s:
        return {}
    out = {}
    for pair in s.split(","):
        if "=" not in pair:
            raise ValueError(f"Label inválido (esperado k=v): {pair!r}")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collector", description="Sampler server-side PostgreSQL")
    parser.add_argument("--dsn", default="postgresql://tcc:tcc@localhost:5435/tcc",
                        help="DSN do Postgres (default: localhost:5435/tcc)")
    parser.add_argument("--interval", type=float, default=1.0, help="Intervalo entre amostras (s)")
    parser.add_argument("--duration", type=float, default=60.0, help="Duração total (s)")
    parser.add_argument("--output", type=Path, required=True, help="Pasta de saída")
    parser.add_argument("--label", default="", help="Metadata 'k1=v1,k2=v2' que vira coluna nos CSVs")
    parser.add_argument("--reset-stats", action="store_true",
                        help="Executa pg_stat_statements_reset() e pg_stat_reset_shared('io') antes do loop")
    parser.add_argument("--pooler-dsn", default=None,
                        help="DSN do pooler (PgBouncer admin DB 'pgbouncer' ou Pgpool DB qualquer). Se omitido, não amostra pooler.")
    parser.add_argument("--pooler-type", default=None, choices=["pgbouncer", "pgcat", "pgpool"],
                        help="Tipo do pooler — define quais SHOW commands usar. Obrigatório se --pooler-dsn.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    if args.pooler_dsn and not args.pooler_type:
        parser.error("--pooler-dsn requer --pooler-type")

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg = SamplerConfig(
        dsn=args.dsn,
        output_dir=args.output,
        interval_s=args.interval,
        duration_s=args.duration,
        label=parse_label(args.label),
        reset_stats=args.reset_stats,
        pooler_dsn=args.pooler_dsn,
        pooler_type=args.pooler_type,
    )
    run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
