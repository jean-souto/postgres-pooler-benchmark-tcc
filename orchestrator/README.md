# Orchestrator

Executa baterias de experimentos definidas em `experiments.yaml`. Cada run:
**warmup → reset stats → measure (pgbench + collector paralelos) → save**.

## Setup

```bash
cd /home/jean/Documentos/TCC/Pratica
source .venv/bin/activate
pip install -r orchestrator/requirements.txt
```

Pré-requisitos: Docker rodando, Postgres na 5433 acessível.

## Uso

```bash
# Smoke (~27min): valida pipeline em 36 runs cobrindo 3 setups + 2 workloads
python -m orchestrator --preset smoke

# Cirúrgica (~26h): 900 runs com varredura útil pra resultados
python -m orchestrator --preset cirurgica

# Full (~79h): 2700 runs cobertura completa
python -m orchestrator --preset full

# Debug: roda só os 5 primeiros runs (após shuffle)
python -m orchestrator --preset smoke --limit 5

# Re-roda só os runs com status=failed (após bateria)
python -m orchestrator --preset cirurgica --rerun-failed

# Força re-execução de TUDO (ignora skip-if-done)
python -m orchestrator --preset smoke --rerun-all
```

## Estrutura de output

```
data/
├── manifest.csv                  # estado de TODOS os runs
├── {exp_id}/                     # um diretório por run
│   ├── config.json
│   ├── pg_stat_activity.csv      # 1 linha por backend por amostra
│   ├── pg_locks.csv
│   ├── pg_stat_statements.csv    # cumulativo (calcular delta)
│   ├── pg_stat_io.csv            # cumulativo (calcular delta)
│   ├── pgbouncer_show_pools.csv  # se setup=pgbouncer-*
│   ├── pgbouncer_show_stats.csv  # idem
│   ├── pgpool_show_pool_nodes.csv      # se setup=pgpool
│   ├── pgpool_show_pool_processes.csv  # idem
│   ├── pgbench.<pid>             # log agregado (1 linha/segundo)
│   ├── pgbench.<pid>.<thread>    # idem por thread
│   ├── pgbench_stdout.log        # output bruto pgbench
│   └── run_meta.json             # status, exit codes, timing
```

## exp_id

Determinístico: `{setup}_{workload}_{mode}_pool{N}_c{clients}_r{rep}`.

Exemplos:
- `none_read_simple_nopool_c50_r1` (sem pooler — não tem pool_size)
- `pgbouncer-tx_write_prepared_pool8_c100_r3`
- `pgpool_mixed_simple_pool32_c500_r2`

## Comportamentos esperados que viram "failed" no manifest

**`setup=none` + clients > max_connections (~415 em db.t3.medium)**: pgbench
emite "FATAL: sorry, too many clients already". Marca como **failed** mas é
**evidência cientificamente relevante** — prova que sem pooler o sistema rejeita.
Documente na análise; não é bug.

**Race em healthcheck após troca rápida de stack**: raro, mas pode acontecer
se docker daemon estiver lento. Use `--rerun-failed` pra retry seletivo.

## Manifest schema (manifest.csv)

| Coluna | Conteúdo |
|---|---|
| `exp_id` | identificador único |
| `status` | `pending` / `running` / `done` / `failed` / `skipped` |
| `started_at`, `finished_at` | ISO 8601 UTC |
| `duration_s` | segundos totais (warmup + measure + overhead) |
| `pgbench_exit_code`, `collector_exit_code` | rc dos subprocessos |
| `error` | mensagem se falhou |
| `setup`, `workload`, `mode`, `pool_size`, `clients`, `rep` | metadata pra grep |

## Estratégia de execução

1. Carrega preset → expande matriz cartesiana
2. Aleatoriza dentro de cada setup (seed do `experiments.yaml` — reproduzível)
3. Agrupa por (setup, pool_size) pra minimizar troca de stack
4. Pra cada grupo: stack up → executa runs do grupo → stack down → próximo grupo
5. Manifest atualizado **atomically** após cada run (tmp + rename — sobrevive Ctrl+C)

## Recuperação após interrupção

Re-rodar o mesmo comando: o orquestrador lê o manifest, pula `status=done`, re-executa pendentes/runnings interrompidos. Adicione `--rerun-failed` pra incluir falhas.

## Decisões metodológicas (ver README.md raiz pra fundamentação)

- **Pool NÃO é drenado entre runs** (cenário realista, pool aquecido)
- **Coletor paralelo ao pgbench** (reflete prod com monitoring ativo)
- **Ordem aleatorizada por bloco** (proteção contra viés temporal sem custo de troca de stack)
- **Falhas continuam bateria** (manifest registra; revisitar com `--rerun-failed`)
- **`pgbench --aggregate-interval=1`** (1 linha/segundo agregada — TPS, lat média/stddev/min/max — em vez de log per-tx que seria 16MB/run)
- **Warmup 30s + medida 60s** (padrão Percona; cabe na cirúrgica de 26h)
