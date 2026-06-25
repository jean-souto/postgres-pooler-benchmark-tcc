# ============================================================
# outputs.tf
#
# Os endpoints aqui são consumidos pelo orquestrador em modo cloud
# via `terraform output -json` (ver orchestrator/cloud_targets.py).
# ============================================================

output "client_public_ip" {
  description = "IP publico da EC2 cliente."
  value       = aws_instance.client.public_ip
}

output "client_private_ip" {
  description = "IP privado da EC2 cliente (mesma subnet do RDS/pooler)."
  value       = aws_instance.client.private_ip
}

output "client_ssh_command" {
  description = "Comando SSH pra entrar na EC2 cliente."
  value       = "ssh -i ~/.ssh/${var.ssh_key_name}.pem ubuntu@${aws_instance.client.public_ip}"
}

output "pooler_private_ip" {
  description = "IP privado da EC2 pooler — usado pelo pgbench (client) pra falar com os poolers (portas 6432-6435)."
  value       = aws_instance.pooler.private_ip
}

output "pooler_docker_host" {
  description = "DOCKER_HOST pro docker context remoto do orquestrador (sobe/desce poolers na EC2 pooler)."
  value       = "ssh://ubuntu@${aws_instance.pooler.private_ip}"
}

output "rds_endpoint" {
  description = "Endpoint do RDS PostgreSQL (acessivel pela VPC)."
  value       = aws_db_instance.main.address
}

output "rds_port" {
  description = "Porta do RDS."
  value       = aws_db_instance.main.port
}

output "rds_proxy_endpoint" {
  description = "Endpoint do RDS Proxy (null se enable_rds_proxy=false)."
  value       = local.create_proxy ? aws_db_proxy.main[0].endpoint : null
}

# Mapa consumido pelo orquestrador: setup -> (host, porta).
# pgbench (na EC2 client) usa esses targets. Portas dos poolers self-hosted
# batem com as do pooler-stack/docker-compose.cloud.yml.
output "targets" {
  description = "Targets por setup pro orquestrador cloud (host:porta de cada caminho de dados)."
  value = {
    none              = "${aws_db_instance.main.address}:5432"
    pgbouncer-tx      = "${aws_instance.pooler.private_ip}:6432"
    pgbouncer-session = "${aws_instance.pooler.private_ip}:6433"
    pgcat-tx          = "${aws_instance.pooler.private_ip}:6434"
    pgcat-session     = "${aws_instance.pooler.private_ip}:6435"
    rds-proxy         = local.create_proxy ? "${aws_db_proxy.main[0].endpoint}:5432" : null
  }
}

# Preços REAIS us-east-1 on-demand (AWS Pricing API + doc, mai/2026):
# m5.large=$0.096/h, m5.xlarge=$0.192/h, db.m5.large PostgreSQL=$0.178/h,
# RDS Proxy=$0.015/vCPU-h × 2 vCPU (mínimo)=$0.030/h, storage gp3 (~70GB) +
# Secrets ~$0.009/h. Total ~$0.505/h. Bateria expandida ~41h ≈ $21.
output "estimated_hourly_cost_usd" {
  description = "Custo on-demand us-east-1 (Pricing API, mai/2026). Storage incluído (~$0.009/h)."
  value = format(
    "Client (%s)=$0.096 + Pooler (%s)=$0.192 + RDS (%s)=$0.178 + Proxy=$%.3f + storage~$0.009 => total ~$%.3f/h",
    var.client_instance_type,
    var.pooler_instance_type,
    var.rds_instance_class,
    local.create_proxy ? 0.030 : 0.0,
    0.096 + 0.192 + 0.178 + 0.009 + (local.create_proxy ? 0.030 : 0.0)
  )
}
