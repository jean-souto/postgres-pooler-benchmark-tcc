# ============================================================
# main.tf — TCC Connection Poolers (estudo PRINCIPAL em AWS)
# Topologia single-AZ, subnet pública. 3 hosts: client, pooler, RDS.
# A EC2 pooler roda os 4 poolers via Docker (host network); o orquestrador
# na EC2 client alterna entre eles via docker context remoto (SSH).
# ============================================================

locals {
  name_prefix  = var.project_name
  create_proxy = var.enable_rds_proxy
}

# ---------- AMI lookup (Ubuntu 24.04 LTS - Canonical) ----------
data "aws_ami" "ubuntu_2404" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# ============================================================
# Chave interna client → pooler (pro docker context remoto via SSH)
# A client precisa de SSH sem senha no pooler pra rodar `docker -H ssh://...`.
# ============================================================
resource "tls_private_key" "internal" {
  algorithm = "ED25519"
}

# ============================================================
# Networking
# ============================================================

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

# Subnet pública primária — onde EC2 cliente, EC2 pooler e RDS rodam.
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name_prefix}-public-${var.availability_zone}"
  }
}

# Subnet privada secundária em outra AZ — existe APENAS pra satisfazer
# o requisito do RDS DB Subnet Group de cobrir >= 2 AZs.
resource "aws_subnet" "private_secondary" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = var.availability_zone_secondary

  tags = {
    Name    = "${local.name_prefix}-private-${var.availability_zone_secondary}"
    Purpose = "DB Subnet Group quorum (no resources here)"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${local.name_prefix}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ============================================================
# Security Groups
# ============================================================

resource "aws_security_group" "client" {
  name        = "${local.name_prefix}-sg-client"
  description = "EC2 cliente: SSH inbound do CIDR autorizado."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-sg-client"
  }
}

resource "aws_vpc_security_group_ingress_rule" "client_ssh" {
  security_group_id = aws_security_group.client.id
  description       = "SSH do CIDR do usuario"
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
  cidr_ipv4         = var.allowed_ssh_cidr
}

resource "aws_vpc_security_group_egress_rule" "client_all" {
  security_group_id = aws_security_group.client.id
  description       = "Egress liberado (apt, pip, docker context SSH pro pooler, etc)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "pooler" {
  name        = "${local.name_prefix}-sg-pooler"
  description = "EC2 pooler: portas 6432-6435 (poolers) + SSH (docker context) inbound do client."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-sg-pooler"
  }
}

resource "aws_vpc_security_group_ingress_rule" "pooler_ssh" {
  security_group_id            = aws_security_group.pooler.id
  description                  = "SSH do client (docker context remoto + debug)"
  from_port                    = 22
  to_port                      = 22
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.client.id
}

resource "aws_vpc_security_group_ingress_rule" "pooler_ssh_user" {
  security_group_id = aws_security_group.pooler.id
  description       = "SSH direto do usuario (debug)"
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
  cidr_ipv4         = var.allowed_ssh_cidr
}

# Uma regra por porta de pooler (6432 pgbouncer-tx, 6433 pgbouncer-session,
# 6434 pgcat-tx, 6435 pgcat-session). Só uma fica ativa por vez na bateria.
resource "aws_vpc_security_group_ingress_rule" "pooler_ports" {
  for_each                     = toset(["6432", "6433", "6434", "6435"])
  security_group_id            = aws_security_group.pooler.id
  description                  = "Pooler port ${each.key} do client"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.client.id
}

resource "aws_vpc_security_group_egress_rule" "pooler_all" {
  security_group_id = aws_security_group.pooler.id
  description       = "Egress liberado (apt, docker pull, RDS upstream)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "rds" {
  name        = "${local.name_prefix}-sg-rds"
  description = "RDS PostgreSQL: 5432 inbound do pooler, do client (baseline) e do RDS Proxy."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-sg-rds"
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_pooler" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres do pooler"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.pooler.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_client" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres do client (baseline sem pooler + bootstrap)"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.client.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_proxy" {
  count                        = local.create_proxy ? 1 : 0
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres do RDS Proxy"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.rds_proxy[0].id
}

resource "aws_vpc_security_group_egress_rule" "rds_all" {
  security_group_id = aws_security_group.rds.id
  description       = "Egress liberado"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "rds_proxy" {
  count       = local.create_proxy ? 1 : 0
  name        = "${local.name_prefix}-sg-rds-proxy"
  description = "RDS Proxy: 5432 inbound do client; egress 5432 pro RDS."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-sg-rds-proxy"
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds_proxy_from_client" {
  count                        = local.create_proxy ? 1 : 0
  security_group_id            = aws_security_group.rds_proxy[0].id
  description                  = "Postgres do client"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.client.id
}

resource "aws_vpc_security_group_egress_rule" "rds_proxy_to_rds" {
  count                        = local.create_proxy ? 1 : 0
  security_group_id            = aws_security_group.rds_proxy[0].id
  description                  = "Egress 5432 pro RDS backend"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.rds.id
}

# ============================================================
# RDS PostgreSQL
# ============================================================

resource "aws_db_subnet_group" "rds" {
  name       = "${local.name_prefix}-db-subnet-group"
  subnet_ids = [aws_subnet.public.id, aws_subnet.private_secondary.id]

  tags = {
    Name = "${local.name_prefix}-db-subnet-group"
  }
}

# Parameter group espelhando postgres/postgresql.conf local.
# CRÍTICO: max_connections e shared_buffers são LITERAIS (não fórmula
# DBInstanceClassMemory) — assim db.m5.large (8GB) emula o footprint de uma
# db.t3.medium (max_connections=415, shared_buffers=1GB), mantendo os cliffs
# do experimento. A m5 entra só pra remover o burst credit do t3.
resource "aws_db_parameter_group" "postgres18" {
  name        = "${local.name_prefix}-pg18"
  family      = "postgres18"
  description = "Tunings espelhando postgresql.conf local. max_connections/shared_buffers travados pra emular db.t3.medium."

  # MD5 client-side: PgCat 1.2 não suporta SCRAM client (issue #255).
  # Paridade com a stack local. Tratado como ameaça à validade na tese.
  # pending-reboot: password_encryption é estático no RDS (immediate é rejeitado).
  # O parameter group é anexado na criação do RDS; o boot inicial já o aplica.
  # Garantia extra de MD5 no bootstrap: client.sh roda `SET password_encryption='md5'`
  # na mesma sessão do ALTER USER (independe do timing do reboot).
  parameter {
    name         = "password_encryption"
    value        = "md5"
    apply_method = "pending-reboot"
  }

  # TRAVADO em 415 (não fórmula) — emula db.t3.medium.
  parameter {
    name         = "max_connections"
    value        = "415"
    apply_method = "pending-reboot"
  }

  # TRAVADO em 1 GB = 131072 páginas de 8 KiB (emula db.t3.medium, não os 8GB da m5.large).
  parameter {
    name         = "shared_buffers"
    value        = "131072"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "effective_cache_size"
    value = "393216" # 3 GB em páginas de 8 KiB (paridade com local)
  }

  parameter {
    name  = "work_mem"
    value = "4096" # KB = 4 MB
  }

  parameter {
    name  = "maintenance_work_mem"
    value = "262144" # KB = 256 MB
  }

  parameter {
    name  = "random_page_cost"
    value = "1.1"
  }

  parameter {
    name  = "effective_io_concurrency"
    value = "200"
  }

  parameter {
    name  = "default_statistics_target"
    value = "100"
  }

  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "pg_stat_statements.track"
    value = "all"
  }

  parameter {
    name         = "pg_stat_statements.max"
    value        = "10000"
    apply_method = "pending-reboot" # static no RDS
  }

  parameter {
    name  = "pg_stat_statements.track_utility"
    value = "0"
  }

  parameter {
    name  = "track_io_timing"
    value = "1"
  }

  parameter {
    name  = "log_lock_waits"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "-1"
  }

  parameter {
    name  = "log_temp_files"
    value = "0"
  }

  parameter {
    name  = "log_checkpoints"
    value = "1"
  }

  # --- WAL / Checkpoints (paridade com postgresql.conf local) ---
  parameter {
    name         = "wal_buffers"
    value        = "2048" # 8KB pages → 16 MB
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "checkpoint_timeout"
    value = "300" # segundos = 5 min
  }

  parameter {
    name  = "checkpoint_completion_target"
    value = "0.9"
  }

  parameter {
    name  = "max_wal_size"
    value = "2048" # MB
  }

  # RDS força mínimo de 128 MB (local usa 80). Impacto desprezível no
  # experimento — min_wal_size é piso de reciclagem de WAL, não afeta a
  # contenção/throughput medidos.
  parameter {
    name  = "min_wal_size"
    value = "128" # MB (mínimo RDS)
  }

  parameter {
    name  = "track_functions"
    value = "pl"
  }

  # RDS restringe log_line_prefix a uma allowlist (não aceita o valor do
  # postgresql.conf local "%m [%p] %q%u@%d"). Usamos o valor permitido mais
  # rico. Não afeta o experimento (formato de log, não medição).
  parameter {
    name  = "log_line_prefix"
    value = "%m:%r:%u@%d:[%p]:%l:%e:%s:%v:%x:%c:%q%a:"
  }

  tags = {
    Name = "${local.name_prefix}-pg18"
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name_prefix}-rds"
  engine         = "postgres"
  engine_version = var.rds_engine_version
  instance_class = var.rds_instance_class

  # max_allocated_storage omitido de propósito: desabilita storage autoscaling
  # (bateria descartável, storage fixo). Igualá-lo a allocated_storage pode ser
  # rejeitado pela API; omitir é a forma limpa de desligar autoscaling.
  allocated_storage = var.rds_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.rds.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  availability_zone      = var.availability_zone
  multi_az               = false
  publicly_accessible    = false

  parameter_group_name = aws_db_parameter_group.postgres18.name

  backup_retention_period      = 0
  skip_final_snapshot          = true
  delete_automated_backups     = true
  deletion_protection          = false
  copy_tags_to_snapshot        = false
  performance_insights_enabled = false
  monitoring_interval          = 0
  apply_immediately            = true

  tags = {
    Name = "${local.name_prefix}-rds"
  }
}

# ============================================================
# RDS Proxy (6º setup — criado por default, gated por enable_rds_proxy)
# ============================================================

resource "aws_secretsmanager_secret" "db" {
  count                   = local.create_proxy ? 1 : 0
  name                    = "${local.name_prefix}-db-credentials"
  description             = "Credenciais do RDS PostgreSQL para o RDS Proxy"
  recovery_window_in_days = 0 # delete imediato no destroy (bateria pontual)
}

resource "aws_secretsmanager_secret_version" "db" {
  count     = local.create_proxy ? 1 : 0
  secret_id = aws_secretsmanager_secret.db[0].id

  secret_string = jsonencode({
    username = var.db_username
    password = var.db_password
  })
}

data "aws_iam_policy_document" "proxy_assume" {
  count = local.create_proxy ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["rds.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "proxy_secrets" {
  count = local.create_proxy ? 1 : 0

  statement {
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [aws_secretsmanager_secret.db[0].arn]
  }
}

resource "aws_iam_role" "proxy" {
  count              = local.create_proxy ? 1 : 0
  name               = "${local.name_prefix}-rds-proxy-role"
  assume_role_policy = data.aws_iam_policy_document.proxy_assume[0].json
}

resource "aws_iam_role_policy" "proxy_secrets" {
  count  = local.create_proxy ? 1 : 0
  name   = "${local.name_prefix}-rds-proxy-secrets"
  role   = aws_iam_role.proxy[0].id
  policy = data.aws_iam_policy_document.proxy_secrets[0].json
}

resource "aws_db_proxy" "main" {
  count                  = local.create_proxy ? 1 : 0
  name                   = "${local.name_prefix}-proxy"
  engine_family          = "POSTGRESQL"
  role_arn               = aws_iam_role.proxy[0].arn
  vpc_subnet_ids         = [aws_subnet.public.id, aws_subnet.private_secondary.id]
  vpc_security_group_ids = [aws_security_group.rds_proxy[0].id]
  require_tls            = false
  idle_client_timeout    = 1800
  debug_logging          = false

  auth {
    auth_scheme = "SECRETS"
    iam_auth    = "DISABLED"
    secret_arn  = aws_secretsmanager_secret.db[0].arn
  }
}

resource "aws_db_proxy_default_target_group" "main" {
  count         = local.create_proxy ? 1 : 0
  db_proxy_name = aws_db_proxy.main[0].name

  connection_pool_config {
    # Dimensionamento p/ NÃO monopolizar os 415 slots do RDS (max_connections):
    #   - 85% => o proxy abre no máx ~352 backends, reservando ~63 slots pra
    #     instrumentação server-side (collector lê pg_stat_* DIRETO no RDS;
    #     reset/VACUUM idem). Sem isso, sob c=1000 o proxy escalava a ~410 e o
    #     collector/reset morriam com "remaining connection slots reserved".
    #   - 25% => mantém no máx ~104 conexões idle quentes (≈ o maior pool
    #     self-hosted testado=100): paridade de pool quente sem penalizar o
    #     proxy com connect-latency a cada borrow, e sem reter ~206 ociosas
    #     entre runs (observado com 50%, contaminava o reset do run seguinte).
    # Decisão de ISOLAMENTO DA MEDIÇÃO, não tuning de desempenho do proxy.
    max_connections_percent      = 85
    max_idle_connections_percent = 25
    connection_borrow_timeout    = 120
  }
}

resource "aws_db_proxy_target" "main" {
  count                  = local.create_proxy ? 1 : 0
  db_proxy_name          = aws_db_proxy.main[0].name
  target_group_name      = aws_db_proxy_default_target_group.main[0].name
  db_instance_identifier = aws_db_instance.main.identifier
}

# ============================================================
# EC2 — Cliente (orquestrador + pgbench + collector)
# ============================================================

resource "aws_instance" "client" {
  ami                         = data.aws_ami.ubuntu_2404.id
  instance_type               = var.client_instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.client.id]
  associate_public_ip_address = true
  key_name                    = var.ssh_key_name

  user_data = templatefile("${path.module}/user_data/client.sh.tftpl", {
    db_endpoint        = aws_db_instance.main.address
    db_username        = var.db_username
    db_password        = var.db_password
    db_name            = var.db_name
    pooler_private_key = tls_private_key.internal.private_key_openssh
    pooler_private_ip  = aws_instance.pooler.private_ip
    rds_proxy_endpoint = local.create_proxy ? aws_db_proxy.main[0].endpoint : ""
  })

  user_data_replace_on_change = true

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  tags = {
    Name = "${local.name_prefix}-client"
    Role = "orchestrator-pgbench-collector"
  }

  depends_on = [aws_db_instance.main]
}

# ============================================================
# EC2 — Pooler (roda os 4 poolers via Docker, host network)
# ============================================================

resource "aws_instance" "pooler" {
  ami                         = data.aws_ami.ubuntu_2404.id
  instance_type               = var.pooler_instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.pooler.id]
  associate_public_ip_address = true
  key_name                    = var.ssh_key_name

  user_data = templatefile("${path.module}/user_data/pooler.sh.tftpl", {
    db_endpoint       = aws_db_instance.main.address
    db_username       = var.db_username
    db_password       = var.db_password
    db_name           = var.db_name
    pooler_public_key = tls_private_key.internal.public_key_openssh
  })

  user_data_replace_on_change = true

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  tags = {
    Name = "${local.name_prefix}-pooler"
    Role = "poolers-docker"
  }

  depends_on = [aws_db_instance.main]
}
