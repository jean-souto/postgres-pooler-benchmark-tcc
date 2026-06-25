#!/usr/bin/env bash
# ============================================================
# allow-my-ip.sh — reabre o SSH no seu IP atual (IP dinâmico).
#
# Detecta o IP público IPv4 atual, atualiza `allowed_ssh_cidr` no
# terraform.tfvars (fonte da verdade — sem drift) e aplica SÓ as regras de
# Security Group do SSH (rápido, ~segundos, não mexe em EC2/RDS).
#
# Use quando o SSH parar de funcionar após troca de rede/IP. A bateria em si
# roda em tmux na EC2 client e NÃO cai se o IP mudar — isto só restaura sua
# capacidade de reconectar.
#
# Pré-requisito: `terraform apply` inicial já rodou (os SGs existem no state).
# Uso:   ./allow-my-ip.sh         (usa AWS_PROFILE=recria-dev por default)
#        AWS_PROFILE=outro ./allow-my-ip.sh
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"
export AWS_PROFILE="${AWS_PROFILE:-recria-dev}"

# IPv4 público (o SG usa cidr_ipv4). curl -4 força IPv4; fallback na AWS.
IP="$(curl -4 -s --max-time 10 https://checkip.amazonaws.com 2>/dev/null || true)"
[ -z "$IP" ] && IP="$(curl -4 -s --max-time 10 ifconfig.me 2>/dev/null || true)"
IP="$(printf '%s' "$IP" | tr -d '[:space:]')"

if ! printf '%s' "$IP" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "ERRO: não consegui detectar um IPv4 válido (obtido: '$IP')." >&2
  exit 1
fi
CIDR="$IP/32"
echo "IP público atual: $CIDR  (profile: $AWS_PROFILE)"

if [ ! -f terraform.tfvars ]; then
  echo "ERRO: terraform.tfvars não existe. Rode o apply inicial primeiro." >&2
  exit 1
fi

# Atualiza a fonte da verdade (sem drift entre código e infra).
if grep -qE '^[[:space:]]*allowed_ssh_cidr' terraform.tfvars; then
  sed -i "s|^[[:space:]]*allowed_ssh_cidr.*|allowed_ssh_cidr = \"$CIDR\"|" terraform.tfvars
else
  echo "allowed_ssh_cidr = \"$CIDR\"" >> terraform.tfvars
fi

# Aplica só as regras de SSH (targeted — rápido, não toca EC2/RDS).
terraform apply -auto-approve \
  -target=aws_vpc_security_group_ingress_rule.client_ssh \
  -target=aws_vpc_security_group_ingress_rule.pooler_ssh_user

echo ""
echo "✓ SSH liberado pra $CIDR."
echo "  Reconecte: AWS_PROFILE=$AWS_PROFILE terraform output -raw client_ssh_command | bash"
