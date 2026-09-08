# API Performance and Multiworker Design

**Status:** aprovado em conversa em 2026-09-03

## Problema

A API de biblioteca está lenta mesmo em hardware forte porque `GET /api/objects/{object_id}/videos` executa trabalho proporcional ao acervo inteiro. Para cada vídeo concluído, `inspect_pipeline_entry()` abre todas as máscaras PNG, decodifica seus pixels, recalcula área/bbox e SHA-256. Essa varredura síncrona ocorre dentro de uma rota `async` servida por um único processo Uvicorn, bloqueando inclusive ações não relacionadas.

No frontend, ações de triagem persistem corretamente, mas aguardam um novo `GET /videos` antes de navegar. Assim, o custo global da listagem é percebido como atraso nos botões “Salvar, enviar ao SAM3 e próximo” e “Sem objeto”. Chamadas simultâneas de refresh também podem duplicar essa carga.

O worker CPU é durável e a fila PostgreSQL já usa leases e `FOR UPDATE SKIP LOCKED`, portanto pode ser replicado. O worker SAM3 deve continuar único por causa do limite de VRAM.

## Decisão

1. O manifesto `run.json` produzido pelo SAM3 será a fonte rápida para determinar completude na biblioteca.
2. A validação pixel a pixel continua obrigatória uma vez ao concluir o SAM3 e novamente em exportações/auditorias explícitas; ela deixa de ocorrer em cada listagem.
3. Runs antigos sem manifesto continuam compatíveis por meio de auditoria legada fora do event loop e cacheada pela assinatura dos arquivos de controle.
4. Toda montagem da biblioteca que ainda acesse disco/PostgreSQL síncronos será executada via `asyncio.to_thread`.
5. Navegação de triagem depende apenas do sucesso da mutação e da lista já carregada; refresh posterior ocorre em segundo plano.
6. Refreshes concorrentes da biblioteca serão coalescidos em uma única Promise.
7. O Compose executará dois workers CPU, cada um limitado a quatro CPUs/threads de FFmpeg e 8 GiB por padrão.
8. Haverá exatamente um `sam3-worker` e um processo web Uvicorn. Multiplicar o Uvicorn fica fora deste escopo porque locks e parte do contexto de workspace ainda vivem em memória.
9. Requisições acima de um segundo serão registradas com duração e receberão `Server-Timing`, permitindo medir regressões sem ferramentas adicionais.

## Invariantes de dados

- Máscaras PNG continuam sendo a anotação canônica.
- Checksum e contagem do manifesto são imutáveis para um run concluído.
- Um manifesto rápido só é aceito quando `status=done`, `frames_written=frame_count`, formato conhecido, quantidade de arquivos esperada e cardinalidade de checksums coerente.
- O estado “concluído” continua significando artefatos válidos e todos os frames revisados; término do job sozinho não promove o vídeo.
- Runs legados, revisões existentes, vídeos e frames não serão alterados ou apagados por esta mudança.
- Fila PostgreSQL, leases e idempotência continuam autoritativos para concorrência entre workers.

## Compatibilidade legada

Um run `png-1bit-v1` com manifesto completo usa somente metadados na listagem. Um run sem `artifacts` passa pela auditoria legada. O resultado dessa auditoria é cacheado em memória com chave composta pela localização e assinaturas (`mtime_ns` e tamanho) de `run.json` e `prompt.json`. A primeira auditoria roda em thread; alterações nesses arquivos invalidam naturalmente a entrada. `mask_review.json` não entra no cache: sua contagem é relida a cada consulta para uma revisão recém-salva aparecer imediatamente. O cache não substitui a validação explícita feita em exportação.

## Metas mensuráveis

- `GET /api/health` permanece responsivo abaixo de 200 ms enquanto ocorre uma auditoria legada fria.
- Após aquecimento, `GET /api/objects/boom/videos` tem p95 menor ou igual a 500 ms no acervo atual de 416 vídeos.
- A navegação após uma mutação bem-sucedida ocorre abaixo de 500 ms mesmo se o refresh de `/videos` ficar pendente.
- A listagem rápida nunca chama `Path.read_bytes()` para máscaras de runs com manifesto válido.
- Dois jobs CPU distintos podem ficar leased/running simultaneamente em workers diferentes.
- Nunca há mais de uma réplica `sam3-worker`.

## Fora do escopo

- Multiprocessar o FastAPI/Uvicorn.
- Executar mais de uma instância do SAM3.
- Migrar locks de edição e todo o índice do workspace para PostgreSQL.
- Alterar o formato de máscara ou o contrato de revisão.
- Introduzir Redis, Celery ou outro broker.
- Alterações destrutivas ou migração de dados.
