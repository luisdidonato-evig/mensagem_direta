# Centro de Atendimento

Integração com gateway, entradas internas e limites atuais: [docs/integracao-middleware.md](docs/integracao-middleware.md).

Primeira fatia vertical do atendimento compartilhado: fila central, entrada simulada, atribuição atômica, mensagens, status, transferência, perfil de contato, tags, notas internas, encerramento categorizado, métricas, auditoria, outbox e eventos em tempo real.

## Requisitos

- Python 3.12+

Para atualizar banco existente, rode `python -m alembic upgrade head` antes de iniciar a API.

Cada grupo configura capacidade total em unidades de carga e custo de carga por atendimento ativo. Exemplo: capacidade 5 e custo 1 permitem 5 conversas ativas com responsável; a sexta fica em espera e recebe a mensagem configurada no grupo. Conversas na fila sem responsável não consomem carga. O limite individual de um agente pode ser reduzido ou ampliado no vínculo com o grupo; sem limite específico, vale a capacidade total do grupo. Admin e supervisor veem conversas de todos os grupos da própria empresa; atendentes veem apenas grupos aos quais estão vinculados.

## Instalação

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

## Execução

```powershell
.\.venv\Scripts\uvicorn app.main:app --reload
```

Se criar `.env`, inicie com `--env-file .env`; o app lê variáveis do processo e não carrega esse arquivo sozinho:

```powershell
.\.venv\Scripts\uvicorn app.main:app --reload --env-file .env
```

- API: `http://127.0.0.1:8000`
- Painel: `http://127.0.0.1:8000/`
- Swagger: `http://127.0.0.1:8000/docs`
- WebSocket: `ws://127.0.0.1:8000/api/v1/ws`

## Testes

```powershell
.\.venv\Scripts\pytest
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
- **Respostas rápidas:** `GET/POST /api/v1/quick-replies`, `PATCH/DELETE /api/v1/quick-replies/{id}`. Qualquer ator lista e usa; só `SUPERVISOR`/`ADMIN` cria, edita ou remove. Podem valer para toda a empresa ou um grupo. Inserção no composer via chip.
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

`ADMIN` gerencia contas e papéis no painel **Equipe e grupos**. `SUPERVISOR` vê os usuários da empresa e pode criar, editar, desativar e vincular **atendentes**; não altera contas de supervisores ou administradores. A edição de conta permite definir uma nova senha (mínimo 8 caracteres). Usuários com atendimento ativo precisam ter os atendimentos transferidos antes da desativação ou desvinculação. A última conta de administrador ativo não pode ser desativada ou rebaixada.

No painel, o administrador também cria e edita grupos e a capacidade por atendente. Administrador e supervisor vinculam usuários aos grupos e podem definir uma capacidade individual. Atendentes veem apenas os grupos aos quais pertencem, filtram os atendimentos do grupo e puxam o próximo da fila. Ao simular uma nova entrada, selecione o grupo de destino. O responsável pode redirecionar atendimento aberto à fila de outro grupo; admin e supervisor também podem atribuir diretamente um usuário vinculado. Grupo com atendimento aberto não pode ser desativado.

Quando `ENABLE_SIMULATOR=true` (padrão em dev/teste), a aplicação semeia automaticamente na primeira subida, todos com senha `dev-local-only`:

| id | papel |
|---|---|
| `agente-1`, `agente-2` | `ATENDENTE` |
| `supervisor-1` | `SUPERVISOR` |
| `admin-1` | `ADMIN` |

Essa semente **nunca roda com `ENABLE_SIMULATOR=false`**. Para cadastrar empresa, grupo inicial e administrador em produção, rode `python -m alembic upgrade head` e `python -m app.provision_tenant --company "Nome" --admin-id admin-empresa --tenant-id UUID-DO-GATEWAY` (senha solicitada no terminal). `--tenant-id` é obrigatório para integrar tenant já existente no gateway; sem ele, o comando gera UUID local. IDs de usuários são únicos em todo o banco.

Configure `JWT_SECRET` no ambiente antes de produção — sem ele a API gera um segredo efêmero a cada subida e todo mundo é deslogado no próximo restart (aceitável em dev, não em produção).

## WhatsApp Cloud API (Meta)

Copie `.env.example` para `.env` ou configure as variáveis no ambiente. O adapter legado recebe diretamente o webhook da Meta e valida `X-Hub-Signature-256` com o App Secret. Com `CHANNEL_GATEWAY_URL`, o envio de texto e interativos usa o gateway; mídia local ainda usa fallback Meta quando configurado.

Contrato e configuração: [`docs/integracao-meta-whatsapp.md`](docs/integracao-meta-whatsapp.md).

Callback para cadastrar no Meta for Developers:

```text
https://SEU-DOMINIO/api/v1/integrations/meta/whatsapp/webhook
```

O endpoint `GET` responde ao desafio de verificação. O `POST` aceita texto, respostas a botões/listas, JPEG/PNG, PDF e status `sent`, `delivered`, `read` e `failed`. O atendente pode enviar texto com até três botões. Ao encerrar, o cliente recebe uma lista de avaliação 1–5; a resposta fica no atendimento sem reabrir a fila. Para desenvolvimento, o simulador local pode continuar ativo.

Para múltiplas empresas, configure `META_TENANTS_JSON` conforme [`docs/integracao-meta-whatsapp.md`](docs/integracao-meta-whatsapp.md#multiempresa). Cada empresa usa seu callback `/webhook/{phone_number_id}`, segredo de assinatura, token e número próprios. Contatos com o mesmo telefone mantêm perfis e tags separados por empresa.

## Limites atuais

- Autenticação é JWT local emitido por esta API (login com id+senha), sem MFA/SSO — ver seção "Autenticação". Integração com IdP corporativo (OIDC) fica para quando a EVIG definir esse provedor para esta aplicação.
- WebSocket (`/api/v1/ws`) exige uma primeira mensagem JSON `{ "token": "..." }` e filtra eventos por empresa/grupo. A conexão usa o token da sessão do painel.
- WebSocket está em memória; usar Redis/pub-sub antes de múltiplas instâncias.
- SQLite serve ao desenvolvimento; usar PostgreSQL em produção.
- Worker atual roda no processo da API; usar fila/worker dedicado antes de escalar horizontalmente.
- Mídia JPEG/PNG (até 5 MB) e PDF (até 16 MB) usa `MEDIA_STORAGE_DIR` privado (padrão: `media` ao lado do banco SQLite). O painel envia anexo e baixa arquivo com autenticação. Homologação com a conta Meta, templates e tratamento assíncrono do webhook ainda faltam.
- Status da Meta com `biz_opaque_callback_data` reconcilia envio cujo retorno local se perdeu. Sem esse callback, a Graph API não oferece idempotência equivalente; uma retentativa ainda pode duplicar a resposta.
- O simulador cria entradas no grupo selecionado. No modo de número único, configure `META_DEFAULT_GROUP_ID`; no modo multiempresa, cada número aponta para grupo explícito em `META_TENANTS_JSON`. Regras de triagem além desse grupo inicial ainda exigem configuração própria.
- A outbox é compartilhada no mesmo banco e processo, mas cada item usa as credenciais da empresa. Escala horizontal exige fila/worker dedicado.
