# Conversa OpenAI em português de Portugal

Uma aplicação Python para manter uma conversa de voz natural no terminal com a
Realtime API da OpenAI. O áudio é enviado e recebido continuamente, sem uma
etapa separada de gravação, transcrição e síntese. A conversa mantém contexto,
deteta automaticamente os turnos e permite interromper o assistente enquanto
este fala.

## Requisitos

- Python 3.8 ou superior
- Uma API key da OpenAI
- Microfone e saída de áudio

> A utilização da API é faturada separadamente de uma subscrição do ChatGPT.
> Consulta os limites e preços da tua conta antes de utilizar a aplicação.

## Instalação

Cria e ativa um ambiente virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

No Windows PowerShell, o segundo comando é:

```powershell
.venv\Scripts\Activate.ps1
```

Instala o projeto:

```bash
python -m pip install -e .
```

Cria o ficheiro de configuração:

```bash
cp .env.example .env
```

Abre `.env` e substitui `coloca_a_tua_api_key_aqui` pela tua chave. Nunca
partilhes nem guardes a chave num commit; o ficheiro `.env` já está excluído
pelo `.gitignore`.

## Utilização

```bash
conversa
```

Começa a falar quando surgir a indicação `Sessão pronta`. Não é necessário
premir teclas entre turnos e pode interromper uma resposta falando por cima.
Usa `Ctrl+C` para terminar a sessão.

Os modelos e a voz podem ser alterados em `.env`:

```dotenv
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
OPENAI_REALTIME_VOICE=marin
```

O microfone e a saída usam PCM mono a 24 kHz. As instruções da sessão pedem
português europeu (`pt-PT`) e respostas curtas, adequadas a voz. Auscultadores
são recomendados para evitar que o microfone capte a voz do assistente.

> A voz reproduzida é gerada por inteligência artificial. A aplicação mostra
> este aviso ao arrancar, conforme exigido para texto-para-voz.

## Execução no Unitree G1 EDU+

No PC2 (NVIDIA Orin NX, normalmente `192.168.123.164`), a aplicação pode usar
diretamente o array de microfones e o altifalante do robô. O array fornece PCM
mono a 16 kHz por multicast; a saída da Realtime API é convertida de 24 para
16 kHz antes de ser enviada ao `AudioClient` do G1.

O `unitree_sdk2_python` tem de estar disponível no mesmo ambiente. Se o exemplo
oficial já funciona diretamente a partir do repositório, não é preciso instalar
o SDK com `pip`. O caminho do repositório é configurado no `.env`:

```bash
cd ~/work/conversation/noBackup
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -e .
```

No `.env` do robô, usa:

```dotenv
AUDIO_BACKEND=unitree
UNITREE_INTERFACE_IP=192.168.123.164
UNITREE_NETWORK_INTERFACE=eth0
UNITREE_SDK_PATH=/home/rmartins/work/unitree_sdk2_python
UNITREE_MIC_MULTICAST_GROUP=239.168.123.161
UNITREE_MIC_PORT=5555
AUDIO_DEBUG=true
```

Confirma o nome da interface com `ip -br address`: é a interface que possui o
endereço `.164`; se não for `eth0`, altera `UNITREE_NETWORK_INTERFACE`.

Antes de arrancar, abre a aplicação Unitree e ativa **Voice Assistant → Wake-up
Conversation Mode**. Sem este modo, o multicast do array não chega ao programa.
Depois executa:

```bash
conversa
```

Quando começa a falar por cima da resposta, o VAD da OpenAI interrompe a geração,
limpa o áudio pendente e chama `PlayStop` no altifalante do G1.

Com `AUDIO_DEBUG=true`, o terminal mostra os bytes e o pico PCM em cada etapa do
percurso. Um pico sempre igual a zero significa silêncio digital; a presença de
`response.output_audio.delta` seguida de `PlayStream` confirma que a resposta
chegou ao cliente do altifalante.

## Testes

Os testes não usam a API nem consomem créditos:

```bash
python -m unittest discover -s tests -v
```

## Estrutura

```text
conversa/cli.py   arranque e configuração
conversa/voz.py   sessão Realtime, microfone, reprodução e interrupções
tests/             testes unitários sem acesso à rede
```
