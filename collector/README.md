# Collector

Sampler de métricas server-side do PostgreSQL. Amostra views em intervalos regulares
e grava em CSVs com timestamp + label do experimento.

## Views amostradas

| View | Conteúdo | Tipo |
|---|---|---|
| `pg_stat_activity` | backends ativos, wait events, queries em execução | snapshot |
| `pg_locks` | locks por backend, modo, granted | snapshot |
| `pg_stat_statements` | queries agregadas, tempos, I/O, WAL | cumulativo |
| `pg_stat_io` | I/O por backend_type/object/context (PG 18) | cumulativo |

## Setup

```bash
cd /home/jean/Documentos/TCC/Pratica
python3 -m venv .venv
source .venv/bin/activate
pip install -r collector/requirements.txt
```

## Uso

```bash
python -m collector \
    --dsn "postgresql://tcc:tcc@localhost:5433/tcc" \
    --interval 1 \
    --duration 60 \
    --output data/exp001 \
    --label "setup=none,workload=read,clients=10"
```

## Output

```
data/exp001/
├── config.json              # metadata do run
├── pg_stat_activity.csv     # 1 linha por backend por amostra
├── pg_locks.csv             # 1 linha por lock por amostra
├── pg_stat_statements.csv   # snapshot por amostra (cumulativo)
└── pg_stat_io.csv           # snapshot por amostra (cumulativo)
```

## Notas

- Usa 1 conexão dedicada e filtra `pid <> pg_backend_pid()` pra não medir a si mesma.
- `flush()` após cada amostra: dados não se perdem se o sampler cair.
- Cumulativos (statements/io) precisam de cálculo de delta na análise — `pg_stat_statements_reset()` antes do experimento simplifica.
- Se o intervalo for menor que o tempo de execução das queries, log warning é emitido.
