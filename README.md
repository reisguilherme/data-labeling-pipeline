# Boom Pipeline

Monorepo implantavel para triagem de videos, pre-anotacao SAM3, revisao de
mascaras e exportacao de datasets. A mascara binaria e a anotacao canonica;
bbox e area sao sempre recalculadas a partir dela.

Esta pasta e autocontida para build e deploy. O acervo existente, checkpoints
e backups nao ficam no repositorio: entram pelos mounts `WORKSPACE_DIR`,
`./models` e `./backups` definidos no Compose.

```text
services/app/                 FastAPI, React e worker CPU
services/sam3-worker/         runner GPU SAM3
packages/pipeline-core/       contratos compartilhados
migrations/                   schema PostgreSQL
config/                       registro versionado de modelos
scripts/                      migracao, backup, restore e verificacao
tests/                        testes de core, infraestrutura e isolamento
```

## Subir localmente

Requisitos: Docker Engine + Compose recente, NVIDIA Container Toolkit, GPU
compativel e acesso apenas pela propria maquina.

1. Copie `.env.example` para `.env` e ajuste `WORKSPACE_DIR`, `UID` e `GID`.
2. Gere os secrets:
   - Windows: `./scripts/bootstrap-secrets.ps1`
   - Linux: `./scripts/bootstrap-secrets.sh`
   Substitua `secrets/gcs_credentials.json` pelo JSON de uma service account
   GCS de privilegio minimo; o placeholder `{}` e intencionalmente recusado.
   O checkpoint de treino `.pt` contem o detector finetunado, mas o predictor
   de video tambem precisa dos modulos de tracking da base oficial. Solicite
   acesso a `facebook/sam3` no Hugging Face e grave um token de leitura, sozinho
   e sem aspas, em `secrets/hf_token.txt`.
3. Crie uma release imutável do checkpoint `.pt` e use o SHA-256 informado pelo
   script:
   `./scripts/stage-model.ps1 -SourcePath ../model.pt -ModelId sam3-finetuned -Version v1`.
   Copie `config/models.yaml` para `config/models.local.yaml`, atualize
   `model_id`, caminho e SHA-256 e associe cada `object_id` ao modelo.
4. Valide: `docker compose -f compose.yml config --quiet`.
5. Suba: `docker compose -f compose.yml up -d --build`.
6. Abra `http://127.0.0.1:8000`.

Por padrao somente a porta `127.0.0.1:8000` e publicada. PostgreSQL e MinIO
existem apenas na rede interna. Para mover a instalacao e os dados para uma
maquina remota que atende em `0.0.0.0:3000`, siga
[`docs/REMOTE-MOVE.md`](docs/REMOTE-MOVE.md). Essa modalidade exige rede
confiavel e firewall, pois a aplicacao ainda nao tem autenticacao de acesso.
O mesmo guia inclui a atualizacao incremental do codigo sem substituir
`data/`, modelos, secrets ou os volumes persistentes.

## Servicos

- `app`: FastAPI e SPA React; faz bootstrap de schema e buckets.
- `worker`: fila CPU PostgreSQL para downloads GCS, proxies/export FFmpeg e
  exportacoes de datasets duraveis.
- `sam3-worker`: sessoes interativas prioritarias e propagacao GPU.
- `postgres`: metadados, jobs, leases, auditoria e revisoes normalizadas.
- `minio`: buckets privados `videos`, `frames`, `masks`, `models`, `datasets` e
  `backups`.

O adaptador de filesystem continua disponivel para ler o acervo legado. Em
container, GCS, FFmpeg, a fila SAM3 e as exportacoes usam PostgreSQL quando
`DATABASE_URL` esta presente. Claims usam `FOR UPDATE SKIP LOCKED`; heartbeat,
expiracao, tentativas, cancelamento e retomada sobrevivem ao restart. Cada run
SAM3 concluido tambem normaliza modelo, prompt, mascaras, bbox e area no banco.
Ao terminar a extracao de frames, o worker CPU confirma o resultado diretamente
na API interna antes de concluir o job. A navegacao ou o fechamento da tela nao
interrompem a promocao do video nem o enfileiramento do SAM3.

## Operacao da interface

A aplicacao usa URLs estaveis, que podem ser salvas como favoritos:

- `/objects/<objeto>/overview`: visao geral e proximas acoes;
- `/objects/<objeto>/screening`: grade de triagem;
- `/objects/<objeto>/sam3`: grade de pre-anotacao;
- `/objects/<objeto>/review`: grade de revisao;
- `/objects`: cadastro e ciclo de vida dos objetos;
- `/export`: construtor global de datasets.

As tres etapas operacionais possuem indicadores e filtros proprios. Um video
so aparece como `Concluido` depois que o run SAM3 possui todas as mascaras
validas e todos os frames foram aprovados ou editados na revisao. Terminar a
propagacao, sozinho, nao conclui o fluxo.

Objetos podem ser renomeados e ter sua classe alterada na tela `Objetos`.
Arquivar remove o objeto da operacao diaria sem apagar historico; restaurar o
torna ativo novamente. A exclusao permanente exige que o objeto esteja
arquivado, nao tenha jobs nem sessoes ativas e que o operador digite a frase
de confirmacao exibida. Datasets ja exportados sao preservados para auditoria.

O construtor em `/export` e global: permite combinar um ou varios objetos e
filtrar videos, tags e intervalos antes de gerar qualquer uma das quatro
combinacoes `YOLO/COCO` x `detection/segmentation`. O preview informa a classe
deterministica, quantidade elegivel, bloqueios e avisos antes de enfileirar o
job. Arquivos de objetos diferentes recebem namespace para impedir colisao
quando os nomes de video coincidem.

## Modelos SAM3

O registro declarativo fica em `config/models.local.yaml`. Cada release `.pt`
fica em `models/releases/<model_id>/<versao>/model.pt`, montada somente para
leitura. Um checkpoint pode conter várias classes/objetos;
`assignments.objects` controla quais objetos usam cada `model_id`.

O worker verifica SHA-256 antes de carregar. Checkpoints completos de video sao
carregados estritamente com `load_from_HF=False`. Para checkpoints de treino do
detector, como o `model.pt` atual, ele baixa a base oficial no primeiro uso,
valida explicitamente as unicas chaves de video que podem faltar e aplica o
detector finetunado sobre ela. O download fica persistido em
`/model-cache/huggingface`; reinicios nao o repetem. Trocar `model_id`, checksum,
commit ou parametros invalida o marcador do run e gera nova inferencia.

Depois de alterar `hf_token.txt`, reinicie apenas o worker GPU:

```powershell
docker compose up -d --force-recreate sam3-worker
docker compose logs -f sam3-worker
```

## Revisao e exportacao

O primeiro frame da pre-anotacao usa `prompt.frame_idx`; a bbox vem de override
quando existente e, caso contrario, diretamente de `prompt.objects`.

O editor de mascara oferece selecao de instancia, overlay/opacidade, pincel,
borracha, tamanho, undo/redo, aprovacao do bruto e estado explicito "sem
objeto". O save envia `expected_revision`; concorrencia obsoleta responde 409.
O bruto nunca e sobrescrito e cada delta mantem historico e tombstones.

Na tela de dataset escolha separadamente:

- formato: `YOLO` ou `COCO`;
- tarefa: `detection` ou `segmentation`.

COCO-seg usa RLE comprimido lossless. YOLO-seg usa contorno normalizado e lista
no manifesto perdas de buracos/componentes. Segmentacao e bloqueada se houver
run legado, frame ausente ou PNG invalido. YOLO/COCO detection derivam a menor
caixa de todos os pixels positivos.

## Inventario e cutover

O dry-run nao escreve no acervo:

```powershell
./.venv/Scripts/python.exe scripts/migrate_legacy.py --workspace $env:WORKSPACE_DIR --object boom
```

Depois de validar o relatorio e fazer backup, a importacao PostgreSQL idempotente
e executada dentro da imagem (continua copy-only em relacao ao legado):

```powershell
docker compose -f compose.yml run --rm app migrate-legacy --object boom
```

Para executar o inventário dentro da imagem sem iniciar PostgreSQL ou MinIO:

```powershell
docker compose run --rm --no-deps --entrypoint python app /opt/scripts/migrate_legacy.py --workspace /workspace --object boom
```

No inventario atual ele encontra 416 videos brutos, 129 segmentos, 9.722
frames, 129 prompts, 847 decisoes de filtro (416 keep, 372 trash, 59
duplicados) e cinco arquivos de revisao legada (272 frames revisados). A
migracao grava essas decisoes em `video_filter_decisions`, inclusive nomes
descartados que nunca foram baixados.
O cutover deve ser
copy-only: mantenha o workspace antigo gravavel apenas durante validacao, rode
um segmento pequeno com o checkpoint escolhido, reprocese os 129 segmentos e
so entao torne o legado read-only. Nunca promova bbox revisada para mascara sem
revisao visual.

## Backup e restore

`./scripts/backup.ps1` cria um diretorio versionado em `backups/` contendo
`pg_dump`, configuracao, manifestos do workspace, mirror de todos os buckets e
checksums. Restore exige confirmacao explicita:

```powershell
./scripts/restore.ps1 20260901T120000Z
```

## Verificacao

Crie o ambiente de desenvolvimento uma vez:

```powershell
python -m venv .venv
./.venv/Scripts/pip.exe install -r requirements.lock
```

```powershell
./scripts/verify.ps1
```

No Linux, use `./scripts/verify.sh` depois de criar o venv da API.

### Benchmark da biblioteca

Com a versao desta branch ja construida e implantada no host que possui o
acervo de referencia, execute o benchmark somente-leitura da listagem:

```powershell
python scripts/benchmark-library.py --base-url http://127.0.0.1:8000 --object-id boom --samples 20
```

O script faz um `GET` de aquecimento e depois mede 20 `GETs` sequenciais, com
timeout finito. Ele informa apenas quantidade de amostras, p50, p95, minimo e
maximo; nao imprime nomes de videos, IDs de objeto nem credenciais. O alvo de
aceite de referencia e p95 aquecido `<= 500 ms` para 416 videos. Esse valor deve
ser medido no host implantado depois do build desta branch; testes unitarios nao
demonstram nem substituem essa medicao operacional.

Antes de qualquer deploy, revogue tokens HF/GCS/worker que ja tenham aparecido
em `.env`, logs ou historico e gere novos. Apagar o valor local nao revoga a
credencial no provedor.
