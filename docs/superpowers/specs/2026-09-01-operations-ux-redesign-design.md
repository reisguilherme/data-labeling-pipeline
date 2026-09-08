# Redesenho operacional da aplicação — especificação

**Data:** 2026-09-01  
**Estado:** aprovado em conversa  
**Escopo:** FastAPI, React, PostgreSQL, MinIO e worker CPU existentes

## 1. Contexto

A interface atual mistura o estado da triagem manual com o estado do SAM3 e da
revisão. O indicador “concluído” representa hoje um vídeo exportado pela
triagem, mesmo que ele nunca tenha sido propagado ou validado. As ações de SAM3
e revisão também dependem do card selecionado ou aparecem apenas no hover, e a
exportação de dataset está presa ao objeto ativo.

O redesenho deve tornar o pipeline legível como quatro áreas operacionais
distintas — Triagem, SAM3, Revisão e Concluídos — sem substituir os editores que
já funcionam. A gestão de objetos/classes e a exportação passam a ser áreas
globais.

## 2. Objetivos

- Separar claramente as filas e ações de Triagem, SAM3 e Revisão.
- Fazer “Concluídos” significar exclusivamente material SAM3 integralmente
  revisado e válido.
- Exibir uma grade própria de vídeos em cada etapa, com tags e ações explícitas.
- Preservar os editores atuais de triagem, prompt SAM3 e máscaras.
- Permitir renomear a apresentação do objeto e sua classe de dataset.
- Oferecer arquivamento reversível e exclusão permanente protegida.
- Exportar um dataset multiclasse a partir de um ou vários objetos.
- Manter filtros por tags e vídeos, splits por vídeo e as quatro combinações de
  formato/tarefa existentes.
- Manter compatibilidade com dados migrados, jobs em andamento e endpoints
  atuais durante a transição.

## 3. Fora de escopo

- Autenticação real, autorização por papel e publicação em LAN.
- Alteração dos algoritmos de inferência, edição de máscara ou polygonização.
- Renomear `object_id` ou mover automaticamente raízes de armazenamento.
- Arrastar cards manualmente entre etapas. A etapa é derivada dos dados, nunca
  escolhida pelo usuário.
- Excluir datasets históricos quando um objeto é excluído.

## 4. Princípios de UX

1. **Uma etapa, uma pergunta:** cada área mostra somente vídeos e ações daquela
   fase do trabalho.
2. **Estado derivado, não decorativo:** tags, contagens e ações vêm da mesma
   classificação autoritativa do backend.
3. **Ação primária visível:** iniciar, continuar, revisar e tentar novamente não
   dependem de hover.
4. **Progresso verificável:** a revisão mostra frames revisados sobre frames
   esperados; SAM3 mostra segmentos concluídos sobre segmentos esperados.
5. **Falha recuperável:** erros exibem a causa resumida e uma ação válida.
6. **Operações destrutivas deliberadas:** arquivar é o caminho normal; excluir
   permanentemente exige pré-condições, inventário e confirmação digitada.
7. **Reprodutibilidade:** exportações congelam a interpretação de classes,
   filtros, runs e splits no manifesto.

## 5. Arquitetura de informação

### 5.1 Shell persistente

A aplicação autenticada usa um shell único:

- barra superior com marca, seletor de objeto, busca contextual e usuário;
- sidebar com `Visão geral`, `Triagem`, `SAM3`, `Revisão`, `Concluídos`,
  `Exportar datasets`, `Objetos e classes` e `Configurações`;
- área principal para boards ou editores;
- sidebar recolhível nos editores para preservar espaço horizontal.

O seletor oferece objetos ativos. `Visão geral` e `Exportar datasets` também
aceitam “Todos os objetos”; as etapas produtivas exigem um objeto específico.
Objetos arquivados aparecem apenas na administração.

### 5.2 Rotas de interface

- `/objects/:objectId/overview`
- `/objects/:objectId/triage`
- `/objects/:objectId/sam3`
- `/objects/:objectId/review`
- `/objects/:objectId/completed`
- `/objects/:objectId/videos/:videoId/triage`
- `/objects/:objectId/videos/:videoId/sam3`
- `/objects/:objectId/videos/:videoId/review`
- `/export`
- `/objects`

Recarregar ou compartilhar a URL conserva objeto, etapa e vídeo. Busca, filtro
e ordenação ficam em query parameters quando alterados pelo operador.

## 6. Modelo autoritativo do pipeline

Cada vídeo recebe `pipeline_stage`, `stage_status` e `stage_progress` no backend.
A precedência é a seguinte:

1. `discarded`: a triagem marcou explicitamente “sem objeto”.
2. `triage`: anotação `pending` ou `in_progress`.
3. `sam3`: a triagem terminou com ao menos um intervalo, mas não existe um run
   SAM3 efetivo completo e válido. Inclui pronto, fila, processamento,
   cancelamento, erro e run parcial.
4. `review`: existe run SAM3 efetivo completo, com todos os artefatos brutos
   esperados válidos, mas nem todos os frames têm revisão efetiva válida.
5. `completed`: todos os frames esperados de todos os segmentos do run efetivo
   têm uma revisão mais recente `ok` ou `edited`, e todas as instâncias efetivas
   referenciam artefatos válidos ou estados vazios explícitos.

Um frame só é revisado quando sua revisão mais recente foi persistida e
indexada. Navegar pelo frame não conta como revisão. Uma máscara vazia aprovada
conta; arquivo ausente não conta.

O denominador da revisão é `expected_frames` do run efetivo. Para múltiplas
instâncias, a revisão do frame precisa resolver todas elas por retenção, edição,
exclusão explícita ou estado vazio. Qualquer checksum/dimensão inválido remove o
vídeo de `completed` e o coloca em `review` com status `inconsistent`.

Ao iniciar um novo run para um vídeo previamente concluído, o novo run passa a
ser efetivo e o vídeo deixa `completed`. Trocar somente a atribuição de modelo,
sem iniciar um novo run, não invalida retrospectivamente o run revisado.

Vídeos `discarded` não entram em `completed`, nem em contagens de material
validado. Eles ficam no filtro “Sem objeto” dentro de Triagem e são excluídos de
datasets por padrão.

## 7. Telas produtivas

### 7.1 Visão geral

Mostra quatro cards clicáveis:

- Triagem: pendentes e em andamento;
- SAM3: prontos, em fila/processamento e com erro;
- Revisão: aguardando, em andamento e inconsistentes;
- Concluídos: somente validados.

O botão “Continuar próximo trabalho” prioriza uma atividade já iniciada pelo
usuário, depois Revisão, SAM3 e Triagem. A visão geral nunca soma descartados a
concluídos.

### 7.2 Triagem

Filtros locais: `Todos`, `Pendentes`, `Em andamento` e `Sem objeto`.

Cards exibem thumbnail, nome, duração, responsável pelo lock e estado. Ações:
`Triar`, `Continuar`, `Consultar` e, quando aplicável, `Restaurar para triagem`.
O botão de página “Iniciar próximo” usa a regra de lock já existente.

Ao concluir com intervalos, o vídeo sai da fila de Triagem e entra em SAM3. Ao
marcar sem objeto, entra em `discarded`.

### 7.3 SAM3

Filtros locais: `Prontos`, `Na fila`, `Processando`, `Com erro` e `Cancelados`.

Cards exibem segmentos, modelo atribuído, tentativas, progresso e mensagem de
erro. Ações: `Configurar e propagar`, `Acompanhar`, `Cancelar`, `Tentar
novamente` e `Detalhes`.

Quando o run completo é validado no backend, o vídeo sai de SAM3 e entra em
Revisão. Falha ou artefato bruto inválido permanece em SAM3.

### 7.4 Revisão

Filtros locais: `Aguardando`, `Em andamento` e `Com inconsistência`.

Cards mostram `reviewed_frames / expected_frames`, barra de progresso,
responsável atual e número de instâncias. Ações: `Revisar`, `Continuar`, `Ver
problema` e `Detalhes`.

O editor atual de máscaras é preservado. Salvar `ok` ou `edited` atualiza o
progresso. O último frame válido move automaticamente o vídeo para Concluídos;
não existe um botão que force essa transição.

### 7.5 Concluídos

Contém somente vídeos em `completed`. Cards mostram data da última revisão,
responsável, run/modelo e totais de frames/instâncias. Ações: `Consultar`,
`Reabrir revisão` e `Adicionar à exportação`.

Reabrir não apaga revisões. Uma nova edição cria outra revisão imutável. O vídeo
permanece concluído se todos os frames continuarem válidos; retorna a Revisão se
a revisão efetiva de qualquer frame ficar ausente ou inconsistente.

## 8. Componentes visuais

O design mantém tema escuro, superfícies zinc, verde esmeralda para ação
primária/validado, violeta para SAM3/Revisão, âmbar para trabalho em andamento e
vermelho apenas para erro ou destruição.

Componentes compartilhados:

- `AppShell`, `GlobalHeader` e `PipelineSidebar`;
- `StageHeader`, `StageFilters` e `StageBoard`;
- `PipelineVideoCard`, `StatusBadge` e `ProgressSummary`;
- `EmptyState`, `InlineError`, `RecoveryAction` e skeletons;
- `ConfirmDialog` e `DestructiveConfirmDialog`;
- seletores pesquisáveis de objetos, tags e vídeos.

Cards usam botões reais e foco por teclado. Informações essenciais não dependem
de cor ou hover. Em telas estreitas, a sidebar recolhe, filtros quebram linha e
a grade reduz colunas; editores continuam priorizando a área de imagem.

## 9. Objetos e classes

### 9.1 Edição

O operador pode alterar:

- `display_name`;
- nome da classe usado em novos datasets;
- URI GCS e regras de sugestão já suportadas.

`object_id`, raízes locais e chaves históricas não são renomeados. O nome da
classe não pode ficar vazio e deve ser único entre objetos ativos após
normalização case-insensitive. Runs continuam ligados por IDs; datasets antigos
mantêm o snapshot do manifesto. Novos exports usam o nome atual.

### 9.2 Arquivamento

Arquivar é reversível, remove o objeto dos seletores produtivos e impede novos
jobs, mas preserva arquivos, blobs, metadados, revisões e datasets. Restaurar
devolve o objeto após validar que seu nome de classe não colide com outro ativo.

### 9.3 Exclusão permanente

Exclusão só é oferecida para objeto arquivado, sem locks, sessões ou jobs
ativos. O operador digita `EXCLUIR <object_id>`.

A operação é um job CPU auditado que:

1. cria inventário final com contagens e checksums no bucket/diretório de
   backups;
2. remove registros PostgreSQL associados em transação;
3. remove blobs MinIO prefixados pelo `object_id`;
4. remove somente raízes locais configuradas que resolvam dentro do workspace
   gerenciado;
5. preserva datasets já exportados e seus manifestos;
6. registra caminhos externos ignorados e o resultado final.

Falha em qualquer etapa anterior à transação deixa o objeto arquivado. A UI não
declara sucesso parcial como exclusão concluída.

## 10. Exportação global multiclasse

`Exportar datasets` opera fora do objeto ativo. O operador escolhe um ou vários
objetos ativos que tenham vídeos concluídos. O resultado é um único dataset
multiclasse.

### 10.1 Seleção e filtros

- objetos/classes;
- tags da triagem, com OR dentro do grupo e AND entre grupos;
- vídeos identificados pelo par `{object_id, video_id}`;
- tarefa `detection` ou `segmentation`;
- formato `yolo` ou `coco`;
- frações de validação e teste;
- inclusão opcional de frames vazios explicitamente revisados.

Somente vídeos `completed` são elegíveis; isso não é um checkbox removível. O
preview é obrigatório e bloqueia máscaras ausentes/inválidas, classes
duplicadas, seleção vazia e splits impossíveis.

### 10.2 Classes e arquivos

IDs de classe são atribuídos pela ordenação estável de `object_id`, não pela
ordem dos cliques. Para um único objeto, o ID é zero. O manifesto registra o
mapa completo. Arquivos recebem namespace do objeto para impedir colisões de
`video_id`, segmento ou frame.

O split é determinístico e agrupado por `{object_id, video_id}`. Frames do mesmo
vídeo nunca atravessam splits. O preview apresenta totais gerais e por classe.

### 10.3 Saída e histórico

Novos datasets globais ficam em `<workspace>/_datasets/<name>` e no prefixo
`datasets/global/<name>` do MinIO. A tela lista tanto datasets globais quanto os
legados por objeto, identificando seu escopo.

O manifesto congela:

- classes e IDs;
- objetos e vídeos selecionados;
- formato, tarefa e parâmetros;
- filtros e flags;
- runs, modelos, commits e checksums;
- seed e atribuição dos splits;
- perdas de polygonização;
- checksums dos artefatos finais.

## 11. Contratos de backend

### 11.1 Pipeline

- `GET /api/pipeline/summary?object_ids=...`: contagens agregadas.
- `GET /api/objects/{object_id}/pipeline`: lista vídeos com
  `pipeline_stage`, `stage_status`, `stage_progress`, ações permitidas e dados
  atuais de lock/job.

Os endpoints existentes de vídeos, SAM3 e revisão permanecem. Durante a
transição, a resposta de vídeos também pode incluir os novos campos sem remover
campos antigos.

### 11.2 Objetos

- `PATCH /api/objects/{object_id}` continua editando campos permitidos.
- `POST /api/objects/{object_id}/archive`.
- `POST /api/objects/{object_id}/restore`.
- `POST /api/objects/{object_id}/purge` com confirmação e retorno de `job_id`.

### 11.3 Datasets globais

- `POST /api/datasets/preview`.
- `POST /api/datasets/export`.
- `GET /api/datasets`.

Os endpoints `/api/objects/{object_id}/dataset/*` continuam válidos para
compatibilidade e usam internamente o mesmo exportador com um contexto.

## 12. Persistência e desempenho

A classificação usa PostgreSQL como fonte primária: anotações, jobs, runs,
artefatos, frame instances e revisões já indexadas. A camada de domínio expõe
uma função pura de classificação e consultas agregadas evitam abrir PNGs ou
varrer todo o filesystem ao listar cards.

O fallback de filesystem existe somente para material legado ainda não
normalizado. Ele nunca promove bbox legada a máscara nem classifica material
sem revisão de máscara como concluído.

As grades podem continuar carregando algumas centenas de vídeos de uma vez,
com `content-visibility` e thumbnails lazy. Busca e filtros são locais após uma
resposta por objeto; atualizações de job/revisão atualizam somente os itens
afetados.

## 13. Erros, estados vazios e concorrência

- Toda tela diferencia carregando, vazio, indisponível e erro.
- Falha do worker SAM3 continua visível no board e na tela do vídeo.
- HTTP 409 de revisão mostra conflito e recarrega o estado, sem sobrescrever.
- Locks desabilitam mutações, mas permitem consulta.
- Jobs preservam tentativas, cancelamento e recuperação atuais.
- Ações destrutivas mostram impacto calculado pelo backend, nunca contagens
  estimadas pelo cliente.

## 14. Compatibilidade e implantação

O rollout é incremental:

1. introduzir classificação/contagens e testes sem trocar a interface;
2. adicionar shell e boards consumindo os endpoints novos;
3. encaixar os editores existentes nas rotas novas;
4. adicionar gestão avançada de objetos;
5. adicionar exportação global;
6. manter rotas e endpoints antigos até os E2E novos passarem.

Não há remigração do acervo. Novas migrações PostgreSQL são aditivas e
idempotentes. Reinícios preservam jobs, revisões e artefatos.

## 15. Testes e critérios de aceitação

### 15.1 Domínio e API

- tabela de casos cobrindo todas as transições e precedências de
  `pipeline_stage`;
- vídeo triado ou apenas propagado nunca aparece em Concluídos;
- último frame revisado move o vídeo para Concluídos;
- nova revisão/run inválido remove o vídeo de Concluídos;
- contagens por objeto e agregadas coincidem com as listas;
- renomear classe preserva IDs e manifestos antigos;
- arquivar/restaurar altera elegibilidade sem apagar dados;
- purge rejeita objeto ativo, confirmação errada, locks, jobs e caminhos fora
  do workspace;
- preview/export multiclasse gera IDs e splits determinísticos;
- nomes iguais de vídeo em objetos diferentes não colidem;
- quatro combinações YOLO/COCO × detecção/segmentação continuam válidas.

### 15.2 Frontend

- sidebar e URLs preservam objeto/etapa após reload;
- cada board mostra apenas vídeos de sua etapa;
- tags, progresso e ações correspondem à resposta do backend;
- ação primária é acessível sem hover e por teclado;
- objetos arquivados ficam fora dos seletores produtivos;
- dialog de purge exige texto exato;
- exportação atualiza preview ao alterar objetos, tags, vídeos ou tarefa;
- build TypeScript, smoke tests e auditoria básica de foco/contraste passam.

### 15.3 E2E

1. triar vídeo com objeto;
2. confirmar que aparece em SAM3, não em Concluídos;
3. propagar fake/real pequeno;
4. confirmar que aparece em Revisão;
5. revisar todos os frames;
6. confirmar que aparece em Concluídos;
7. selecionar esse e outro objeto concluído;
8. exportar dataset multiclasse nas quatro combinações;
9. reiniciar serviços e confirmar estados, histórico e datasets.

## 16. Premissas aprovadas

- Concluído significa todos os frames SAM3 aprovados ou editados.
- Remoção oferece arquivamento e exclusão permanente protegida.
- Exportação de vários objetos gera um único dataset multiclasse.
- A navegação escolhida é “Áreas operacionais” com sidebar persistente.
- Máscaras permanecem a fonte canônica; bboxes continuam derivadas.
