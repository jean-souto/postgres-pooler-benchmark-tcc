# infra-aws — Estudo PRINCIPAL em AWS (Connection Poolers)

Infra-as-Code Terraform para a bateria **expandida** (~1.411 runs, ~41h) que é
a fonte primária de dados da tese. Provisiona 3 hosts separados (cliente,
pooler, RDS) pra medir o overhead REAL de rede de cada pooler.

> O experimento local (Docker) virou apenas sanity-check/dev. **A nuvem é o
> estudo principal.**

---

## 1. O que essa infra provisiona

```
                          AWS us-east-1
   ┌──────────────────────────────────────────────────────────────┐
   │ VPC 10.0.0.0/16   ·   Subnet pública 10.0.1.0/24 (us-east-1a) │
   │                                                                │
   │  ┌─ EC2 client ──────┐   ┌─ EC2 pooler ───────────┐  ┌─ RDS ─┐ │
   │  │ m5.large          │   │ m5.xlarge (4 vCPU)      │  │ PG18  │ │
   │  │ • orquestrador    │──▶│ Docker (host network):  │─▶│ db.m5 │ │
   │  │ • pgbench (nativo)│   │  pgbouncer-tx   :6432   │  │ .large│ │
   │  │ • collector       │   │  pgbouncer-sess :6433   │  │       │ │
   │  │                   │   │  pgcat-tx       :6434   │  │ params│ │
   │  │ controla pooler   │   │  pgcat-session  :6435   │  │ trava-│ │
   │  │ via SSH ──────────┼──▶│ (um por vez)            │  │ dos   │ │
   │  └─────────┬─────────┘   └─────────────────────────┘  └───▲───┘ │
   │            │  none/rds-proxy: client → RDS / Proxy direto ─┘     │
   │            └──────────▶ RDS Proxy (6º setup) :5432 ─────────────┘
   │  Subnet privada 10.0.2.0/24 (us-east-1b) — vazia (quorum 2-AZ do RDS) │
   └──────────────────────────────────────────────────────────────┘
```

**Decisões de design** (ver `docs/architecture.md` e a tese):
- **m5 (CPU dedicada)**: elimina o burst credit do t3, que contaminaria dados
  CPU-bound sob carga sustentada de 41h.
- **Pooler m5.xlarge (4 vCPU)**: dá espaço pro multi-threading do PgCat brilhar
  vs PgBouncer single-thread (hipótese central). Client/RDS = 2 vCPU.
- **Parâmetros RDS travados** (`max_connections=415`, `shared_buffers=1GB`):
  emulam o footprint de uma `db.t3.medium` apesar da m5.large ter 8 GB — isola
  a variável "burst" sem mudar os cliffs do experimento.
- **MD5 global** (`password_encryption=md5`): PgCat 1.2 não suporta SCRAM
  client-side (issue #255). Paridade entre os 6 setups; ameaça à validade
  documentada na tese.
- **Os 4 poolers num único deploy**: o orquestrador alterna (um por vez, via
  SSH) — não há `var.pooler_type`.

---

## 2. Pré-requisitos

1. **AWS CLI configurada** (`aws sts get-caller-identity` responde).
2. **Terraform >= 1.9** (`terraform version`).
3. **EC2 Key Pair** existente:
   ```bash
   aws ec2 create-key-pair --key-name tcc-poolers \
     --query 'KeyMaterial' --output text > ~/.ssh/tcc-poolers.pem
   chmod 600 ~/.ssh/tcc-poolers.pem
   ```
4. **Seu IP público**: `curl -s ifconfig.me`

---

## 3. Variáveis (`terraform.tfvars`)

Copie `terraform.tfvars.example` → `terraform.tfvars` e preencha. **Não
commitar** (já está no `.gitignore`).

```hcl
db_password      = "TROCAR-min-12-chars"   # ≥12 chars (vai pro RDS + poolers via MD5)
allowed_ssh_cidr = "200.123.45.67/32"      # seu IP/32
ssh_key_name     = "tcc-poolers"           # nome da EC2 Key Pair
# enable_rds_proxy = true   # 6º setup (default true)
```

⚠️ `db_password` vai em cleartext no user_data das EC2 (visível via
`ec2:DescribeInstanceAttribute`). Aceitável pra bateria descartável; não usar em
prod.

---

## 4. Apply

```bash
cd infra-aws
terraform init
terraform plan -out=tfplan      # revise: ~25 recursos
terraform apply tfplan
```

**Tempos**: VPC/SG/EC2 ~1min · RDS PG18 ~10-12min · RDS Proxy +~5min ·
user_data (Docker, venv, bootstrap MD5) ~2-3min após boot. Total ~15-20min.

---

## 5. Rodar a bateria (orquestrador modo cloud)

O orquestrador roda **na EC2 client**. Traga o repo e dispare:

```bash
# 1. Copie o repo do TCC pra client (do seu laptop):
CLIENT=$(terraform output -raw client_public_ip)
rsync -az -e "ssh -i ~/.ssh/tcc-poolers.pem" \
  --exclude data/ --exclude .git/ --exclude '*/.venv/' \
  ./ ubuntu@$CLIENT:/home/ubuntu/Pratica/

# 2. SSH na client:
eval "$(terraform output -raw client_ssh_command)"
#    IP dinâmico? Se o SSH parar após trocar de rede, rode ./allow-my-ip.sh
#    (reabre o SSH no IP atual em ~segundos). A bateria em tmux NÃO cai nesse
#    meio tempo — só sua reconexão é afetada.

# 3. Na client — carrega endpoints + ativa venv:
set -a; . ~/.tcc/env; set +a       # TCC_DB_ENDPOINT, _POOLER_HOST, etc.
source ~/.venv/bin/activate
cd ~/Pratica

# 4. SMOKE cloud primeiro (valida pipeline end-to-end, ~poucos min):
python -m orchestrator --mode cloud --preset smoke --output data \
  --log-file data/orch-smoke-cloud.log
#   → confira: todos os setups OK, CSVs do pooler (pgcat_*/pgbouncer_*) NÃO vazios

# 5. Bateria expandida (~41h — use tmux!):
tmux new -s tcc
python -m orchestrator --mode cloud --preset expandida --output data \
  --log-file data/orch-expandida.log
#   Ctrl+B D pra desanexar · tmux attach -t tcc pra voltar
```

O orquestrador automaticamente: lê `~/.tcc/env`, gera as configs com a senha
real + `rsync` pro pooler, sobe um pooler por vez via SSH, roda pgbench nativo
apontando os endpoints, faz VACUUM/CHECKPOINT entre cada config.

**Trazer os dados de volta** (pro notebook/análise no laptop):
```bash
rsync -az -e "ssh -i ~/.ssh/tcc-poolers.pem" ubuntu@$CLIENT:/home/ubuntu/Pratica/data/ ./data-cloud/
```

---

## 6. Custo (us-east-1, on-demand — AWS Pricing API + doc, mai/2026)

| Recurso | $/h |
|---|---:|
| EC2 client (m5.large) | $0.0960 |
| EC2 pooler (m5.xlarge) | $0.1920 |
| RDS db.m5.large (PostgreSQL) | $0.1780 |
| RDS Proxy (2 vCPU × $0.015, mínimo) | $0.0300 |
| Storage gp3 (~70 GB) + Secrets | ~$0.0092 |
| **Total** | **~$0.505/h** |

Bateria expandida ~41h ≈ **$21**. Smoke + provisioning + re-runs + folga:
orce **~$25-30**. `terraform output estimated_hourly_cost_usd` mostra o cálculo.
Preços verificados na Pricing API (não estimados).

> Pra economizar entre sessões: `terraform apply` só roda quando você quer. Não
> há `stop` automatizado — destrua (§7) ao terminar a bateria.

---

## 7. Destroy

```bash
terraform destroy
```

Pós-destroy (deve voltar tudo vazio):
```bash
aws ec2 describe-instances --filters "Name=tag:Project,Values=TCC-Poolers" \
  --query 'Reservations[].Instances[].[InstanceId,State.Name]'
aws rds describe-db-instances --query 'DBInstances[?DBName==`tcc`]'
aws secretsmanager list-secrets --query 'SecretList[?Name==`tcc-poolers-db-credentials`]'
```
`skip_final_snapshot` + `recovery_window_in_days=0` ⇒ zero resíduo cobrável.

---

## 8. Limitações conhecidas (ameaças à validade — citar na tese)

- **MD5 em vez de SCRAM**: PgCat 1.2 e Odyssey 1.4 não suportam SCRAM
  client-side. MD5 global mantém paridade entre setups; cifragem de wire mais
  fraca, mitigada por rede privada (VPC).
- **Poolers em Docker (host network)**: overhead de container ~negligível com
  host network; representa deployment k8s/ECS moderno.
- **CPU steal residual**: m5 compartilha hypervisor (sem dedicated tenancy).
  Muito menor que t3, mas não zero.
- **Single AZ**: foco é overhead de pooling, não HA/failover.
- **`auth_query` desabilitado no PgCat** (bug 1.2 envia user sem aspas). Usa
  senha inline. Reativar exige rodar `init.sql` equivalente no RDS (a função
  `user_lookup` e o user `pgcat_auth` só existem no Postgres local).
- **RDS Proxy + `require_tls=false`**: paridade com os poolers self-hosted (que
  rodam sem TLS). Documentar.
