# Centro de Atendimento

Primeira fatia vertical do atendimento compartilhado: fila central, entrada simulada, atribuição atômica, mensagens, status, transferência, perfil de contato, tags, notas internas, encerramento categorizado, métricas, auditoria, outbox e eventos em tempo real.

## Requisitos

- Python 3.12+

## Instalação

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

## Execução

```powershell
.venv\Scripts\uvicorn app.main:app --reload
```

- API: `http://127.0.0.1:8000`
- Painel: `http://127.0.0.1:8000/`
- Swagger: `http://127.0.0.1:8000/docs`
- WebSocket: `ws://127.0.0.1:8000/api/v1/ws`

## Testes

```powershell
.venv\Scripts\pytest
```

## Fluxo mínimo

1. `POST /api/v1/inbox/messages` cria/reutiliza atendimento e deduplica evento externo.
2. `POST /api/v1/attendances/{id}/claim` atribui atomicamente.
3. `POST /api/v1/attendances/{id}/messages` registra saída e outbox.
4. `PATCH /api/v1/attendances/{id}/status` avança fluxo.
5. Nova entrada retoma atendimento em `AGUARDANDO_CLIENTE`.
6. Responsável ou supervisão edita nome, telefone, tags e estágio do contato.

## Painel de contatos

Aba "Contatos" traz um quadro por estágio de classificação (`NAO_CLASSIFICADO`, `NOVO_CONTATO`, `CLIENTE_POTENCIAL`, `CLIENTE_ATIVO`, `INATIVO`), inspirado no board de contatos da Blip. `GET /api/v1/contacts` lista com filtro por estágio, tag e busca; `PATCH /api/v1/contacts/{id}/stage` move o contato de estágio. Clicar num contato abre o atendimento mais recente dele no mesmo painel de conversa; sem atendimento, abre o simulador de entrada pré-preenchido.

## Backlog P1 do piloto

- **Busca por protocolo:** campo de busca do quadro de Atendimentos casa nome/telefone/`contact_id` e também o protocolo (8 primeiros caracteres do id do atendimento) ou o id completo.
- **Tags do atendimento:** `PUT /api/v1/attendances/{id}/tags` — separado das tags de contato, filtrável no quadro de Atendimentos. Só responsável ou supervisão edita.
- **Respostas rápidas:** `GET/POST /api/v1/quick-replies`, `DELETE /api/v1/quick-replies/{id}`. Qualquer ator lista e usa; só `SUPERVISOR`/`ADMIN` cria ou remove. Inserção no composer via chip.
- **Regra de inatividade:** `AttendanceRead.stale` (calculado, sem persistir) fica `true` quando o atendimento passa do limiar por status (`AGUARDANDO` 15min, `EM_ATENDIMENTO` 4h, `AGUARDANDO_CLIENTE`/`AGUARDANDO_INTERNO` 24h — ver `STALE_THRESHOLDS` em `app/domain.py`). Sem reabertura automática — decisão fechada em [`docs/planejamento-atendimento-compartilhado.md`](docs/planejamento-atendimento-compartilhado.md#13-decisões-fechadas-2026-09-21).
- **Notificações:** toast + `Notification` API do navegador ao chegar `attendance.created` sem responsável, ou `attendance.updated` num atendimento seu. Só no navegador aberto — sem push/e-mail.
- **Reprocessamento assistido:** `GET /api/v1/integrations/outbox` lista itens não entregues (com tentativas/status); `POST /api/v1/integrations/outbox/{id}/retry` zera tentativas de um item específico. `POST /api/v1/integrations/outbox/process` (já existia) processa o lote. Tudo `SUPERVISOR`/`ADMIN`, exposto no botão "Falhas de envio" do painel.

## Autenticação

Rotas operacionais exigem `Authorization: Bearer <token>` com JWT (HS256) emitido por `POST /api/v1/auth/login`:

```json
// POST /api/v1/auth/login
{"id": "agente-1", "password": "..."}
```

Token expira em 12h (`ACCESS_TOKEN_TTL` em `app/security.py`). Papéis: `ATENDENTE`, `SUPERVISOR`, `ADMIN`.

`ADMIN` cria novas contas via `POST /api/v1/agents` (`{id, role, password}`, senha mínima 8 caracteres) e lista via `GET /api/v1/agents`. Não há autoatendimento de cadastro nem redefinição de senha — ambos ficam para quando entrar RBAC definitivo.

Quando `ENABLE_SIMULATOR=true` (padrão em dev/teste), a aplicação semeia automaticamente na primeira subida, todos com senha `dev-local-only`:

| id | papel |
|---|---|
| `agente-1`, `agente-2` | `ATENDENTE` |
| `supervisor-1` | `SUPERVISOR` |
| `admin-1` | `ADMIN` |

Essa semente **nunca roda com `ENABLE_SIMULATOR=false`** — em produção, crie a primeira conta `ADMIN` diretamente no banco (`app.security.hash_password`) antes do primeiro deploy.

Configure `JWT_SECRET` no ambiente antes de produção — sem ele a API gera um segredo efêmero a cada subida e todo mundo é deslogado no próximo restart (aceitável em dev, não em produção).

## WhatsApp Cloud API (Meta)

Copie `.env.example` para `.env` ou configure as variáveis no ambiente. A aplicação recebe diretamente o webhook da Meta, valida `X-Hub-Signature-256` com o App Secret e envia texto pela Graph API. Não há middleware intermediário.

Contrato e configuração: [`docs/integracao-meta-whatsapp.md`](docs/integracao-meta-whatsapp.md).

Callback para cadastrar no Meta for Developers:

```text
https://SEU-DOMINIO/api/v1/integrations/meta/whatsapp/webhook
```

O endpoint `GET` responde ao desafio de verificação. O `POST` aceita mensagens de texto e atualizações `sent`, `delivered`, `read` e `failed`. Para desenvolvimento, o simulador local pode continuar ativo.

## Limites atuais

- Autenticação é JWT local emitido por esta API (login com id+senha), sem MFA/SSO — ver seção "Autenticação". Integração com IdP corporativo (OIDC) fica para quando a EVIG definir esse provedor para esta aplicação.
- WebSocket (`/api/v1/ws`) não valida o token — qualquer conexão recebe todos os eventos, sem filtro por papel/fila. Autenticar o WebSocket é trabalho futuro.
- WebSocket está em memória; usar Redis/pub-sub antes de múltiplas instâncias.
- SQLite serve ao desenvolvimento; usar PostgreSQL em produção.
- Worker atual roda no processo da API; usar fila/worker dedicado antes de escalar horizontalmente.
- Mídia (imagem/PDF — já decidido como requisito, ver seção 13 do planejamento), templates e tratamento assíncrono do webhook ainda não foram implementados.
- A Graph API não oferece idempotência equivalente para envio de texto; uma falha após aceite remoto e antes da confirmação local pode exigir reconciliação para evitar duplicação.
- Escopo por fila/equipe entra junto com ilhas e RBAC definitivo.
