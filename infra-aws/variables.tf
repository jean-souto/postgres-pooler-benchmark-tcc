# ============================================================
# variables.tf — TCC Connection Poolers (bateria AWS — estudo PRINCIPAL)
#
# Modelo: a EC2 pooler roda TODOS os poolers via Docker (host network).
# O orquestrador (na EC2 client) sobe um de cada vez via docker context
# remoto. Não há var pooler_type — a matriz inteira roda num único deploy.
# ============================================================

variable "aws_region" {
  description = "Região AWS onde a bateria roda."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]$", var.aws_region))
    error_message = "aws_region deve seguir o padrão AWS (ex: us-east-1, sa-east-1)."
  }
}

variable "availability_zone" {
  description = "AZ primária onde EC2 e RDS rodam (single-AZ por design)."
  type        = string
  default     = "us-east-1a"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9][a-z]$", var.availability_zone))
    error_message = "availability_zone deve ter sufixo de letra (ex: us-east-1a)."
  }
}

variable "availability_zone_secondary" {
  description = "AZ secundária — usada APENAS pra satisfazer o requisito de 2 AZs do DB Subnet Group. Nenhum recurso roda nela."
  type        = string
  default     = "us-east-1b"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9][a-z]$", var.availability_zone_secondary))
    error_message = "availability_zone_secondary deve ter sufixo de letra (ex: us-east-1b)."
  }
}

variable "project_name" {
  description = "Prefixo de nome usado em recursos e tags."
  type        = string
  default     = "tcc-poolers"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.project_name))
    error_message = "project_name deve ter 3-31 chars, lowercase, começar com letra, conter só [a-z0-9-]."
  }
}

variable "client_instance_type" {
  description = "EC2 do cliente (pgbench + collector + orquestrador). m5 = CPU dedicada (sem burst credits que contaminam dados sob carga sustentada)."
  type        = string
  default     = "m5.large"

  validation {
    condition     = can(regex("^[a-z][0-9][a-z]?\\.[a-z0-9]+$", var.client_instance_type))
    error_message = "client_instance_type deve ser um instance type AWS válido (ex: m5.large)."
  }
}

variable "pooler_instance_type" {
  description = "EC2 do pooler (roda 1 pooler por vez via Docker). m5.xlarge (4 vCPU) dá espaço pro multi-threading do PgCat brilhar vs PgBouncer single-thread — núcleo da hipótese H3."
  type        = string
  default     = "m5.xlarge"

  validation {
    condition     = can(regex("^[a-z][0-9][a-z]?\\.[a-z0-9]+$", var.pooler_instance_type))
    error_message = "pooler_instance_type deve ser um instance type AWS válido (ex: m5.xlarge)."
  }
}

variable "rds_instance_class" {
  description = "Classe RDS PostgreSQL. db.m5.large = 2 vCPU dedicada. Parâmetros travados (max_connections=415, shared_buffers=1GB) emulam footprint db.t3.medium sem o burst."
  type        = string
  default     = "db.m5.large"

  validation {
    condition     = can(regex("^db\\.[a-z][0-9][a-z]?\\.[a-z0-9]+$", var.rds_instance_class))
    error_message = "rds_instance_class deve ser uma classe RDS válida (ex: db.m5.large)."
  }
}

variable "rds_engine_version" {
  description = "Versão do PostgreSQL no RDS. Postgres 18 é suportado pelo RDS desde nov/2025."
  type        = string
  default     = "18.3"

  validation {
    condition     = can(regex("^1[0-9](\\.[0-9]+)?$", var.rds_engine_version))
    error_message = "rds_engine_version deve estar no formato MAJOR ou MAJOR.MINOR (ex: 18.3)."
  }
}

variable "rds_allocated_storage_gb" {
  description = "Storage gp3 do RDS (GB)."
  type        = number
  default     = 20

  validation {
    condition     = var.rds_allocated_storage_gb >= 20 && var.rds_allocated_storage_gb <= 100
    error_message = "rds_allocated_storage_gb deve estar entre 20 e 100 (bateria pontual, sem necessidade de mais)."
  }
}

variable "enable_rds_proxy" {
  description = "Cria RDS Proxy (6º setup da matriz). Default true — é parte do estudo. Desligue só pra debug/economia."
  type        = bool
  default     = true
}

variable "db_username" {
  description = "Usuário master do RDS."
  type        = string
  default     = "tcc"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{0,62}$", var.db_username))
    error_message = "db_username deve começar com letra, conter só [a-z0-9_], até 63 chars."
  }
}

variable "db_password" {
  description = "Senha master do RDS. Sem default — deve ser passada via tfvars/env."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.db_password) >= 12 && length(var.db_password) <= 128
    error_message = "db_password deve ter entre 12 e 128 caracteres."
  }
}

variable "db_name" {
  description = "Nome do database inicial criado no RDS."
  type        = string
  default     = "tcc"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{0,62}$", var.db_name))
    error_message = "db_name deve começar com letra, conter só [a-z0-9_]."
  }
}

variable "allowed_ssh_cidr" {
  description = "CIDR autorizado a fazer SSH na EC2 cliente (e a falar direto com RDS pra baseline). Sem default — exige passar."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.allowed_ssh_cidr))
    error_message = "allowed_ssh_cidr deve ser um CIDR válido (ex: 200.123.45.67/32)."
  }
}

variable "ssh_key_name" {
  description = "Nome de uma EC2 Key Pair pré-existente na conta AWS."
  type        = string

  validation {
    condition     = length(var.ssh_key_name) > 0
    error_message = "ssh_key_name é obrigatório (crie a key pair antes via AWS Console/CLI)."
  }
}

variable "tags" {
  description = "Tags aplicadas como default_tags do provider."
  type        = map(string)
  default = {
    Project   = "TCC-Poolers"
    Owner     = "Jean"
    ManagedBy = "Terraform"
  }
}
