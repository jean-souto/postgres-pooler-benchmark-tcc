# pooler-stack — poolers na EC2 pooler (cloud)

Roda os 4 poolers via Docker (host network) na EC2 pooler. **Um pooler sobe
por vez**, controlado pelo orquestrador na EC2 client via SSH.

## Conteúdo

- `docker-compose.cloud.yml` — 4 serviços (profiles), host network, sem postgres
- `templates/*.tmpl` — configs com placeholders `__RDS_ENDPOINT__` e `__POOL_SIZE__`
- `generated/` — **populado pelo orquestrador** (não versionado): configs finais
  com RDS endpoint e pool_size resolvidos, por variante (`{setup}-pool{N}`)
- `userlist.txt` — **gerado pelo orquestrador**: `"tcc" "md5<hash>"` onde
  `hash = md5(db_password + "tcc")`. Não é versionado porque depende da senha
  real do RDS (≥12 chars, definida no `terraform apply`).

## Fluxo

1. `terraform apply` provisiona a EC2 pooler; `pooler.sh` instala Docker +
   autoriza a chave da client + cria `/opt/pooler-stack/`.
2. Orquestrador (client, início da bateria): gera `generated/` + `userlist.txt`
   com a senha real e faz `rsync` pra `pooler:/opt/pooler-stack/`.
3. Por run, via SSH:
   `cd /opt/pooler-stack && POOL_SIZE=8 docker compose -f docker-compose.cloud.yml --profile pgcat-tx up -d`
4. pgbench (client) → `pooler_ip:{6432,6433,6434,6435}` → RDS.

## Portas

| Setup | Porta |
|---|---|
| pgbouncer-tx | 6432 |
| pgbouncer-session | 6433 |
| pgcat-tx | 6434 |
| pgcat-session | 6435 |
