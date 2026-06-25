# Workloads pgbench

3 scripts customizados que estressam dimensões diferentes do PostgreSQL.

## Scripts

| Script | Estressa | Operações por tx |
|---|---|---|
| `read.sql` | Throughput de leitura, plan cache, latência | 1 SELECT por PK |
| `write.sql` | Row locks, WAL, fsync | 1 UPDATE + 1 INSERT |
| `mixed.sql` | Tudo um pouco (TPC-B-like) | 3 UPDATE + 1 SELECT + 1 INSERT |

## Por que customizado?

- `read.sql`: pgbench tem `--select-only` mas é fixo no SELECT do TPC-B (que inclui um JOIN implícito). Aqui é mais magro.
- `write.sql`: não existe builtin. UPDATE em `pgbench_branches` (scale linhas) é PROPOSITAL pra amplificar contenção de row lock — exatamente o que poolers afetam.
- `mixed.sql`: equivalente ao `--builtin tpcb-like` mas explícito (orientador/banca consegue ler o que está sendo testado).

## Pré-requisito

```bash
# Inicializa tabelas (1x apenas, scale=10 = ~150 MB)
docker compose exec -T -e PGPASSWORD=tcc postgres pgbench -h postgres -p 5432 -U tcc -d tcc -i -s 10
```

## Como rodar

Forma "manual" (durante desenvolvimento):

```bash
# Direto no Postgres (baseline)
docker compose exec -T -e PGPASSWORD=tcc postgres \
    pgbench -h postgres -p 5432 -U tcc -d tcc \
    -c 50 -j 4 -T 60 \
    -f /workload/read.sql

# Via PgBouncer-tx (precisa ter o profile up)
docker compose exec -T -e PGPASSWORD=tcc postgres \
    pgbench -h pgbouncer-tx -p 6432 -U tcc -d tcc \
    -c 50 -j 4 -T 60 -M prepared \
    -f /workload/write.sql
```

Os arquivos `.sql` precisam ser visíveis ao container que executa o pgbench
(montar `./workload:/workload:ro` no service `postgres` do compose, ou copiar
inline). A automação dos experimentos vai cuidar disso na fase de matriz.

## Variáveis pgbench utilizadas

- `:scale` — scale factor (passado em `-s` no init)
- `:aid`, `:bid`, `:tid`, `:delta` — random gerado por transação

## Notas pro TCC

- Transações curtas (read.sql) são propositalmente curtas — maximizam hand-offs em transaction-mode pooling.
- `pgbench_branches` tem só `scale` linhas → write.sql concentra contenção em poucas rows (efeito do pooler fica visível).
- Cada workload roda contra o mesmo dataset (init `-s 10`); só muda o `-f`.

## Modos de query (`-M`) — DIMENSÃO DA MATRIZ

Cada workload é executado em **2 modos** do pgbench:

| Modo | Flag | O que mede |
|---|---|---|
| Simple | `-M simple` (default) | Parse + plan + execute a cada execução. Estressa plan cache do Postgres. |
| Prepared | `-M prepared` | PREPARE 1×, execute N×. Recurso novo no PgBouncer 1.21+ em transaction mode. |

**Por que ambos**: sem variar `-M`, o experimento mede só "overhead de parse repetido". Variando, conseguimos isolar o ganho de prepared statements em transaction mode — exatamente o ângulo recente que torna o TCC original (PgBouncer 1.21 trouxe isso em set/2023).

`extended` (`-M extended`) **fora do escopo**: pouco usado em produção e adiciona dimensão sem ganho proporcional de informação.

## Matriz completa de experimentos

```
4 setups × 3 workloads × 2 modos × N níveis de concorrência × 5 repetições
```

A automação (orquestrador, fase futura) deve iterar todas essas combinações.
