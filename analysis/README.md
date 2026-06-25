# analysis/

Camada offline de exploracao dos dados coletados pelo `collector/`. Eh aqui que o TCC investiga a pergunta:

> **Em qual ponto o overhead de um connection pooler eh compensado pela reducao de contencao no PostgreSQL?**

## Conteudo

| Arquivo | Funcao |
|---|---|
| `loader.py` | Funcoes utilitarias para carregar experimentos, calcular deltas de metricas cumulativas, agregar wait events e locks, parsear logs do pgbench. |
| `notebooks/01_baseline.ipynb` | Notebook de inspecao inicial: visao geral, wait events, locks, deltas de `pg_stat_statements` e `pg_stat_io`. |
| `requirements.txt` | Deps de analise (pandas, matplotlib, jupyterlab, seaborn). Separado do `collector/requirements.txt` de proposito (collector roda enxuto). |

## Estrutura esperada de `data/`

O loader assume o layout que o collector ja produz:

```
data/
  {exp_id}/
    config.json              # {dsn, interval_s, duration_s, label{...}, started_at}
    pg_stat_activity.csv     # snapshot por amostra
    pg_locks.csv             # snapshot por amostra
    pg_stat_statements.csv   # CUMULATIVO -- usar compute_deltas
    pg_stat_io.csv           # CUMULATIVO -- usar compute_deltas
```

`exp_id` eh o nome do diretorio (livre). O `label` no `config.json` carrega os eixos do experimento (ex: `setup=pgbouncer-tx, workload=tpcb, clients=20, mode=prepared`) e fica replicado em cada linha dos CSVs.

## Como rodar o notebook

A partir da raiz `Pratica/`:

```bash
source .venv/bin/activate
pip install -r analysis/requirements.txt
jupyter lab analysis/notebooks/01_baseline.ipynb
```

O notebook re-resolve `sys.path` para que `from analysis.loader import ...` funcione mesmo sem instalacao em modo editavel.

## API do `loader.py`

| Funcao | O que faz |
|---|---|
| `load_experiment(exp_dir)` | Carrega 1 experimento. Retorna `{config, activity, locks, statements, io}`. |
| `load_experiments(data_dir, glob_pattern="*")` | Carrega N experimentos em paralelo (ThreadPoolExecutor). Retorna `{exp_id: ...}`. |
| `compute_deltas(df, group_by, cumulative_cols)` | Diferenca entre amostras consecutivas dentro de cada grupo. Cria `delta_<col>` + `interval_s`. |
| `wait_event_distribution(activity)` | Conta wait events entre backends `state == 'active'` (NaN -> `CPU`). |
| `lock_summary(locks)` | Agrega locks por `(locktype, mode)`. |
| `tps_from_pgbench_log(log_path)` | Parseia stdout do pgbench (`tps`, `latency_avg_ms`, `clients`, etc). |

Todas sao defensivas: aceitam DataFrames vazios, CSVs ausentes ou logs incompletos sem crashar.
