# Integração direta — WhatsApp Cloud API

O Centro de Atendimento integra diretamente com os Webhooks e a Graph API da Meta. Não existe middleware entre o sistema e o WhatsApp.

## Configuração

Variáveis necessárias:

```text
META_VERIFY_TOKEN=<valor privado definido por nós>
META_APP_SECRET=<App Secret do aplicativo Meta>
META_ACCESS_TOKEN=<token do system user com acesso ao WhatsApp>
META_PHONE_NUMBER_ID=<ID do número no WhatsApp Business>
META_GRAPH_VERSION=v23.0
```

Use token de usuário de sistema em produção. Não registre tokens, App Secret, payload integral ou conteúdo de mensagens nos logs.

## Webhook

Callback:

```text
GET|POST /api/v1/integrations/meta/whatsapp/webhook
```

O `GET` valida `hub.mode=subscribe` e `hub.verify_token`, devolvendo `hub.challenge` como texto puro.

O `POST` valida o corpo bruto com:

```text
X-Hub-Signature-256: sha256=<HMAC-SHA256(META_APP_SECRET, corpo bruto)>
```

Eventos suportados nesta etapa:

- mensagem recebida do tipo `text`, `image` (JPEG/PNG) ou `document` (PDF);
- status `sent`, `delivered`, `read` e `failed`;
- nome de perfil e `wa_id` do contato.

Cada mensagem recebida usa o `wamid` como chave idempotente. Reentregas do mesmo webhook não criam mensagens duplicadas. Um status atrasado não reduz uma mensagem de `LIDA` para `ENTREGUE`.

Outras mídias, reação, localização, contato, botão, lista, edição e exclusão são contabilizados como ignorados até seus adaptadores serem implementados. Imagens e PDFs são baixados da Meta e guardados em `MEDIA_STORAGE_DIR`, fora de `/static`, com download autenticado pelo painel. Imagem: até 5 MB; PDF: até 16 MB.

## Envio

A resposta do atendente entra na outbox. O worker chama:

```text
POST https://graph.facebook.com/{META_GRAPH_VERSION}/{META_PHONE_NUMBER_ID}/messages
Authorization: Bearer {META_ACCESS_TOKEN}
```

Texto usa `type=text`. Imagem/PDF são enviados primeiro para `/{phone-number-ID}/media`, depois como `image`/`document` com o ID retornado. O ID interno da outbox segue em `biz_opaque_callback_data` para correlação. A mensagem local fica `ACEITA_PELO_PROVEDOR` quando a API retorna um `wamid`; depois, webhooks avançam para `ENTREGUE`, `LIDA` ou `FALHA`.

## Pendências antes da produção

- colocar recebimento e outbox em worker/fila durável;
- armazenar erro sanitizado e oferecer reprocessamento operacional;
- status com `biz_opaque_callback_data` já reconcilia envio cujo retorno local se perdeu; ainda falta uma política de espera/consulta para casos sem callback antes de repetir o envio;
- suportar janela de atendimento, templates e erros específicos da Meta;
- definir retenção/expiração de mídia local e homologar upload/download com conta Meta real;
- adicionar métricas, alertas e rotação de credenciais;
- homologar com número e aplicativo Meta próprios.
