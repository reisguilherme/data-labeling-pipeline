# Mover para uma maquina remota simples

Este procedimento move o projeto novo e todo o trabalho ja feito, sem copiar o
restante do antigo `workspace`. O destino publicara a interface em
`0.0.0.0:3000`; PostgreSQL e MinIO continuam internos ao Compose.

> A aplicacao ainda nao possui autenticacao de acesso. Use uma rede privada e
> um firewall que permita a porta 3000 somente para os IPs necessarios. Nao a
> exponha diretamente na Internet.

## 1. Preparar a copia local

Na maquina atual, pare somente quem poderia escrever durante a copia e crie um
backup consistente do banco e do MinIO:

```powershell
cd C:\Users\Guilherme\Downloads\boom-pipeline\workspace\boom-pipeline-prod
docker compose stop app worker sam3-worker
docker compose run --rm --no-deps app backup
```

Monte um workspace portatil dentro do proprio projeto. Ele le `objects.json` e
copia somente as raizes exatas dos objetos cadastrados (por exemplo,
`boom/raw`, `boom/dataset`, `microfone/raw` e `microfone/dataset`). O cache
descartavel `dataset/_cache` — proxies, thumbs e janelas de navegacao — e
explicitamente excluido e sera recriado sob demanda no servidor. Permanecem
videos, frames exportados, decisoes de triagem, prompts, revisoes e filas
locais:

```powershell
.\scripts\prepare-portable-workspace.ps1 `
  -SourceWorkspace C:\Users\Guilherme\Downloads\boom-pipeline\workspace
```

Depois copie a pasta `boom-pipeline-prod` inteira para o servidor, incluindo
`data/workspace`, `backups`, `models` e `secrets`. Nao copie `.venv` nem
`services/app/web/node_modules`.

## 2. Preparar o servidor

No servidor Linux, instale Docker Engine com Compose e o NVIDIA Container
Toolkit. Copie a pasta para, por exemplo, `/srv/boom-pipeline`, e proteja os
segredos:

```bash
cd /srv/boom-pipeline
chmod 700 secrets
chmod 600 secrets/*
cp .env.example .env
```

Edite `.env` para:

```dotenv
WORKSPACE_DIR=/srv/boom-pipeline/data/workspace
UID=1000
GID=1000
APP_BIND=0.0.0.0
APP_PORT=3000
```

Mantenha os demais valores que ja funcionam, inclusive o registro do modelo e
os arquivos em `secrets/`. O cache Hugging Face pode ser baixado novamente; o
checkpoint finetunado precisa estar em `models/releases/...` e continuar
coerente com `config/models.local.yaml`.

## 3. Restaurar o estado e subir

Primeiro suba apenas os servicos que recebem o restore:

```bash
docker compose up -d postgres minio
docker compose ps
```

Descubra o nome da pasta de backup copiada em `backups/` e restaure-a. Este
comando sobrescreve o banco e o MinIO vazios do servidor novo:

```bash
docker compose run --rm --no-deps app restore /backups/AAAAmmddTHHMMSSZ --confirm
docker compose up -d --build
docker compose ps
```

Abra `http://IP-DO-SERVIDOR:3000`. Confirme objetos, contagens, uma imagem de
frame e um artefato SAM3 antes de desligar a maquina antiga. Mantenha a copia
antiga intacta ate concluir essa validacao.

## 4. Atualizar somente o codigo depois da migracao

O trabalho operacional nao fica no Git. Os videos, frames, anotacoes e
revisoes continuam em `WORKSPACE_DIR`; PostgreSQL e MinIO continuam nos volumes
nomeados do Docker; modelos, backups, configuracao local e secrets permanecem
nas pastas ignoradas pelo repositorio. Portanto uma atualizacao de codigo nao
exige copiar o acervo novamente.

Antes de atualizar, confirme que o servidor esta no diretorio correto e crie
um backup consistente:

```bash
cd /srv/boom-pipeline
docker compose ps
docker compose stop app worker sam3-worker
docker compose run --rm --no-deps app backup
```

Atualize apenas os arquivos versionados com `git pull --ff-only` (depois que a
branch aprovada tiver sido enviada ao remoto). Nao substitua nem apague `.env`,
`config/models.local.yaml`, `secrets/`, `data/`, `models/` ou `backups/`.

Para a versao que torna a conclusao da exportacao independente do navegador,
reconstrua e recrie apenas a API e o worker CPU:

```bash
git pull --ff-only
docker compose build app worker
docker compose up -d --no-deps --force-recreate app worker
docker compose ps
docker compose logs --since 10m app worker
```

Nao use `docker compose down -v`: a opcao `-v` remove os volumes nomeados do
PostgreSQL e do MinIO. Recriar os containers com `up --force-recreate` preserva
esses volumes e o bind mount de `WORKSPACE_DIR`, incluindo todo o trabalho
manual ja realizado.

Depois da atualizacao, salve um video com intervalo, navegue imediatamente para
o proximo e confirme nos cards que o worker conclui a exportacao e enfileira o
SAM3 mesmo com a tela de triagem fechada.
