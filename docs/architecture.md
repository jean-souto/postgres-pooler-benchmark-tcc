# Arquitetura — Setup experimental do TCC

Este documento descreve a arquitetura do experimento empírico em
duas materializações: **local** (Docker Compose, usado para a maior
parte das centenas de runs) e **cloud** (Terraform em AWS, usado
para a bateria pontual de validação e para o estudo de caso do RDS
Proxy). Em seguida, discute as **diferenças entre os dois ambientes
que afetam a interpretação dos resultados** e as **decisões
metodológicas** que sustentam o desenho.

---

## 1. Arquitetura local (Docker Compose)

### 1.1. Topologia

```
                    Host (workstation Linux)
   ┌────────────────────────────────────────────────────────────┐
   │                                                            │
   │  ┌─────────────┐         ┌─────────────────────────────┐   │
   │  │ orchestrator│         │ Docker network "default"    │   │
   │  │  (Python)   │         │  (driver=bridge)            │   │
   │  │             │         │                             │   │
   │  │ - matrix    │         │  ┌───────────────────────┐  │   │
   │  │ - runner    │  exec───┼─→│ tcc-postgres          │  │   │
   │  │ - manifest  │  pgbench│  │ image: postgres:18    │  │   │
   │  └──────┬──────┘         │  │ port: 5433 → 5432     │  │   │
   │         │                │  │ cpus=2.0 mem=4G       │  │   │
   │         │                │  └───────────┬───────────┘  │   │
   │         │ TCP 5433 / 6432 / 6433 / 9999 │              │   │
   │         │                │              │              │   │
   │         │     ┌──────────┼──────────────┘              │   │
   │         │     │          │  (hostname "postgres")      │   │
   │         │     │          │              │              │   │
   │  ┌──────▼─────▼┐         │  ┌───────────▼───────────┐  │   │
   │  │  collector  │  TCP    │  │ tcc-pgbouncer-tx      │  │   │
   │  │  (Python)   │ ─────→  │  │ image: edoburu        │  │   │
   │  │             │  5433   │  │   /pgbouncer:1.25.1   │  │   │
   │  │ samples:    │  +6432  │  │ profile: pgbouncer-tx │  │   │
   │  │  pg_stat_*  │  +9999  │  │ port: 6432 → 6432     │  │   │
   │  │  pg_locks   │         │  └───────────────────────┘  │   │
   │  │  SHOW POOLS │         │  ┌───────────────────────┐  │   │
   │  │  SHOW POOL_*│         │  │ tcc-pgbouncer-session │  │   │
   │  └─────────────┘         │  │ profile: pgb-session  │  │   │
   │                          │  │ port: 6433 → 6432     │  │   │
   │                          │  └───────────────────────┘  │   │
   │                          │  ┌───────────────────────┐  │   │
   │                          │  │ tcc-pgpool            │  │   │
   │                          │  │ image: bitnami        │  │   │
   │                          │  │   /pgpool:4.6.3       │  │   │
   │                          │  │ profile: pgpool       │  │   │
   │                          │  │ port: 9999 → 5432     │  │   │
   │                          │  └───────────────────────┘  │   │
   │                          └─────────────────────────────┘   │
   └────────────────────────────────────────────────────────────┘
```

### 1.2. Componentes e responsabilidades

| Componente | Imagem | Função | Acesso pelo cliente |
|---|---|---|---|
| `tcc-postgres` | `postgres:18` | Servidor PostgreSQL 18 sob teste | `localhost:5433` (baseline sem pooler) |
| `tcc-pgbouncer-tx` | `edoburu/pgbouncer:v1.25.1-p0` | PgBouncer em modo *transaction* | `localhost:6432` |
| `tcc-pgbouncer-session` | `edoburu/pgbouncer:v1.25.1-p0` | PgBouncer em modo *session* | `localhost:6433` |
| `tcc-pgpool` | `bitnamilegacy/pgpool:4.6.3` | Pgpool-II 4.6 | `localhost:9999` |
| `orchestrator` (host) | n/a (Python local) | Orquestra docker compose, dispara pgbench, atualiza manifest CSV | — |
| `collector` (host) | n/a (Python local) | Amostra views server-side em paralelo ao pgbench | — |

### 1.3. Profiles do Compose: por que apenas um pooler ativo por vez

O `docker-compose.yml` define os três poolers sob *profiles*
(`pgbouncer-tx`, `pgbouncer-session`, `pgpool`). O Compose só sobe
serviços do profile explicitamente solicitado:

```yaml
pgbouncer-tx:
  profiles: ["pgbouncer-tx"]
  image: edoburu/pgbouncer:v1.25.1-p0
  ...
```

**Por quê**: garantir isolamento de recursos. Se os três poolers
estivessem ativos simultaneamente, mesmo que o cliente conecte
apenas em um, os outros consumiriam descritores de arquivo e RAM
da JVM/heap interna do container — ruído indesejado.

O orquestrador (`orchestrator/stack.py`) executa
`docker compose --profile <X> up -d` antes de cada *bucket* de runs
e `docker compose down` ao trocar de pooler.

### 1.4. Volumes e configurações

| Volume / Mount | Propósito |
|---|---|
| `./postgres/postgresql.conf:/etc/postgresql/postgresql.conf:ro` | Configuração tunada para mimetizar `db.t3.medium` (4 GB RAM, 2 vCPU, `max_connections=415`) |
| `./postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro` | Cria extensão `pg_stat_statements` |
| `./workload:/workload:ro` | Scripts pgbench customizados (`read.sql`, `write.sql`, `mixed.sql`) |
| `./data:/data:rw` | Saída compartilhada entre container e host |
| `pgdata:/var/lib/postgresql` | Volume nomeado para persistir entre runs (evita re-init de pgbench) |
| `./pgbouncer/pgbouncer-{transaction,session}.ini:/etc/pgbouncer/pgbouncer.ini:ro` | Configurações distintas por modo |

### 1.5. Limites de recursos

```yaml
postgres:
  shm_size: 512mb
  deploy:
    resources:
      limits:
        cpus: "2.0"
        memory: 4G
```

Os limites espelham a instância AWS-alvo. `shm_size: 512mb` dá
margem para o segmento de memória dinâmico (DSM) usado por
*parallel workers* — TPC-B raramente os dispara, mas hash joins
podem.

Os containers de pooler **não têm limites** definidos: PgBouncer é
notoriamente leve (~10 MiB) e Pgpool-II com `num_init_children=32`
fica abaixo de 200 MiB. Limitar artificialmente confundiria o
diagnóstico.

---

## 2. Arquitetura cloud (Terraform — planejada)

> Status: implementada em `infra-aws/`, não aplicada ainda. Será
> usada apenas para a bateria pontual de validação. Custo estimado
> ~$0,17/h com tudo ligado; bateria de 10h ≈ $1,70.

### 2.1. Topologia

```
                          AWS us-east-1
            ┌─────────────────────────────────────────────────┐
            │ VPC 10.0.0.0/16                                 │
            │                                                 │
            │  ┌──────────────────────────────────────────┐   │
            │  │ Subnet pública 10.0.1.0/24 (us-east-1a)  │   │
            │  │                                          │   │
            │  │  ┌──────────┐    ┌──────────┐            │   │
   SSH ─────┼──┼─→│ EC2      │───→│ EC2      │──┐         │   │
   (Jean)   │  │  │ client   │    │ pooler   │  │         │   │
            │  │  │ t3.medium│    │ t3.small │  │ 5432    │   │
            │  │  │ pgbench  │    │ pgb/pgp  │  ▼         │   │
            │  │  └────┬─────┘    └──────────┘ ┌────────┐ │   │
            │  │       │ 5432 (baseline)       │ RDS    │ │   │
            │  │       └──────────────────────→│ PG18   │ │   │
            │  │       │ 5432 (proxy)          │ db.t3. │ │   │
            │  │       ▼                       │ medium │ │   │
            │  │  ┌──────────┐                 └────────┘ │   │
            │  │  │ RDS Proxy│─────5432────────────▲      │   │
            │  │  │ (opt.)   │                     │      │   │
            │  │  └──────────┘                     │      │   │
            │  └──────────────────────────────────────────┘   │
            │                                                 │
            │  ┌──────────────────────────────────────────┐   │
            │  │ Subnet privada 10.0.2.0/24 (us-east-1b)  │   │
            │  │ (vazia — só pra satisfazer 2-AZ do RDS)  │   │
            │  └──────────────────────────────────────────┘   │
            └─────────────────────────────────────────────────┘
```

### 2.2. Hosts separados — justificativa

A escolha de **três EC2 separadas** (cliente, pooler, RDS) é
deliberada e contrasta com o setup local (tudo no mesmo host):

1. **Reflete arquitetura de produção.** Em prod o pooler quase
   nunca compartilha host com o cliente nem com o banco —
   tipicamente roda em ASG dedicado, atrás de NLB. Co-localizar
   pooler e cliente em testes ocultaria o overhead de rede.
2. **Mede *overhead* REAL de rede.** Cada query atravessa:
   `cliente → SG → ENI → pooler → SG → ENI → RDS`. Cada salto
   adiciona latência de microssegundos a milissegundos. O TCC
   precisa medir isso explicitamente para defender ou refutar a
   hipótese de que o overhead se paga em alta concorrência.
3. **Replica decisões de SG e IAM** que aparecem em qualquer
   bench cloud honesto.

### 2.3. Security groups

```
Cliente (sg_client)        Pooler (sg_pooler)         RDS (sg_rds)
   │                           │                          │
   │ outbound → 5432           │ inbound 6432 ← sg_client │
   │ outbound → 6432           │ outbound → 5432          │ inbound 5432 ← sg_client
   │ inbound 22 ← Jean IP      │ inbound 22 ← Jean IP     │ inbound 5432 ← sg_pooler
                                                            inbound 5432 ← sg_proxy (se ligado)
```

### 2.4. Configurações espelhadas

A paridade local ↔ cloud é mantida via:

- **PostgreSQL**: `infra-aws/postgres-rds.conf` é uma cópia funcional
  de `postgres/postgresql.conf` traduzida para Parameter Group RDS
  (alguns parâmetros têm nome ou unidade diferentes).
- **PgBouncer**: o `user_data` da EC2 pooler escreve um
  `pgbouncer.ini` derivado dos arquivos em `pgbouncer/`.
- **Pgpool-II**: variáveis de ambiente passadas no `user_data`
  espelham as do container Bitnami.

### 2.5. Custos e ciclo de vida

| Recurso | $/h | Notas |
|---|---:|---|
| EC2 client (t3.medium) | $0,0416 | sempre |
| EC2 pooler (t3.small) | $0,0208 | só quando `pooler_type != none` |
| RDS db.t3.medium | $0,0720 | sempre |
| RDS Proxy (2 vCPU) | $0,0300 | só quando `enable_rds_proxy=true` |
| EBS gp3 + Secrets | ~$0,005 | desprezível em horas |
| **Total full setup** | **~$0,17** | tudo ligado |

A stack inteira é destruível com `terraform destroy`; sem
`prevent_destroy` em nenhum recurso, intencionalmente — bateria
descartável.

---

## 3. Diferenças local ↔ cloud que afetam a interpretação

Estas diferenças precisam aparecer no capítulo de discussão do TCC.
Ignorá-las invalidaria a comparação.

| Dimensão | Local (Docker) | Cloud (AWS) | Impacto na medição |
|---|---|---|---|
| **Latência de rede** | bridge ~30–80 µs intra-host | VPC same-AZ ~150 µs–1 ms | Cloud penaliza queries muito curtas; pode mascarar ganho do pooling em baixa concorrência |
| **Subsistema I/O** | SSD NVMe local sem QoS | RDS gp3 (3000 IOPS, 125 MB/s baseline) | Cloud tem teto rígido — cargas write podem saturar IOPS antes de saturar locks |
| **CPU (t3.medium)** | sem burst credits | burst credits que esgotam em ~30 min sob 100% CPU | Runs curtos (<10 min) podem dar TPS irrealisticamente alto |
| **`shared_buffers`** | mesmo valor (1 GB) | mesmo valor | OK, paridade |
| **`max_connections`** | 415 | 415 (default RDS para t3.medium) | OK, paridade |
| **TLS** | desabilitado | desabilitado para reduzir variáveis | OK, paridade |
| **Pooler em outra máquina** | não (mesmo host, container vizinho) | sim (EC2 dedicada) | Cloud mede overhead de rede do pooler; local mede só overhead de processamento |

### 3.1. Burst credits do t3 — armadilha conhecida

Instâncias `t3.*` acumulam *CPU credits* quando ociosas e os
queimam quando saturam. Em runs curtos (<5 min) pode-se observar
TPS irrealisticamente alto seguido de queda abrupta quando os
créditos zeram. **Mitigação**: rodar cada combinação por ≥5 min
(o preset `cirurgica` usa `measure_s=60` mais `warmup_s=30` =
1m30s por run; **isso é insuficiente para esgotar créditos** e
deve ser discutido honestamente como caveat). Para a bateria
cloud, runs específicos de validação podem usar `measure_s=300`
(5 min).

> [REVISAR: confirmar com orientador se vale aumentar `measure_s`
> globalmente para 180 ou 300 segundos. Trade-off: triplica o
> tempo total da bateria.]

---

## 4. Decisões metodológicas que materializam a arquitetura

### 4.1. Cada componente em container separado, mesmo no setup local

Seria possível rodar PgBouncer no host (ele é leve, ~10 MiB), mas
**não foi essa a escolha**. Justificativas:

1. **Paridade com cloud.** Em AWS o pooler está em outra máquina;
   no local quero ao menos um *namespace* de rede separado para
   que latência inter-componente não seja zero.
2. **Reproduzibilidade.** Container empacota dependências
   exatas — não depende de pacotes do sistema do desenvolvedor.
3. **Limpeza de runs.** `docker compose down` zera o estado do
   pooler entre runs sem afetar o PostgreSQL (que mantém o
   `pgdata` em volume nomeado).

### 4.2. Single AZ

A bateria cloud usa uma única AZ (`us-east-1a`). Não testar
*failover* nem latência cross-AZ é deliberado:

- Single AZ reduz variabilidade de rede (~150 µs intra-AZ vs
  ~2–4 ms cross-AZ), tornando a comparação entre poolers mais
  limpa.
- A pergunta de pesquisa não é sobre HA — é sobre overhead de
  pooling. Adicionar AZ múltiplas confundiria a análise.
- Custo: cross-AZ data transfer custa $0,01/GB, multiplicado por
  ~720 runs aceitáveis mas evitável.

A subnet privada secundária vazia existe apenas para satisfazer o
requisito do RDS DB Subnet Group (mínimo 2 AZs). RDS continua na
primária via `availability_zone`.

### 4.3. Pooler em EC2 dedicada (não co-located)

Em produção, *connection poolers* nunca rodam na mesma máquina do
banco — queimaria CPU/RAM compartilhada e introduziria efeitos de
*noisy neighbor*. Co-localizar **falsificaria** a medição do
*overhead* de rede que é parte do que se quer estimar. Por isso
mesmo no setup local cada componente está em container distinto.

### 4.4. Coletor server-side fora da máquina cliente

O `collector/` Python roda **no host**, não dentro do container
postgres nem do cliente:

- Conecta ao PostgreSQL e aos *poolers* via sockets TCP, igual a
  qualquer aplicação externa — então não há viés de "monitor
  intra-processo".
- Em execução paralela ao pgbench, com grade temporal fixa
  (`time.monotonic()`, sem drift cumulativo) — captura estado
  durante a carga.
- Suas queries (em `collector/queries/*.sql`) são leves e
  versionadas — qualquer um pode reproduzir as mesmas amostragens.

```python
# orchestrator/runner.py — extrato conceitual
collector_proc = subprocess.Popen(["python", "-m", "collector", ...])
pgbench_proc   = subprocess.Popen(["pgbench", ...])
pgbench_proc.wait()
collector_proc.terminate()
```

### 4.5. Manifest CSV como fonte da verdade

O orquestrador (`orchestrator/manifest.py`) mantém um CSV com
status (`pending` / `running` / `done` / `failed` /
`expected_failure`) por `exp_id`. Re-runs são idempotentes:
`--rerun-failed` só executa o que falhou; `--rerun-all` força tudo.
Essa decisão arquitetural permite interromper baterias longas (21h)
e retomá-las sem repetir trabalho.

### 4.6. Pre-marcação de runs inviáveis

O matrix expander (`orchestrator/matrix.py`) marca como
`expected_failure` runs onde `clients > max_backend_connections`
no setup `none`, ou `clients > num_init_children` no Pgpool. Isso
é **arquitetural**, não bug: documenta limites previstos pela
literatura ao invés de tentar executá-los e gastar tempo em
*timeouts*.

---

## 5. Resumo

A arquitetura experimental foi desenhada com três princípios:

1. **Paridade local ↔ cloud** sempre que possível, com diferenças
   documentadas (não escondidas).
2. **Cada componente isolado** em namespace de rede próprio
   (container ou EC2), refletindo prod e permitindo medir overhead
   de rede honestamente.
3. **Tudo versionado e reproduzível** — Docker Compose, Terraform,
   orquestrador, queries do collector, manifest de runs, configs
   tunadas. Qualquer terceiro pode `git clone` e rodar.

> [REVISAR: confirmar com orientador se a discussão de
> *burst credits* deve ir em capítulo próprio de "ameaças à
> validade" ou ficar dispersa.]
