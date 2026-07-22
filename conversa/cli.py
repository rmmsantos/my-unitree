"""Interface de linha de comandos."""

from __future__ import annotations

import os
from pathlib import Path
import sys

# Ao executar `python conversa/cli.py`, Python coloca a pasta `conversa/` em
# sys.path e pode importar uma instalação antiga do pacote. Dá prioridade à
# raiz deste projeto, tal como acontece com `python -m conversa.cli`.
if __package__ in {None, ""}:
    raiz_projeto = str(Path(__file__).resolve().parent.parent)
    if raiz_projeto in sys.path:
        sys.path.remove(raiz_projeto)
    sys.path.insert(0, raiz_projeto)

from dotenv import load_dotenv
from openai import OpenAI
import sounddevice as sd

from conversa.chat import Conversa
from conversa.voz import AudioTerminal, VozOpenAI, VozRealtime


def criar_aplicacao() -> tuple[Conversa, VozOpenAI, AudioTerminal]:
    """Carrega a configuração e cria os componentes da aplicação."""
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key == "coloca_a_tua_api_key_aqui":
        raise RuntimeError(
            "Falta a OPENAI_API_KEY. Copia .env.example para .env e adiciona a tua chave."
        )

    client = OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-5.6-terra").strip()
    modelo_transcricao = os.getenv(
        "OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"
    ).strip()
    modelo_voz = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts").strip()
    voz = os.getenv("OPENAI_TTS_VOICE", "marin").strip()
    return (
        Conversa(client, model=model),
        VozOpenAI(client, modelo_transcricao, modelo_voz, voz),
        AudioTerminal(),
    )


def criar_conversa() -> Conversa:
    """Mantém a função original disponível para integrações existentes."""
    conversa, _, _ = criar_aplicacao()
    return conversa


def criar_realtime() -> VozRealtime:
    """Carrega a configuração da conversa de voz em tempo real."""
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key == "coloca_a_tua_api_key_aqui":
        raise RuntimeError(
            "Falta a OPENAI_API_KEY. Copia .env.example para .env e adiciona a tua chave."
        )
    modelo = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1").strip()
    voz = os.getenv("OPENAI_REALTIME_VOICE", "marin").strip()
    backend_audio = os.getenv("AUDIO_BACKEND", "local").strip().lower()
    interface_ip = os.getenv("UNITREE_INTERFACE_IP", "192.168.123.164").strip()
    interface_rede = os.getenv("UNITREE_NETWORK_INTERFACE", "eth0").strip()
    grupo_microfone = os.getenv(
        "UNITREE_MIC_MULTICAST_GROUP", "239.168.123.161"
    ).strip()
    caminho_sdk = os.getenv("UNITREE_SDK_PATH", "").strip()
    debug_texto = os.getenv("AUDIO_DEBUG", "false").strip().lower()
    if debug_texto not in {"1", "true", "yes", "sim", "0", "false", "no", "nao", "não"}:
        raise RuntimeError("AUDIO_DEBUG tem de ser true ou false.")
    debug_audio = debug_texto in {"1", "true", "yes", "sim"}
    try:
        porta_microfone = int(os.getenv("UNITREE_MIC_PORT", "5555"))
    except ValueError as erro:
        raise RuntimeError("UNITREE_MIC_PORT tem de ser um número inteiro.") from erro
    return VozRealtime(
        api_key=api_key,
        modelo=modelo,
        voz=voz,
        backend_audio=backend_audio,
        unitree_interface_ip=interface_ip,
        unitree_interface_rede=interface_rede,
        unitree_grupo_microfone=grupo_microfone,
        unitree_porta_microfone=porta_microfone,
        unitree_sdk_path=caminho_sdk,
        debug_audio=debug_audio,
    )


def main() -> int:
    """Executa o ciclo interativo da aplicação."""
    try:
        conversa = criar_realtime()
    except RuntimeError as erro:
        print(f"Erro de configuração: {erro}", file=sys.stderr)
        return 1

    print("Conversa OpenAI por voz, em português de Portugal")
    print("A voz que irá ouvir é gerada por inteligência artificial.")
    try:
        conversa.executar()
    except KeyboardInterrupt:
        print("\nAté à próxima!")
        return 0
    except (RuntimeError, ValueError) as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 1
    except (OSError, sd.PortAudioError) as erro:
        print(f"Erro no dispositivo de áudio: {erro}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
