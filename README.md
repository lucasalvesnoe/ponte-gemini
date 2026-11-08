# ponte-gemini

Proxy local entre o Claude Code e os modelos de linguagem. Ele oferece duas funções:

1. **Outro modelo no `/model`.** Um modelo servido pelo [9router](https://github.com/decolua/9router) (por exemplo, Gemini) aparece como opção no seletor de modelos do Claude Code.
2. **Reserva automática.** Quando a Anthropic recusa a requisição por limite de uso ou falta de crédito, a mesma requisição é reenviada a uma cadeia de modelos de reserva no 9router. Quando o Claude volta a responder, a ponte retorna a ele sozinha.

O proxy escuta apenas em `127.0.0.1` e não registra cabeçalhos nem corpos de requisição.

## Como funciona

| Situação | Destino |
|---|---|
| Modelo com prefixo de provedor do 9router (`ag/`, `gc/`, `gh/`, `nvidia/`, `ollama/`, `openrouter/`...) | 9router (`PONTE_ROUTER`) |
| Qualquer outro modelo | `api.anthropic.com`, com o login do próprio Claude Code repassado sem alteração |
| Anthropic responde 429, ou 400/402/403 com mensagem de crédito ou cota | Cadeia de reserva (`PONTE_FALLBACK`), com notificação no macOS e aviso no início da resposta |
| Anthropic inacessível | Cadeia de reserva |

Antes de enviar ao 9router, a ponte remove campos exclusivos da Anthropic (`cache_control`, blocos de *thinking* assinados) e não repassa as credenciais do Claude Code: o 9router recebe somente a chave dele.

O estado atual (`claude` ou `reserva`) é gravado em `~/.claude/ponte-gemini/state.json`, o que permite exibi-lo numa statusline.

## Requisitos

- Python 3.10 ou superior.
- [9router](https://github.com/decolua/9router) rodando localmente, com a chave de API dele. Sem o 9router, o Claude continua funcionando; apenas a reserva e os modelos alternativos falham.
- macOS para o serviço do launchd, o Keychain e as notificações. Em outros sistemas, a ponte roda manualmente e as notificações vão para o log.

## Instalação (macOS)

```bash
git clone <url-deste-repositorio> ponte-gemini
cd ponte-gemini
security add-generic-password -a "$USER" -s 9router-local -w   # cola a chave do 9router quando pedir
bash scripts/install-macos.sh
```

O script cria o venv, instala as dependências, gera o plist em `~/Library/LaunchAgents/com.ponte-gemini.plist` com os caminhos da máquina e sobe o serviço. O log fica em `~/Library/Logs/ponte-gemini.log`.

Para remover o serviço: `bash scripts/uninstall-macos.sh`.

## Instalação manual (qualquer sistema)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
PONTE_ROUTER_KEY=<chave-do-9router> .venv/bin/python ponte.py
```

## Uso com o Claude Code

Recomenda-se abrir pela ponte somente as sessões que precisarem dela, por meio de um alias no `~/.zshrc`:

```bash
alias claude-ponte='ANTHROPIC_BASE_URL=http://127.0.0.1:8788 ANTHROPIC_CUSTOM_MODEL_OPTION=ag/gemini-3.8-flash-high ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Gemini (9router)" claude'
```

Com isso, `claude` abre a sessão normal e `claude-ponte` abre a sessão com o modelo alternativo no `/model` e a reserva automática.

Também é possível fixar a ponte para todas as sessões no bloco `env` de `~/.claude/settings.json`, mas há uma limitação: **com `ANTHROPIC_BASE_URL` apontando para outro endereço, o Claude Code desativa o Remote Control** (`/remote-control` passa a responder "Unknown command"). Por esse motivo, o alias é a forma recomendada.

## Configuração

| Variável | Padrão | Função |
|---|---|---|
| `PONTE_PORT` | `8788` | Porta local da ponte |
| `PONTE_UPSTREAM` | `https://api.anthropic.com` | Endereço da Anthropic |
| `PONTE_ROUTER` | `http://127.0.0.1:20128` | Endereço do 9router |
| `PONTE_FALLBACK` | `ag/gemini-3.8-flash-high,ag/gemini-3.7-flash-high,gc/gemini-3-flash-preview` | Cadeia de reserva, separada por vírgula |
| `PONTE_ROUTER_KEY` | (vazio) | Chave do 9router; quando vazia, a ponte consulta o Keychain |
| `PONTE_KEYCHAIN_SERVICE` | `9router-local` | Nome do item no Keychain do macOS |
| `PONTE_STATE_FILE` | `~/.claude/ponte-gemini/state.json` | Arquivo de estado para a statusline |
| `PONTE_LABEL` | `com.ponte-gemini` | Rótulo do serviço no launchd (só nos scripts de instalação) |

## Segurança

- A ponte escuta somente em `127.0.0.1`. Qualquer processo local, porém, consegue usar o 9router através dela sem conhecer a chave; por isso, não a exponha na rede.
- As credenciais do Claude Code seguem apenas para a Anthropic e nunca para o 9router.
- A chave do 9router fica no Keychain ou numa variável de ambiente, nunca no código.
- O log registra apenas método, caminho, status e modelo.

## Licença

MIT. Ver `LICENSE`.
