# Secrets locais

Rode `scripts/bootstrap-secrets.ps1` no Windows ou `scripts/bootstrap-secrets.sh`
no Linux. Os arquivos `*.txt` e `*.json` desta pasta sao ignorados pelo Git e
montados como Docker secrets. Substitua `gcs_credentials.json` pelo JSON da
service account com acesso minimo aos buckets necessarios. O token Hugging Face
pode ficar vazio quando o registro usa somente checkpoints locais.

Se um token ja apareceu em `.env` ou historico de Git, remove-lo do arquivo nao
o revoga: revogue no provedor e gere outro antes do deploy.
