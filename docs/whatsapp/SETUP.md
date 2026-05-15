# WhatsApp Cloud API — configuração do operador

> **Público:** operador humano titular da conta Business da Meta que vai hospedar este bot. Tudo neste documento depende de você — a DEILE não consegue criar contas Meta automaticamente, solicitar aprovações ou manter a infraestrutura HTTPS.

Este é o passo a passo para levar o adaptador WhatsApp de “código mesclado” até “mensagens fluindo em produção”. Não pule nada — a Meta rejeita implantações mal configuradas sem aviso (ou pior, com erros obscuros tipo 132xxx).

---

## 0. O que você está assinando

| Item | Realidade |
|------|-----------|
| **Custo por conversa** | US$ 0,05 – 0,20 de acordo com categoria e país (veja preço da Meta). As primeiras 1000 conversas de serviço por mês são grátis. |
| **Janela de conversa de 24h** | Após a última mensagem recebida do usuário, você tem 24h para enviar texto livre. Depois disso, **apenas templates aprovados passam**. |
| **Templates** | Cada template que quiser enviar deve ser submetido para a Meta e aprovado (24–48h de análise). Templates de marketing são mais rigorosos. |
| **Editar/reação/digitando** | Cloud API não suporta editar (tem que deletar e reenviar) nem reações do remetente fora de conversas. Não há suporte para “digitando”. |
| **HTTPS** | Endpoint do webhook deve ser HTTPS público com certificado válido. Não pode ser self-signed. Não pode ser localhost — a Meta acessa de fora. |
| **Verificação empresarial** | Obrigatória antes de sair do sandbox. Reserve 1–2 semanas. |

---

## 1. Meta Business Manager — provisionando do zero

1. Crie ou acesse uma **conta Business da Meta** em https://business.facebook.com/.
2. Adicione uma **Conta WhatsApp Business (WABA)** em *Configurações Empresariais → Contas → Contas do WhatsApp*.
3. Dentro da WABA, adicione um **número de telefone** (o número de sandbox serve para desenvolvimento; produção precisa número real seu e que não esteja em uso recente no app pessoal).
4. Vá em *Configurações Empresariais → Usuários → Usuários do Sistema* e crie um usuário de sistema com perfil **Admin** na WABA.
5. **Gere um token de acesso de Usuário de Sistema** com escopos `whatsapp_business_messaging` e `whatsapp_business_management`.
   - **Configure o token para nunca expirar.** É o único tipo possível. Os tokens de usuário de 24h padrão do console vão **te bloquear** durante conversas.
6. Copie o token para um gerenciador de senhas. A Meta só mostra uma vez.

Anote:

```
DEILE_BOT_WHATSAPP_ACCESS_TOKEN=<token de usuário do sistema, nunca expira>
DEILE_BOT_WHATSAPP_PHONE_NUMBER_ID=<do WhatsApp Manager → API Setup>
DEILE_BOT_WHATSAPP_BUSINESS_ACCOUNT_ID=<id da WABA, também do API Setup>
```

---

## 2. Configuração do Webhook

O bot precisa expor uma URL HTTPS pública que a Meta consiga alcançar. A Cloud API exige:

- Um handshake `GET` no registro inicial (verificação do token).
- Um `POST` para cada evento recebido (texto, imagem, resposta de botão, resposta de lista, callback de status).

No *WhatsApp Manager → Configuração → Webhook*:

1. **Callback URL** = `https://seuhost-publico/webhook/whatsapp`
2. **Verify token** = uma string aleatória longa escolhida por você. Configure o mesmo valor no bot:

```
DEILE_BOT_WHATSAPP_VERIFY_TOKEN=<string aleatória longa>
```

3. Assine os campos do webhook: `messages` (obrigatório), `message_status_updates` (recomendado).

4. **Assine o payload.** Em *Painel do App → Configurações → Básico*, copie o **App Secret** e configure:

```
DEILE_BOT_WHATSAPP_APP_SECRET=<app secret>
```

   O bot vai validar `X-Hub-Signature-256` em cada POST recebido. Se o
   header não vier ou estiver errado, responde 401 — a Meta acha que está mal configurado e desativa o webhook após alguns erros.

Se não configurar o App Secret, a checagem de assinatura é pulada (apenas para desenvolvimento).
**Nunca vá pra produção sem isso.**

---

## 3. Fluxo de aprovação de templates

Templates moram no `config/whatsapp_templates.yaml` do bot **e**
precisam de template aprovado idêntico (nome exato) na Meta. O bot envia
o *nome* + *idioma*; a Meta resolve o conteúdo. Diferença = erro 132001.

Para cada template desejado:

1. Em *WhatsApp Manager → Modelos de Mensagem*, clique em **Criar Modelo**.
2. Escolha a **categoria**:
   - `utility` — atualizações de pedidos, mudanças de conta, lembretes. Barato.
   - `marketing` — promoções, ofertas. Caro, revisão rigorosa.
   - `authentication` — OTP, códigos de verificação. Barato, formato restrito.
   - `service` — respostas dentro das 24h. Grátis.
3. Escolha o **idioma**. Pode adicionar mais idiomas depois (cada um é entrada separada e precisa aprovação separada).
4. Preencha o **corpo**. Use `{{1}}`, `{{2}}` para variáveis. Forneça um valor de exemplo para cada uma — a Meta exige para a aprovação.
5. Opcionalmente, adicione **cabeçalho** (texto ou mídia) e **botões** (resposta ou URL).
6. Envie. Aprovação geralmente leva de 30 minutos a 24 horas. Marketing pode demorar mais.
7. Após aprovação, **espelhe o template em `config/whatsapp_templates.yaml`**:

   ```yaml
   templates:
     - name: appointment_reminder      # nome exato na Meta
       language: pt_BR                  # código de idioma/língua exato
       category: utility
       body_params:
         - name: patient_name           # corresponde ao {{1}}
         - name: appointment_time       # corresponde ao {{2}}
       header_params: []
   ```

   O catálogo valida se a quantidade de parâmetros no corpo/cabeçalho bate com o que você envia, **antes** do request ir pra Meta — poupa debug de 400 com “param count mismatch”.

**Erros comuns:**

- Colocar o YAML sem aprovar na Meta → Meta responde 132001.
- Aprovar na Meta mas esquecer o YAML → o adaptador envia sem componentes e a Meta recusa se o template tem variáveis.
- Editar corpo do template já aprovado → Meta coloca novamente como “pendente” até rever.
- Tentar enviar `marketing` de número não verificado como empresa → Meta descarta a mensagem sem aviso.

---

## 4. Lado DEILE — plugando a ferramenta

No checkout da DEILE, configure:

```
DEILE_BOT_ENDPOINT=http://127.0.0.1:8765
DEILE_BOT_AUTH_TOKEN=<bearer token configurado no control plane do bot>
```

A ferramenta `messaging.whatsapp_send_template` se registra automaticamente quando:

1. `deilebot` está no PYTHONPATH/importável, E
2. As duas variáveis acima estão setadas.

A ferramenta pede **aprovação explícita do operador a cada envio** (é `DANGEROUS` + `require_approval=True`) pois cada envio gera custo. Configure seu
`ApprovalSystem` para auto-liberar em dev ou integre com UI de confirmação para produção.

---

## 5. Validação ponta-a-ponta (10 cenários)

Esta é a bateria de testes que você deve executar antes de declarar o WhatsApp “pronto para produção”.
O detalhamento está em `docs/future/deilebot/whatsapp/03-FASE-E2E.md` (lado DEILE).
Registre os resultados em `docs/future/deilebot/whatsapp/05-E2E-RESULTADOS.md`.

| # | Cenário | Critério de aprovação |
|---|---------|----------------------|
| 1 | Texto recebido dentro da janela → resposta texto livre | Resposta chega e aparece no WhatsApp |
| 2 | Imagem recebida → adaptador recebe `Attachment(IMAGE)` | Ferramenta vê mime + url |
| 3 | Clique em Botão Reply → `interactive.button_reply` parseado → `env.text == title`, `env.raw["interactive"]["id"] == callback_data` | Teste enviando mensagem com botão e clicando |
| 4 | Seleção de Lista → `interactive.list_reply` parseado igual | Idem acima |
| 5 | Texto fora da janela de 24h → adaptador rejeita (ou pipeline redireciona para template) | Não envia em silêncio |
| 6 | Envio de template aprovado (utility, sem params) | Mensagem chega, métrica `category=utility,status=ok` incrementa |
| 7 | Envio de template aprovado (utility, 2 params) | Variáveis são substituídas corretamente |
| 8 | Nome de template não aprovado → ProviderError 132001 exibido ao operador | Ferramenta retorna erro, não gera cobrança |
| 9 | Param-count errado → ProviderError levantado antes do HTTP | Mensagem de erro nomeia o template |
| 10 | Envio de marketing (após verificação empresarial) | Meta aceita, métrica `category=marketing` incrementa |

Falha no E2E #5 geralmente significa que sua fundação está ignorando a janela — é a falha mais cara (você vai gastar créditos de marketing tentando mandar texto livre).

---

## 6. Indo para produção

- **Verificação empresarial** — submeta em `WhatsApp Manager → Account Info → Business Verification`. Reserve 1–2 semanas. Sem isso, só sandbox (50 destinatários únicos/dia, marketing desabilitado).
- **Aprovação do nome exibido** — submeta seu nome. 24h de análise.
- **Confiabilidade do webhook** — a Meta desativa o webhook após ~5 erros seguidos. Monitore com `health` no control plane do bot.
- **Dashboard de custos** — a métrica `bot_whatsapp_conversations_total{category,status}` é a fonte de verdade sobre gastos. Pluge onde você puder monitorar.
- **Rotação de token** — tokens de Usuário de Sistema não expiram, mas o usuário pode ser desabilitado. Tenha um segundo Usuário de Sistema preparado para rodar sem downtime.

---

## 7. Onde buscar solução

| Sintoma | Possível causa |
|---|-------------------|
| `132001 Template name does not exist` | Erro no nome/idioma ou template não aprovado na Meta |
| `131056 (Re-engagement message)` | Tentou texto livre fora da janela de 24h. Use template. |
| `131047 Re-engagement message` | Mesmo caso acima, mensagem de erro mais antiga. |
| `131051 Unsupported message type` | Você enviou tipo que a Cloud API rejeita (ex: interativo em número sem perfil Business). |
| Webhook em silêncio (nenhum evento recebido) | Token de verificação errado ou falha na assinatura do App Secret. Veja logs do bot. |
| `400 with no clear reason` | Quase sempre payload interativo passou do limite de caracteres imposto pela Meta. Catálogo aceita, a Cloud rejeita. |

Veja primeiro os `logs` do bot (`./data/logs/`), depois `bot_outbound_total{provider=whatsapp,status=fail}` para tendências. A Meta não oferece UI útil para “por que foi rejeitado” — seu único sinal é a resposta da API.
