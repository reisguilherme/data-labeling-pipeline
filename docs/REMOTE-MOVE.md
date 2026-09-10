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
CPU_WORKER_REPLICAS=2
CPU_WORKER_CPUS=4.0
CPU_WORKER_MEMORY=8G
MST_FFMPEG_THREADS=4
```

Mantenha os demais valores que ja funcionam, inclusive o registro do modelo e
os arquivos em `secrets/`. O cache Hugging Face pode ser baixado novamente; o
checkpoint finetunado precisa estar em `models/releases/...` e continuar
coerente com `config/models.local.yaml`.

Gere o override local que monta, separadamente, as raízes `raw` e `dataset` de
cada objeto nos três processos que as utilizam. O comando apenas lê
`objects.json` e grava configuração; não move, copia nem cria dados:

```bash
python3 scripts/generate_compose_object_mounts.py \
  --workspace /srv/boom-pipeline/data/workspace
docker compose config --quiet
```

`compose.override.yml` fica fora do Git, portanto uma atualização de código não
o sobrescreve. Regenere-o depois de cadastrar ou rehomear uma classe.

O `docker compose up -d` sobe duas replicas do worker CPU por padrao, cada uma
limitada a quatro CPUs, 8 GiB de RAM e quatro threads por processo FFmpeg. O
`sam3-worker` continua com exatamente uma replica para nao disputar VRAM. Para
alterar a concorrencia de forma permanente, edite `CPU_WORKER_REPLICAS` no
`.env`; nao combine `docker compose up --scale worker=N` com
`deploy.replicas`, pois seriam duas fontes de configuracao concorrentes.

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
`compose.override.yml`, `config/models.local.yaml`, `secrets/`, `data/`,
`models/` ou `backups/`.

Para a versao que torna a conclusao da exportacao independente do navegador,
reconstrua e recrie apenas a API e o worker CPU:

```bash
git pull --ff-only
docker compose build app worker
docker compose up -d --no-deps --force-recreate app worker
docker compose ps
docker compose logs --since 10m app worker
```

Para a versao com projecao operacional, mantenha PostgreSQL ativo, mas app e
workers parados. Depois do backup e do `git pull --ff-only`, construa a imagem e
inicie app/worker para aplicar as migrations aditivas 005/006. So entao rode o
dry-run. O reconciliador apenas verifica schema e marcadores, sem executar
migration; se a 006 nao tiver rodado, ele falha com orientacao e sem escrita:

```bash
docker compose build app worker
docker compose up -d --no-deps --force-recreate app worker
./scripts/reconcile-pipeline-projection.sh --limit 100
```

O relatorio e somente-leitura e separa `current`, `stale`, `missing`, `pending`,
`legacy` e `invalid`. Investigue erros de metadados antes do apply. Para reparar,
use lotes limitados e retome exatamente do token retornado:

Os wrappers usam a mesma selecao Compose do deploy, incluindo
`compose.override.yml`/`COMPOSE_FILE`. O inventario valida primeiro todas as
raizes registradas; um mount ausente aborta como erro operacional em vez de ser
interpretado como video removido.

```bash
./scripts/reconcile-pipeline-projection.sh --apply --limit 100
./scripts/reconcile-pipeline-projection.sh --apply --limit 100 --resume-token TOKEN
```

Repita ate `resume_token` ser `null`; reexecutar e seguro. O apply grava somente
nas tres tabelas aditivas de projecao. Nao grava anotacoes, revisoes, mascaras,
midia, registro de objetos, MinIO ou outros indices. Depois, recrie app/worker e
acompanhe os logs como no bloco anterior.

Rollback e por codigo: volte ao commit/imagem anterior e recrie app e worker,
sem remover as migrations 005/006 e sem apagar suas tabelas. As linhas de
projecao sao derivadas e inertes para o codigo antigo. O backup feito antes do
rollout ja inclui o banco inteiro, portanto nao copie essas linhas separadamente;
uma restauracao do `pg_dump` as recupera junto com o restante do PostgreSQL.

As copias imutaveis de snapshots usadas para publicar datasets aumentam o uso
de disco. Esse custo foi aceito para impedir que bytes de uma publicacao sejam
alterados por outra execucao e para manter rollback/auditoria reproduziveis;
dimensione `WORKSPACE_DIR` e o backup considerando essas copias.

Existe ainda um risco menor conhecido: se MinIO/get-frame falhar depois do
replace do manifesto de revisao, o indice SQL normalizado de revisao pode ficar
atrasado. A projecao continua reparavel pelo comando acima, mas o indice de
revisao e separado e exigira reconciliacao propria futura.

Nao use `docker compose down -v`: a opcao `-v` remove os volumes nomeados do
PostgreSQL e do MinIO. Recriar os containers com `up --force-recreate` preserva
esses volumes e o bind mount de `WORKSPACE_DIR`, incluindo todo o trabalho
manual ja realizado.

Depois da atualizacao, salve um video com intervalo, navegue imediatamente para
o proximo e confirme nos cards que o worker conclui a exportacao e enfileira o
SAM3 mesmo com a tela de triagem fechada.
