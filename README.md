# Connection Poolers para PostgreSQL

**Análise empírica do trade-off entre sobrecarga e contenção em ambiente de nuvem.**

Parte prática do Trabalho de Conclusão de Curso (Bacharelado em Ciência da Computação — UFU/FACOM). Estudo empírico que compara seis configurações de pooling de conexões PostgreSQL sob diferentes regimes de concorrência, medindo tanto métricas *client-side* (vazão, latência) quanto *server-side* (wait events, contenção de locks, métricas do próprio pooler).

> **Autor:** Jean Souto Galvão Moreira · **Orientadora:** Profa. Dra. Maria Adriana Vidigal de Lima

## Pergunta de pesquisa

> Em que ponto a sobrecarga introduzida por um connection pooler passa a ser compensada pela redução de contenção no PostgreSQL? Análise comparativa entre PgBouncer, PgCat e AWS RDS Proxy sob diferentes regimes de concorrência e perfis de carga.

## Hipótese

Existe um regime de concorrência (medido em número de clientes simultâneos) abaixo do qual a sobrecarga de um pooler degrada a performance percebida pelo cliente, e acima do qual o pooler é necessário para evitar o colapso por contenção de recursos no servidor PostgreSQL. A localização e o formato dessa transição (o break-even `c*`) variam por pooler e por perfil de carga (read-only / write-heavy / mixed).

## Setups comparados

Seis configurações, medidas lado a lado na mesma infraestrutura:

| Setup | Descrição |
|---|---|
| `none` | Controle — clientes conectam direto no PostgreSQL, sem pooler |
| `pgbouncer-tx` | PgBouncer em modo *transaction* (single-thread, event-driven) |
| `pgbouncer-session` | PgBouncer em modo *session* |
| `pgcat-tx` | PgCat em modo *transaction* (multi-thread, Rust/tokio) |
| `pgcat-session` | PgCat em modo *session* |
| `rds-proxy` | AWS RDS Proxy (pooler gerenciado) — apenas no modo cloud |

> PgCat substituiu o Pgpool-II na matriz principal por ser multi-thread e por compartilhar o protocolo administrativo do PgBouncer, o que permite coleta *server-side* simétrica entre os poolers auto-hospedados.

## Diferencial vs. literatura

- **Tigger (Butrovich et al., VLDB 2023)** compara PgBouncer/Pgpool/Odyssey como baseline para propor um proxy novo, sem responder à pergunta de break-even.
- **Benchmarks industriais** (Percona, Tembo, Zalando) medem apenas métricas *client-side*; não olham para dentro do PostgreSQL. A Percona explicitamente pulou a faixa crítica de 56–150 clientes.
- **PgBouncer 1.21+** introduziu prepared statements em transaction mode (2023), sem benchmark comparativo público.
- A **BDTD** não retorna trabalhos brasileiros sobre connection pooling em PostgreSQL.

## Metodologia

A arquitetura completa (local e cloud) está documentada em [`docs/architecture.md`](docs/architecture.md).

### Variáveis controladas

- **Concorrência:** 1, 30, 50, 100, 200, 300, 500, 1000 clientes (varredura fina na faixa do break-even).
- **Workload:** `read` / `write` / `mixed` — ver [`workload/README.md`](workload/README.md).
- **Modo de query (`-M`):** `simple` e `prepared` (isola o ganho de prepared statements em transaction mode).
- **Tamanho do pool:** 2 / 8 / 100 (sub / adequado / super) — `default_pool_size` no PgBouncer e `pool_size` no PgCat, com faixas idênticas para comparação direta.
- **Repetições:** 3 por combinação.

### Métricas

**Client-side (controle):** vazão (TPS), latência p50/p95/p99, erros e timeouts.

**Server-side (foco do trabalho):** wait events dominantes (`pg_stat_activity`), contenção de locks (`pg_locks`, em especial `LWLock:LockManager`), parse/plan time e cache (`pg_stat_statements`), I/O por categoria (`pg_stat_io`, novo no PG 18) e métricas do pooler (`SHOW POOLS`/`SHOW STATS`), que distinguem "fila no pooler" de "lock no Postgres".

### Pool sizing

Hardware-alvo: `db.t3.medium` (2 vCPU, 4 GB RAM). Pela fórmula do PostgreSQL Wiki, `(núcleos × 2) + spindles ≈ 4` é o "número mágico" teórico; o experimento varre **abaixo (2), em torno (8) e muito acima (100)** dele para observar as transições. Fundamentação: [PostgreSQL Wiki — Number Of Database Connections](https://wiki.postgresql.org/wiki/Number_Of_Database_Connections), [HikariCP — About Pool Sizing](https://github.com/brettwooldridge/HikariCP/wiki/About-Pool-Sizing), [Percona — Scaling PostgreSQL with PgBouncer](https://www.percona.com/blog/scaling-postgresql-with-pgbouncer-you-may-need-a-connection-pooler-sooner-than-you-expect/).

> **Caveat metodológico:** PgBouncer e PgCat não são comparáveis 1:1 mesmo com o mesmo pool_size — no PgBouncer o cliente N+1 entra em fila (`cl_waiting` cresce), e uma comparação justa exige medir as métricas *server-side* do próprio pooler, não só a curva client-side. Detalhes na seção de ameaças à validade do TCC.

## Como reproduzir

### Local (Docker — centenas de runs sem custo)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r orchestrator/requirements.txt

# Valida o pipeline ponta a ponta (~25 min, 5 setups locais)
python -m orchestrator --preset smoke --mode local

# Bateria principal local
python -m orchestrator --preset cirurgica --mode local
```

Pré-requisito: Docker em execução. A stack (PostgreSQL 18 + poolers) sobe via `docker-compose.yml`, com um profile por setup.

### Cloud (AWS — validação em RDS real + RDS Proxy)

A infraestrutura é provisionada por Terraform em [`infra-aws/`](infra-aws/) (VPC, RDS, EC2 cliente e EC2 pooler). O modo cloud lê os endpoints injetados via `user_data` e executa `pgbench` nativo contra o pooler por SSH:

```bash
cd infra-aws && terraform apply        # provisiona — ver infra-aws/README.md
# na EC2 cliente:
python -m orchestrator --preset expandida --mode cloud
```

A bateria é retomável após interrupção (o manifesto pula runs já concluídos) e aceita `--rerun-failed` para reexecutar apenas as falhas.

### Presets disponíveis (`experiments.yaml`)

| Preset | Escala | Uso |
|---|---|---|
| `smoke` | ~60 runs | valida o pipeline ponta a ponta |
| `smoke-cloud` / `preflight` | ~24–60 runs | pré-voo da infraestrutura cloud |
| `cirurgica` | ~900 runs | bateria principal local |
| `expandida` | ~1.400 runs | **estudo principal (cloud)**, inclui RDS Proxy |
| `full` | cobertura densa | exploratório |

## Estrutura do repositório

```
.
├── docker-compose.yml      # stack local (Postgres 18 + poolers via profiles)
├── experiments.yaml        # matriz de experimentos (presets)
├── postgres/               # config + init do PostgreSQL
├── pgbouncer/              # configs PgBouncer (transaction + session)
├── pgcat/                  # configs PgCat (transaction + session)
├── workload/               # scripts pgbench customizados (read/write/mixed)
├── collector/              # sampler server-side (queries SQL + amostragem)
├── orchestrator/           # executor da matriz (expande, roda, salva manifesto)
├── analysis/               # loaders + notebooks de análise (figuras do TCC)
├── infra-aws/              # Terraform da infra AWS (RDS, RDS Proxy, EC2)
└── docs/architecture.md    # arquitetura detalhada (local + cloud)
```

As saídas dos experimentos (`data/`) não são versionadas.

## Status

Bateria principal concluída: **1.332 ensaios** executados em AWS (preset `expandida`). As figuras do manuscrito são geradas por `analysis/figs_monografia.py` a partir dos dados coletados.

## Licença

Código distribuído sob a licença MIT — ver [`LICENSE`](LICENSE).
