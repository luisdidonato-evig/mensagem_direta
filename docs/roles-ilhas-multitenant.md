# Especificação — roles, ilhas de atendimento e multiempresa

**Estado:** regras de alcance confirmadas por Luis; contrato técnico ainda depende de alinhamento com o sistema central de roles. **Contexto:** conversa entre Luis e Miguel em 2026-09-23. Este documento descreve o contrato desejado; não declara a migração implementada.

## Decisão de domínio

- **Empresa (tenant):** fronteira de dados, usuários, canais e configurações. Ações operacionais sobre uma empresa usam seu `company_id`; operações globais de `master` podem listar e gerenciar empresas.
- **Grupo de atendimento (ilha):** unidade operacional da empresa que organiza fila, atendimentos e vendedores. Não é workspace nem tenant. Uma empresa pode ter várias ilhas.
- **Vendedor:** papel do atendente neste produto. Deve estar vinculado a pelo menos uma ilha para atender. O vínculo com ilha define quais filas e atendimentos pode acessar.
- **Admin:** gerencia tudo dentro da própria empresa: ilhas, equipe, atendimentos e configurações. Não acessa outras empresas.
- **Master:** papel global; gerencia empresas e vê todos os dados de todas elas. Também pode administrar os recursos de cada empresa.

**Proposta para o Mensagem Direta:** `admin` e `vendedor` são papéis por empresa; `master` tem alcance global. O sistema central de roles pode ser a fonte desses papéis, mas o backend deste produto precisa reconhecer e aplicar os três. `operador` não representa atendente aqui; o atendente usa `vendedor`. A role da demo já foi definida fora desta conversa e não deve ser recriada ou alterada por esta especificação.

## Matriz de acesso proposta

| Ação | Master | Admin da empresa | Vendedor |
|---|---|---|---|
| Criar, editar e desativar empresa | Sim | Não | Não |
| Ver dados e atendimentos | Todas as empresas | Todas as ilhas da própria empresa | Apenas ilhas vinculadas |
| Criar, editar e desativar ilha | Qualquer empresa | Própria empresa | Não |
| Gerenciar usuários e vínculos de ilha | Qualquer empresa | Própria empresa | Não |
| Assumir, responder e encerrar atendimento | Qualquer empresa | Própria empresa | Apenas ilha vinculada |
| Transferir entre ilhas | Qualquer empresa | Própria empresa | Conforme regra operacional a definir |
| Configurar canal WhatsApp e demais recursos | Qualquer empresa | Própria empresa | Não |

Regras obrigatórias: autorização no backend para cada leitura, escrita, WebSocket e job; aplicar escopo global para `master`, escopo da empresa para `admin` e escopo de empresa mais vínculo de ilha para `vendedor`; exigir empresa alvo inequívoca nas alterações de recursos de uma empresa; impedir vínculo entre usuário e ilha de empresas diferentes; auditar criação de empresa/ilha, mudanças de vínculo, transferência e ações no WhatsApp. A interface pode ocultar ações, mas não substitui a autorização do servidor.

## Contrato necessário do sistema central de roles

1. **Identidade:** `user_id` estável, papel, estado ativo/inativo e emissor confiável. `admin` e `vendedor` precisam de `company_id`; `master` precisa de identidade global e informa empresa alvo nas operações de uma empresa.
2. **Papéis e permissões:** reconhecer `master` global, `admin` da empresa e `vendedor` vinculado a ilhas. Se o provedor central usar permissões granulares, elas devem preservar essa matriz; confirmar nomes/códigos com ele antes de implementar.
3. **Validação:** formato do token ou introspecção, assinatura/chaves, audiência, expiração, revogação e sincronização de alterações de papel/estado. Definir quem provisiona a identidade local necessária para atribuição e auditoria.
4. **Escopo de ilha:** vínculo entre `user_id`, `company_id` e `group_id`, inclusive política para múltiplas ilhas, desativação e vendedor com atendimento ativo. Decidir qual serviço é fonte de verdade desse vínculo; o Mensagem Direta já persiste `group_agents`.
5. **Erros e eventos:** distinguir autenticação inválida de acesso negado e informar alterações de conta/permissão com latência aceitável para revogar sessões.


## Migração do estado local

Hoje `app/domain.py` define `ATENDENTE`, `SUPERVISOR` e `ADMIN`; `app/auth.py` valida JWT local, papel e `company_id` contra `agents`. Grupos estão em `service_groups`; vínculos estão em `group_agents`. Rotas, consultas e WebSocket já usam escopos de empresa e ilha, mas contêm verificações diretas desses papéis. A integração Meta tem configuração por empresa/número. Isso é implementação temporária, não o contrato central definitivo.

Migração sugerida:

1. Fechar o contrato de identidade e permissões com PHP/Go e decidir a fonte dos vínculos de ilha.
2. Introduzir adaptador de autenticação central e contexto de requisição (`user_id`, papel, `company_id` quando aplicável), preservando temporariamente o login local apenas para desenvolvimento/demo se necessário.
3. Mapear `ATENDENTE` para `vendedor`; revisar cada uso de `SUPERVISOR` antes de removê-lo. Suas capacidades atuais incluem ver todas as ilhas e gerir vínculos, portanto não devem ser concedidas automaticamente ao vendedor.
4. Substituir verificações diretas de enum por permissões e escopo de empresa/ilha em API, WebSocket, ações do WhatsApp e processamento assíncrono.
5. Migrar usuários/sessões existentes, testar revogação e isolamento entre empresas; desligar a autorização temporária após equivalência funcional.

## Critérios de aceite

- Admin cria ilha na própria empresa, vincula vendedor e esse vendedor vê e atende apenas ilhas vinculadas.
- Vendedor sem ilha não recebe fila nem assume atendimento; solicitação direta por ID também é negada.
- Admin e vendedor da empresa A não leem, alteram, transferem nem recebem eventos da empresa B, mesmo com IDs conhecidos.
- Master lista e gerencia empresas, lê atendimentos de todas elas e administra recursos de qualquer empresa; alterações identificam a empresa alvo e ficam auditadas.
- Alterar/desativar papel ou vínculo revoga acesso conforme a política de sessão acordada.
- Ações do WhatsApp e eventos em tempo real obedecem ao mesmo contexto de empresa e escopo operacional.

## Pendências para alinhamento

- Definir contrato técnico para representar e validar `master` global no sistema central de roles e neste backend.
- Decidir destino das capacidades atuais de `SUPERVISOR`: `admin`, permissões avulsas ou papel adicional no futuro.
- Confirmar se vendedor pode pertencer a várias ilhas e em quais condições pode transferir atendimentos.
- Definir emissor/validador de token, provisionamento de usuário e fonte de verdade dos vínculos.
- Fechar política de autorização das ações WhatsApp e dependência da IA para o início de estratégias em especificação separada.
