# Atendimento e middleware: auditoria e contrato

## Arquitetura encontrada

O Atendimento é uma API FastAPI com SQLAlchemy, migrations Alembic, outbox e painel estático. `app/api.py` reúne rotas e regras operacionais; `app/integration.py` contém `MessageSender`, Meta Cloud API, gateway e worker da outbox. `app/models.py` persiste grupos, vínculos, agentes, contatos, atendimentos, mensagens, anexos, eventos, avaliações e respostas rápidas. `app/domain.py` define estados. O painel em `static/` já oferece quadro de conversas, fila, chat, grupos, membros, transferências, respostas rápidas, anexos, encerramento e avaliação. Não há repositórios separados.

Antes desta alteração, a entrada principal era simulador ou webhook Meta. O gateway era usado apenas para texto em `/internal/v1/proactive-delivery`; interativos e mídia iam diretamente à Meta. Status e avaliação da Meta eram tratados no webhook.

O armazenamento já usa `company_id` como chave local do tenant. A entrada interna expõe `tenant_id` e o mapeia para esse ID existente; não cria outra identidade nem exige migração destrutiva. Provisione com `python -m app.provision_tenant --company "Nome" --admin-id admin --tenant-id UUID-DO-GATEWAY` após migrar banco para manter IDs iguais.

## Arquitetura após alterações

`middleware → /api/v1/internal/* → process_inbound_message / domínio existente → outbox → MessageSender → middleware`. O webhook Meta legado continua como adapter de entrada. Estado de entrega passa por `process_delivery_status` compartilhado. Handoff interno usa a mesma transição de automação dos endpoints operacionais.

## Contrato de entrada

Todas as rotas internas exigem `X-Internal-Key` igual a `CHANNEL_GATEWAY_INTERNAL_KEY`; sem chave configurada retornam 401. O tenant deve existir previamente no Atendimento. Essas rotas são contrato **oferecido pelo Atendimento**; o gateway ainda precisa ser configurado para chamá-las.

- `POST /api/v1/internal/messages/inbound`: `event_id`, `tenant_id`, `channel: "whatsapp"`, `channel_account_id`, `conversation_id`, `message_id`, `sender: {id, name?}`, `message: {type: "text", text}` ou `{type: "choice", choice_id, text?}`, `occurred_at` ISO 8601. Retorna `InboundResult` com `duplicate`. Reusa grupo ativo do tenant e atendimento aberto da conversa; deduplica evento e mensagem. `choice_id` no formato `rating:<attendance_id>:<1–5>` registra avaliação de atendimento encerrado sem reabrir fila. IDs de tenant, conta e conversa devem ser UUIDs aceitos pelo gateway.
- `POST /api/v1/internal/messages/status`: `event_id`, `tenant_id`, `status` (`QUEUED`, `SENT`, `DELIVERED`, `READ`, `FAILED`), `occurred_at`, `channel_account_id?` e `message_id` e/ou `idempotency_key` da outbox. Retorna `{ "processed": bool }`. `false` significa mensagem desconhecida; reenvio do mesmo evento reconhecido é idempotente.
- `POST /api/v1/internal/handoff`: `event_id`, `tenant_id`, `conversation_id`, `channel_account_id`, `reason`, `summary?`, `sender_id?`. Reusa atendimento aberto ou cria no primeiro grupo ativo do tenant; põe automação em `HUMAN_REQUESTED`. Repetição do evento devolve atendimento existente.

## Contrato de saída

`ChannelGatewaySender` chama o endpoint **existente** `POST /internal/v1/messages` do `golang-gateway` com `X-Internal-Key`. Envia `tenant_id`, `idempotency_key` (ID da outbox), `source: "atendimento"`, `message` e **um** destino: `conversation_id` ou `channel_account_id` com `recipient_id`. Resposta 202: `{ "status": "queued", "delivery_id": "..." }`. O gateway valida UUIDs para tenant, conversa e conta, resolve endereço/tenant, renderiza e enfileira.

Mapeamento: texto → `{kind:"text",text}`; botões → `choices` com `body` e `options[{id,label}]`; lista de avaliação → `list`; imagem/PDF com `asset_id` de gateway → `image`/`document` com `asset`. Mídia local sem `asset_id` usa adapter Meta legado quando configurado; sem fallback, falha explícita na outbox. O gateway não oferece neste contrato upload de arquivo local do Atendimento.

## Integração com middleware

Fonte conferida: `golang-gateway/src/httpapi/messages.go`, `src/channel/outbound.go`, `docs/internal-messages.md`. O gateway também mantém `/internal/v1/proactive-delivery`, mas `/internal/v1/messages` suporta tipos necessários e destino por conta. `CHANNEL_GATEWAY_URL` ativa sender do gateway. `CHANNEL_GATEWAY_INTERNAL_KEY` autentica saída e entrada interna; deve coincidir com chave interna aceita pelo gateway.

## Funcionalidades existentes reaproveitadas

CRUD de grupos/membros/agentes, capacidade e fila, transferência, quick replies, upload/download protegido, avaliação, histórico, status, painel, controle otimista de versões, outbox e handoff operacional. Nenhuma dessas regras foi movida ao gateway.

## Alterações realizadas

Entrada interna autenticada e mapeada para `process_inbound_message`; chave de deduplicação inclui tenant e conta; busca de atendimento considera conta e conversa; avaliação por escolha usa domínio compartilhado com adapter Meta; status neutro compartilhado com adapter Meta, progressão monotônica e falha terminal até reprocessamento explícito; handoff interno idempotente, com `HUMAN_ACTIVE` ao assumir/puxar; sender usa contrato real `/internal/v1/messages` e traduz texto/interativos; payloads da outbox carregam IDs de canal. Painel exibe estado de automação e contexto do handoff. Testes cobrem autenticação, tenant, idempotência, status, avaliação, handoff e contrato de envio.

## Gaps dependentes de mudança futura no middleware

O gateway ainda não chama estes endpoints de inbound, status ou handoff. Não há contrato do gateway para upload do arquivo local e obtenção de `asset_id`; anexos locais dependem do fallback Meta. `channel_account_id` e `conversation_id` devem ser UUIDs conhecidos pelo gateway. Status assíncrono de entrega precisa de produtor no gateway que envie eventos a esta API. A entrada interna aceita texto e escolha, mas mídia inbound exige payload de evento normalizado acordado antes de ampliar o contrato.

## Gaps próprios do Atendimento

A escolha de grupo inicial ainda é o primeiro grupo ativo do tenant; não existem regras configuráveis de roteamento por conta, overflow nem distribuição automática além de fila/pull/claim. Eventos distintos da mesma conversa em concorrência podem disputar criação do atendimento; requer restrição parcial única e migração de dados existentes antes de múltiplas instâncias. O painel mostra o handoff, mas ainda não oferece controles próprios para pausar ou retomar IA.

## Pontos Meta/WhatsApp ainda existentes

Webhook Meta legado, validação de assinatura, download de mídia, envio de mídia local por fallback e configurações `META_*`. Permanecem em adapters para preservar instalações atuais. O domínio operacional usa estados e IDs neutros nas novas rotas.

## Próximos passos

Configurar gateway para entregar eventos de inbound/status/handoff ao Atendimento; definir contrato de mídia inbound e upload/asset para substituir fallback; homologar fluxo completo com dois tenants e contas reais; migrar instalações Meta legadas quando gateway cobrir mídia e callback de avaliação.
