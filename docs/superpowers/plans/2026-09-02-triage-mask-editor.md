# Triage and Mask Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Corrigir a exportação com contexto obsoleto, tornar o avanço da triagem imediato e melhorar o editor de máscaras.

**Architecture:** O worker relê o estado durável por job; o frontend separa iniciar exportação de aguardar sua conclusão. O editor mantém o mesmo contrato PNG e reorganiza somente interação e viewport.

**Tech Stack:** Python 3.12/FastAPI, React 19/TypeScript, Zustand, Canvas 2D, PostgreSQL jobs.

**Spec:** `docs/superpowers/specs/2026-09-02-triage-mask-editor-design.md`

## Global Constraints

- Preservar `no_boom` no armazenamento.
- Preservar PNG binário e `expected_revision`.
- Não aguardar FFmpeg antes de navegar.
- Não apagar dados existentes.

---

### Task 1: Contexto fresco no worker CPU

**Files:**
- Modify: `services/app/worker.py`
- Test: `services/app/server/tests/test_worker_context.py`

**Interfaces:**
- Consumes: `workspace.invalidate(object_id)` e `workspace.context(object_id)`.
- Produces: `_context(job)` com `AnnotationStore` recarregado do disco.

- [ ] Criar teste que escreve uma nova anotação após o primeiro contexto e exige que o segundo job a leia.
- [ ] Executar o teste e confirmar a falha `anotacao ou intervalos ausentes`/entrada ausente.
- [ ] Invalidar o contexto do objeto antes de `ensure_loaded()`.
- [ ] Executar testes do worker e rotas de anotação.

### Task 2: Enfileirar e navegar sem aguardar FFmpeg

**Files:**
- Modify: `services/app/web/src/store/annotator.ts`
- Modify: `services/app/web/src/views/AnnotatorView.tsx`
- Modify: `services/app/web/src/lib/keys.ts`
- Test: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Produces: `queueExport(): Promise<JobInfo | null>` e conclusão acompanhada em segundo plano.

- [ ] Criar smoke que exige navegação após a resposta de criação do job, mantendo o job incompleto.
- [ ] Confirmar que o smoke falha porque a tela aguarda o job.
- [ ] Separar criação do job de acompanhamento/finalização e atualizar a biblioteca ao concluir.
- [ ] Confirmar navegação imediata e fluxo final para SAM3.

### Task 3: Sem objeto

**Files:**
- Modify: `services/app/web/src/views/AnnotatorView.tsx`
- Modify: `services/app/web/src/lib/keys.ts`
- Test: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Consumes: `markNoObject` e `nextPending`.
- Produces: texto genérico e navegação após persistência.

- [ ] Criar smoke para confirmar chamada da API e abertura do próximo vídeo.
- [ ] Renomear textos e manter `no_boom` interno.
- [ ] Executar o smoke completo.

### Task 4: Editor de máscaras inspirado no CVAT

**Files:**
- Modify: `services/app/web/src/components/MaskEditor.tsx`
- Modify: `services/app/web/src/views/ReviewView.tsx`
- Test: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Consumes: `MaskReviewFrame`, `saveMaskReviewFrame` e viewport da revisão.
- Produces: ferramentas mover/pincel/borracha, zoom/pan, formato, cursor, visibilidade e atalhos.

- [ ] Estender o smoke com controles e nomes acessíveis esperados.
- [ ] Confirmar falha pelos controles ausentes.
- [ ] Implementar viewport e ferramentas preservando o canvas binário.
- [ ] Executar build TypeScript e todos os smoke tests.

### Task 5: Verificação integrada

**Files:**
- Verify: `services/app/server/tests/`
- Verify: `services/app/web/`

- [ ] Executar testes Python do app no container.
- [ ] Executar `npm run build`.
- [ ] Conferir sintaxe e arquivos modificados.
- [ ] Documentar os arquivos que precisam ser copiados e os comandos de rebuild do servidor.
