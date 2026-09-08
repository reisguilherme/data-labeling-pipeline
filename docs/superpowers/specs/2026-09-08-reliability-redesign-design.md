# Reliability Redesign — design aprovado

**Status:** estabilização incremental aprovada em conversa em 2026-09-08.

## Objetivo e limites

Tornar o pipeline previsível desde a biblioteca até a exportação, preservando integralmente o trabalho existente. O sistema continua usando FastAPI, React/Zustand, PostgreSQL, arquivos de anotação, PNGs binários, MinIO e SAM3. Não há reescrita completa, troca de broker nem conversão automática do acervo.

Este documento consolida e atualiza os designs de operações, editor e desempenho de setembro de 2026. As etapas são entregas incrementais do mesmo fluxo; o plano mantém gates independentes para armazenamento, backend, frontend e operação.

## Global Constraints

- Preservar integralmente vídeos, frames, máscaras, revisões, intervalos, decisões, exports e configurações existentes.
- Versionar código, testes, migrações, configuração de exemplo e documentação; excluir dados reais, modelos, backups e segredos do Git.
- O layout canônico de novos objetos é `data/<objeto>/{raw,dataset}`; `MST_WORKSPACE` aponta para `data` no host e `/workspace` no container.
- Usar armazenamento persistente por objeto; recriar containers não pode apagar dados de nenhum objeto.
- Não copiar, mover, renomear, converter nem reprocessar automaticamente dados existentes.
- Máscaras PNG binárias e revisões imutáveis continuam canônicas; a projeção é derivada e reconstruível.
- `GET /api/objects/{object_id}/videos` não abre nem decodifica PNGs, inclusive na primeira consulta de runs legados.
- Manter um processo web, exatamente dois workers CPU e exatamente um worker GPU SAM3.
- Cada worker CPU usa no máximo quatro CPUs, quatro threads FFmpeg e 8 GiB por padrão.
- Preservar Python `>=3.12,<3.13` e dependências já fixadas; nenhuma troca de framework ou broker.
- O editor não tem botão Aprovar: frame efetivamente carregado e visitado sem alteração fica `ok` localmente; edições ficam locais até Salvar trecho em lote.
- Um frame previamente editado nunca volta a `ok` somente porque foi visitado novamente.
- Nenhum vídeo vira concluído somente porque o job terminou; exige artefatos validados e todos os frames revisados persistidos.
- Alterações destrutivas de lifecycle continuam restritas à ação explícita existente de purge, com confirmação e inventário; esta estabilização não executa purge.

## Git, dados e compatibilidade

O repositório contém a aplicação e `data/.gitkeep`. Cada objeto possui duas raízes canônicas: `data/boom/raw` para mídia de entrada e `data/boom/dataset` para derivados e anotações. Os subdiretórios já usados pelo pipeline dentro dessas raízes permanecem válidos. Cadastro novo usa esse layout; cadastro existente mantém os caminhos registrados quando acessíveis. Caminho estrangeiro ou ausente não autoriza criar uma pasta vazia que esconda dados: apresentar diagnóstico com o caminho esperado e permitir apontar explicitamente para a raiz existente.

Bind mounts persistentes por objeto são a implementação inicial: o Compose recebe um override gerado a partir do registro, montando cada raiz existente em `/workspace/<objeto>/raw` e `/workspace/<objeto>/dataset` para web e workers. Os mesmos destinos são usados em todos os serviços. PostgreSQL, MinIO, configuração e cache de modelos mantêm os volumes persistentes existentes. Não criar volumes vazios sobre diretórios preenchidos; a geração de override apenas descreve mounts e não executa migração nem reinicia produção.

Antes de alterar configuração operacional, registrar branch, HEAD, arquivos modificados, raízes resolvidas e contagens leves. Verificar dados com amostras e inventário; hash integral somente em auditoria solicitada, para não impor uma leitura completa desnecessária. Rollback troca código/configuração e mantém os bytes originais.

## P0: biblioteca rápida e API disponível

`pipeline_state.inspect_pipeline_entry` atualmente abre PNGs em um laço por frame; `routers/library.py` chama esse código durante a listagem. Separar inspeção rápida de auditoria profunda. O fast path lê `run.json`, `prompt.json` e `mask_review.json`, valida formato, contagens e identidade e jamais lê pixels. Para manifesto incompleto/legado, retornar estado explícito `audit_required`, sem promover para concluído; a auditoria é uma ação explícita em job CPU que apenas verifica e publica metadados derivados.

Manifesto completo exige `status=done`, formato conhecido, `frame_count>0`, `frames_written=frame_count`, dimensões positivas, IDs de objetos únicos e cardinalidade coerente de arquivos/checksums. A validação completa permanece na finalização SAM3 e na exportação. Um índice não comprova que bytes não foram modificados fora da aplicação; a UI informa quando a validação foi feita e a exportação revalida.

Montagem da listagem, leitura de disco e chamadas PostgreSQL síncronas ficam fora do event loop por `asyncio.to_thread`, com limites de concorrência. O job de auditoria não pode ocupar o atendimento HTTP. Refreshes concorrentes por objeto são coalescidos. A navegação da triagem depende do sucesso da mutação e da lista carregada; o refresh ocorre depois, com falha visível sem desfazer a navegação já persistida.

## Projeção operacional

Adicionar `video_pipeline_projection`, chaveada por `(object_id, video_id)`, com estágio, estado, contagens, validade, assinatura da fonte, revisão e horário de atualização. Uma projeção ausente ou incompatível não vira evidência de conclusão. Metadados fonte e PNGs continuam autoritativos; a projeção não substitui o histórico de revisões.

Atualizar a projeção após commit de anotação, conclusão SAM3, lote de revisão, exportação e lifecycle. Usar upsert condicional por revisão monotônica para um evento antigo não sobrescrever um novo. Publicação parcial entre manifesto e banco precisa ser recuperável: gravar a intenção durável antes de publicar o manifesto e reconciliar por identidade/revisão; no erro, responder com a revisão canônica e sinalizar projeção pendente. Reconciliação lê metadados e não reprocessa mídia. Listagem pode servir o último snapshot coerente com indicador de atualização; decisões mutáveis e exportação validam a fonte.

## Frontend, frames e exportação

Toda resposta assíncrona é vinculada a objeto, vídeo, segmento e geração da requisição. Troca de escopo cancela requisições antigas; resposta atrasada não substitui a tela atual. Cache inclui objeto e revisão do artefato. Playback tem um único timer, avança somente depois que o frame corrente foi carregado e pausa no último frame, no erro, ao trocar de segmento e ao desmontar.

Salvar/enviar e Sem objeto persistem antes de avançar. Exportação é job durável com snapshot dos intervalos e referências às revisões; o worker abre contexto fresco por job. A UI mostra job criado, execução, erro recuperável e resultado disponível. Exportadores usam máscaras efetivas, incluindo edição vazia e instâncias retidas, e nunca oferecem um download parcial como pronto. Arquivar/restaurar atualiza o escopo ativo e a biblioteca; dados arquivados continuam acessíveis ao fluxo de restauração.

## Editor de máscara

O layout usa grade com barra superior para frame/navegação/Salvar trecho, trilho esquerdo de ferramentas, canvas central e painel de instâncias. Controles ocupam espaço próprio e não sobrepõem imagem ou timeline em 1280×720 e 1920×1080; em largura menor, painéis recolhem e mantêm acesso por teclado.

Ferramentas: mover, pincel/borracha circular ou quadrada, tamanho em pixels da imagem, cursor de diâmetro correspondente, opacidade, visibilidade por instância, zoom centrado no cursor, pan por Espaço, ajustar à tela, desfazer/refazer por frame e instância. A transformação entre viewport e pixels é única para pincel, cursor e bbox derivada. Remover uma instância não remove as demais; Sem objeto produz uma edição vazia explícita. Carregamentos de imagem antigos não podem repintar o canvas após trocar de frame/instância.

Somente um frame que terminou de carregar e foi apresentado à pessoa recebe visita local. Prefetch, erro de imagem e frames pulados não são visita. A visita mantém edição anterior; não modifica PNG original. Rascunhos são chaveados por `(object_id,video_id,segment,frame,revision)` e sobrevivem à navegação dentro do trecho. Salvar trecho envia um único lote com `expected_revision` por frame, incluindo `ok` visitados e `edited` modificados. Falha ou conflito mantém todos os rascunhos; sucesso limpa somente versões reconhecidas pelo servidor. Mudança de escopo com rascunho pede salvar ou descartar explicitamente.

## Concorrência e integridade

PostgreSQL continua coordenando leases e `FOR UPDATE SKIP LOCKED`. Dois workers CPU podem processar jobs independentes, mas jobs sobre o mesmo alvo de escrita precisam de exclusão no banco por `(object_id,video_id,kind)` e token de posse. Workers atrasados perdem o direito de publicar resultado após expirar o lease. FFmpeg tem limites e heartbeat; o contexto é reaberto ao iniciar cada job. GPU permanece singleton.

Lote de revisão valida todos os frames/revisões antes de publicar o manifesto. PNGs editados são imutáveis, gravados antes do ponteiro atômico; arquivos órfãos de falha não são removidos automaticamente. Retentativa com mesma chave idempotente retorna o resultado já persistido. Conflito de revisão é HTTP 409 e nunca sobrescreve silenciosamente o trabalho de outro cliente.

## Segurança, observabilidade e gates

Autenticação e autorização continuam nas rotas de leitura/escrita aplicáveis; token de worker não autoriza ações humanas. Resolver caminhos contra a raiz do objeto, rejeitar traversal e symlinks externos. Limitar quantidade de frames por lote, dimensões/pixels e bytes decodificados de PNG para evitar consumo ilimitado. Logs não contêm tokens, credenciais nem base64 de máscaras.

Registrar request ID, rota normalizada, método, status e duração; emitir `Server-Timing` e destacar requisições acima de um segundo. Jobs registram ID, objeto, tipo, worker, tentativa, tempo em fila, execução e erro. Readiness distingue dependências indisponíveis de processo vivo; métricas não usam nomes de arquivo ou video_id como rótulos de alta cardinalidade.

Critérios de aceite: nenhum PNG lido por `/videos`; p95 aquecido de `/videos` ≤500 ms no acervo de referência de 416 vídeos; `/api/health` ≤200 ms sob auditoria CPU; navegação após mutação ≤500 ms com refresh bloqueado; dois jobs CPU simultâneos e uma GPU; nenhuma contaminação entre objetos após navegação rápida; salvar lote e reabrir preserva exatamente pixels/status; exportação usa a revisão efetiva; reinício de containers preserva raízes e registros.

Testes incluem unidades para contratos puros, PostgreSQL real para leases/revisões, FFmpeg real em clipe sintético pequeno e browser real para canvas, foco, navegação, rede e layout. Resultados baseados apenas em busca de strings no código não contam como validação de comportamento. O teste GPU usa uma amostra existente somente se explicitamente selecionada para execução; não dispara reprocessamento em massa.
