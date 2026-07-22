"""Áudio local e conversas de voz com a Realtime API da OpenAI."""

from __future__ import annotations

import base64
import json
from array import array
from contextlib import nullcontext
from pathlib import Path
from queue import Empty, Full, Queue
import socket
import struct
import sys
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol
from urllib.parse import urlencode
import wave

import sounddevice as sd
import websocket


PROMPT_TRANSCRICAO = (
    "Transcreve fielmente em português europeu (pt-PT), com a ortografia e "
    "pontuação usadas em Portugal."
)
INSTRUCOES_VOZ = (
    "Fala em português europeu, com pronúncia natural de Portugal. "
    "Usa um tom caloroso, claro e conversacional."
)
INSTRUCOES_REALTIME = """És um interlocutor útil, caloroso e natural.
Fala sempre em português europeu (pt-PT), com pronúncia, vocabulário e
construções naturais de Portugal. Responde normalmente numa ou duas frases e
deixa o utilizador continuar; só desenvolvas mais quando ele pedir detalhes.
Não faças introduções, recapitulações nem leias formatação, listas ou símbolos
em voz alta. Se não perceberes o utilizador, pede-lhe naturalmente para repetir.
"""

AMOSTRAGEM_REALTIME = 24_000
AMOSTRAGEM_UNITREE = 16_000
APLICACAO_AUDIO_UNITREE = "conversa-openai"


class ClienteAudio(Protocol):
    """Parte do cliente OpenAI usada para transcrição e síntese."""

    audio: object


class ClienteAudioUnitree(Protocol):
    """Parte do AudioClient do G1 usada para reproduzir PCM."""

    def PlayStream(self, app_name: str, stream_id: str, pcm_data: bytes) -> object: ...

    def PlayStop(self, app_name: str) -> object: ...


class ConversorPCM:
    """Reamostra PCM16 mono de forma incremental com interpolação linear."""

    def __init__(self, origem: int, destino: int) -> None:
        if origem <= 0 or destino <= 0:
            raise ValueError("As frequências de amostragem têm de ser positivas.")
        self._passo = origem / destino
        self._posicao = 0.0
        self._amostras = array("h")

    def converter(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("O bloco PCM16 tem um número ímpar de bytes.")
        novas = array("h")
        novas.frombytes(pcm)
        if sys.byteorder != "little":
            novas.byteswap()
        self._amostras.extend(novas)

        saida = array("h")
        while self._posicao + 1 < len(self._amostras):
            esquerda = int(self._posicao)
            fracao = self._posicao - esquerda
            valor = round(
                self._amostras[esquerda] * (1.0 - fracao)
                + self._amostras[esquerda + 1] * fracao
            )
            saida.append(max(-32_768, min(32_767, valor)))
            self._posicao += self._passo

        consumidas = int(self._posicao)
        if consumidas:
            del self._amostras[:consumidas]
            self._posicao -= consumidas
        if sys.byteorder != "little":
            saida.byteswap()
        return saida.tobytes()


class VozOpenAI:
    """Converte ficheiros de voz em texto e texto em ficheiros de voz."""

    def __init__(
        self,
        client: ClienteAudio,
        modelo_transcricao: str = "gpt-4o-mini-transcribe",
        modelo_voz: str = "gpt-4o-mini-tts",
        voz: str = "marin",
    ) -> None:
        self._client = client
        self._modelo_transcricao = modelo_transcricao
        self._modelo_voz = modelo_voz
        self._voz = voz

    def transcrever(self, caminho: Path) -> str:
        """Transcreve uma gravação, favorecendo a variante pt-PT."""
        with caminho.open("rb") as audio:
            resposta = self._client.audio.transcriptions.create(  # type: ignore[attr-defined]
                model=self._modelo_transcricao,
                file=audio,
                language="pt",
                prompt=PROMPT_TRANSCRICAO,
            )
        texto = str(resposta.text).strip()
        if not texto:
            raise RuntimeError("Não foi possível reconhecer fala na gravação.")
        return texto

    def sintetizar(self, texto: str, caminho: Path) -> None:
        """Gera uma resposta falada com pronúncia de Portugal."""
        texto = texto.strip()
        if not texto:
            raise ValueError("O texto a sintetizar não pode estar vazio.")

        with self._client.audio.speech.with_streaming_response.create(  # type: ignore[attr-defined]
            model=self._modelo_voz,
            voice=self._voz,
            input=texto,
            instructions=INSTRUCOES_VOZ,
            response_format="wav",
        ) as resposta:
            resposta.stream_to_file(caminho)


class VozRealtime:
    """Mantém uma conversa de voz bidirecional e interrompível em tempo real."""

    def __init__(
        self,
        api_key: str,
        modelo: str = "gpt-realtime-2.1",
        voz: str = "marin",
        instrucoes: str = INSTRUCOES_REALTIME,
        amostragem: int = AMOSTRAGEM_REALTIME,
        backend_audio: str = "local",
        unitree_interface_ip: str = "192.168.123.164",
        unitree_interface_rede: str = "eth0",
        unitree_grupo_microfone: str = "239.168.123.161",
        unitree_porta_microfone: int = 5555,
        unitree_sdk_path: str = "",
        debug_audio: bool = False,
        cliente_audio_unitree: ClienteAudioUnitree | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("A API key não pode estar vazia.")
        self._api_key = api_key
        self._modelo = modelo
        self._voz = voz
        self._instrucoes = instrucoes
        self._backend_audio = backend_audio.strip().lower()
        if self._backend_audio not in {"local", "unitree"}:
            raise ValueError("AUDIO_BACKEND tem de ser 'local' ou 'unitree'.")
        self._amostragem_entrada = (
            AMOSTRAGEM_UNITREE if self._backend_audio == "unitree" else amostragem
        )
        self._amostragem_saida = amostragem
        self._unitree_interface_ip = unitree_interface_ip
        self._unitree_interface_rede = unitree_interface_rede
        self._unitree_grupo_microfone = unitree_grupo_microfone
        self._unitree_porta_microfone = unitree_porta_microfone
        self._unitree_sdk_path = unitree_sdk_path.strip()
        self._debug_audio = debug_audio
        self._cliente_audio_unitree = cliente_audio_unitree

        self._socket: websocket.WebSocket | None = None
        self._socket_microfone: socket.socket | None = None
        self._envio_lock = Lock()
        self._estado_lock = Lock()
        self._parar = Event()
        self._microfone: Queue[bytes | None] = Queue(maxsize=100)
        self._reproducao: Queue[tuple[int, str, int, bytes | None]] = Queue()
        self._saida_audio: sd.RawOutputStream | None = None
        self._erro_background: BaseException | None = None
        self._debug_inicio = monotonic()
        self._debug_ultimos: dict[str, float] = {}
        self._bytes_microfone_recebidos = 0
        self._bytes_microfone_enviados = 0
        self._bytes_openai_recebidos = 0
        self._bytes_unitree_enviados = 0

        self._geracao_audio = 0
        self._item_audio: str | None = None
        self._indice_conteudo = 0
        self._inicio_reproducao: float | None = None
        self._amostras_enviadas = 0

    def configuracao_sessao(self) -> dict[str, object]:
        """Devolve o evento usado para configurar a sessão Realtime."""
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self._modelo,
                "output_modalities": ["audio"],
                "instructions": self._instrucoes,
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": self._amostragem_entrada,
                        },
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "high",
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    },
                    "output": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": self._amostragem_saida,
                        },
                        "voice": self._voz,
                    },
                },
            },
        }

    def executar(self) -> None:
        """Liga microfone e altifalantes até o utilizador premir Ctrl+C."""
        parametros = urlencode({"model": self._modelo})
        url = f"wss://api.openai.com/v1/realtime?{parametros}"
        self._parar.clear()
        self._erro_background = None
        self._debug(
            "Arranque: "
            f"backend={self._backend_audio}, modelo={self._modelo}, "
            f"entrada={self._amostragem_entrada} Hz, "
            f"saída={self._amostragem_saida} Hz."
        )

        try:
            if self._backend_audio == "unitree":
                self._preparar_unitree()
            self._debug("A ligar ao WebSocket da Realtime API…")
            self._socket = websocket.create_connection(
                url,
                header=[f"Authorization: Bearer {self._api_key}"],
                enable_multithread=True,
            )
            self._debug("WebSocket ligado.")
            self._enviar(self.configuracao_sessao())
            self._debug("Evento session.update enviado.")

            emissor = Thread(target=self._emitir_microfone, daemon=True)
            alvo_reproducao = (
                self._reproduzir_audio_unitree
                if self._backend_audio == "unitree"
                else self._reproduzir_audio_local
            )
            leitor = Thread(target=alvo_reproducao, daemon=True)
            emissor.start()
            leitor.start()
            self._debug("Threads de envio e reprodução iniciadas.")

            if self._backend_audio == "unitree":
                receptor = Thread(target=self._captar_audio_unitree, daemon=True)
                receptor.start()
                self._debug("Thread do array de microfones iniciada.")

            print("Sessão pronta. Fale naturalmente; pode interromper a resposta.")
            print("Prima Ctrl+C para terminar.\n")
            captura = (
                nullcontext()
                if self._backend_audio == "unitree"
                else sd.RawInputStream(
                    samplerate=self._amostragem_entrada,
                    blocksize=max(1, self._amostragem_entrada // 50),
                    channels=1,
                    dtype="int16",
                    callback=self._captar_audio_local,
                )
            )
            with captura:
                while not self._parar.is_set():
                    mensagem = self._socket.recv()
                    if not mensagem:
                        if self._erro_background is not None:
                            raise RuntimeError(str(self._erro_background))
                        raise RuntimeError("A ligação Realtime foi encerrada.")
                    self.processar_evento(json.loads(mensagem))
        except websocket.WebSocketException as erro:
            if self._erro_background is not None:
                raise RuntimeError(str(self._erro_background)) from self._erro_background
            raise RuntimeError(f"Falha na ligação Realtime: {erro}") from erro
        finally:
            self._debug("A terminar a sessão e a parar a reprodução.")
            self._parar.set()
            self._colocar_sem_bloquear(self._microfone, None)
            self._reproducao.put((self._geracao_audio, "", 0, None))
            if self._socket_microfone is not None:
                self._socket_microfone.close()
                self._socket_microfone = None
            if self._cliente_audio_unitree is not None:
                try:
                    self._cliente_audio_unitree.PlayStop(APLICACAO_AUDIO_UNITREE)
                except Exception:
                    pass
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def processar_evento(self, evento: dict[str, object]) -> None:
        """Processa um evento do servidor; público para permitir testes isolados."""
        tipo = evento.get("type")
        if tipo == "response.output_audio.delta":
            try:
                audio = base64.b64decode(str(evento["delta"]), validate=True)
                item_id = str(evento["item_id"])
                indice = int(evento.get("content_index", 0))
            except (KeyError, TypeError, ValueError) as erro:
                raise RuntimeError("A API devolveu um bloco de áudio inválido.") from erro
            self._bytes_openai_recebidos += len(audio)
            self._debug_periodico(
                "openai-audio",
                "OpenAI → áudio: "
                f"{self._bytes_openai_recebidos} bytes recebidos no total; "
                f"último bloco={len(audio)} bytes, pico={self._pico_pcm16(audio)}.",
            )
            with self._estado_lock:
                if item_id != self._item_audio:
                    self._item_audio = item_id
                    self._indice_conteudo = indice
                    self._inicio_reproducao = None
                    self._amostras_enviadas = 0
                geracao = self._geracao_audio
            self._reproducao.put((geracao, item_id, indice, audio))
        elif tipo == "response.output_audio.done":
            self._debug(
                "OpenAI → response.output_audio.done "
                f"(item={evento.get('item_id', self._item_audio)})."
            )
            with self._estado_lock:
                geracao = self._geracao_audio
                item_id = self._item_audio or str(evento.get("item_id", ""))
                indice = self._indice_conteudo
            self._reproducao.put((geracao, item_id, indice, None))
        elif tipo == "input_audio_buffer.speech_started":
            self._debug("OpenAI → fala detetada; a interromper a resposta atual.")
            self._interromper_resposta()
        elif tipo == "error":
            erro = evento.get("error")
            detalhe = erro.get("message") if isinstance(erro, dict) else erro
            raise RuntimeError(f"Erro da Realtime API: {detalhe or 'erro desconhecido'}")
        elif tipo == "response.done":
            resposta = evento.get("response")
            if isinstance(resposta, dict):
                self._debug(
                    "OpenAI → response.done: "
                    f"status={resposta.get('status')!r}, "
                    f"detalhes={resposta.get('status_details')!r}."
                )
            else:
                self._debug("OpenAI → evento response.done.")
        elif tipo in {
            "session.created",
            "session.updated",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "conversation.item.created",
            "response.created",
        }:
            self._debug(f"OpenAI → evento {tipo}.")
        else:
            self._debug_periodico(
                f"evento-{tipo}",
                f"OpenAI → evento {tipo!r} (não processado diretamente).",
                intervalo=2.0,
            )

    def _captar_audio_local(
        self, dados: object, _frames: int, _tempo: object, estado: object
    ) -> None:
        if estado:
            print(f"Aviso do microfone: {estado}")
        self._colocar_sem_bloquear(self._microfone, bytes(dados))

    def _preparar_unitree(self) -> None:
        if self._cliente_audio_unitree is None:
            if self._unitree_sdk_path:
                caminho_sdk = Path(self._unitree_sdk_path).expanduser()
                if not caminho_sdk.is_dir():
                    raise RuntimeError(
                        f"UNITREE_SDK_PATH não existe: {caminho_sdk}"
                    )
                caminho_sdk_texto = str(caminho_sdk)
                if caminho_sdk_texto not in sys.path:
                    sys.path.insert(0, caminho_sdk_texto)
                self._debug(f"SDK Unitree: {caminho_sdk}.")
            try:
                from unitree_sdk2py.core.channel import ChannelFactoryInitialize
                from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            except ImportError as erro:
                modulo = getattr(erro, "name", None) or "desconhecido"
                raise RuntimeError(
                    "Não foi possível importar o SDK Unitree. "
                    f"Módulo/dependência em falta: {modulo!r}. "
                    f"Detalhe original: {erro}. "
                    f"Python em uso: {sys.executable}."
                ) from erro
            self._debug(
                f"A iniciar DDS na interface {self._unitree_interface_rede}…"
            )
            ChannelFactoryInitialize(0, self._unitree_interface_rede)
            cliente = AudioClient()
            cliente.SetTimeout(3.0)
            cliente.Init()
            self._cliente_audio_unitree = cliente
            self._debug("AudioClient do G1 iniciado.")
            obter_volume = getattr(cliente, "GetVolume", None)
            if callable(obter_volume):
                try:
                    self._debug(f"Volume atual do G1: {obter_volume()!r}.")
                except Exception as erro:
                    self._debug(f"Não foi possível consultar o volume: {erro}.")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(self._unitree_interface_ip),
        )
        sock.bind(("", self._unitree_porta_microfone))
        membro = struct.pack(
            "=4s4s",
            socket.inet_aton(self._unitree_grupo_microfone),
            socket.inet_aton(self._unitree_interface_ip),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membro)
        sock.settimeout(0.5)
        self._socket_microfone = sock
        self._debug(
            "Multicast do array pronto: "
            f"{self._unitree_grupo_microfone}:{self._unitree_porta_microfone} "
            f"pela interface/IP {self._unitree_interface_rede}/"
            f"{self._unitree_interface_ip}."
        )

    def _captar_audio_unitree(self) -> None:
        if self._socket_microfone is None:
            self._falhar_background(RuntimeError("O microfone do G1 não foi iniciado."))
            return
        limite_inicial = monotonic() + 5.0
        recebeu_audio = False
        while not self._parar.is_set():
            try:
                dados, _origem = self._socket_microfone.recvfrom(65_536)
            except socket.timeout:
                if not recebeu_audio and monotonic() >= limite_inicial:
                    self._falhar_background(
                        RuntimeError(
                            "O array de microfones do G1 não enviou áudio. "
                            "Na aplicação Unitree, ativa Voice Assistant > "
                            "Wake-up Conversation Mode e confirma o IP/interface."
                        )
                    )
                    return
                continue
            except OSError as erro:
                if not self._parar.is_set():
                    self._falhar_background(erro)
                return
            if len(dados) % 2:
                dados = dados[:-1]
            if dados:
                recebeu_audio = True
                self._bytes_microfone_recebidos += len(dados)
                self._debug_periodico(
                    "microfone-recebido",
                    "Array G1 → aplicação: "
                    f"{self._bytes_microfone_recebidos} bytes no total; "
                    f"último datagrama={len(dados)} bytes, "
                    f"pico={self._pico_pcm16(dados)}, "
                    f"fila={self._microfone.qsize()}.",
                )
                self._colocar_sem_bloquear(self._microfone, dados)

    @staticmethod
    def _colocar_sem_bloquear(fila: Queue[bytes | None], valor: bytes | None) -> None:
        try:
            fila.put_nowait(valor)
        except Full:
            # Não bloquear a callback de áudio se a rede estiver temporariamente lenta.
            pass

    def _emitir_microfone(self) -> None:
        while not self._parar.is_set():
            try:
                dados = self._microfone.get(timeout=0.1)
            except Empty:
                continue
            if dados is None:
                return
            try:
                self._enviar(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(dados).decode("ascii"),
                    }
                )
                self._bytes_microfone_enviados += len(dados)
                self._debug_periodico(
                    "microfone-enviado",
                    "Aplicação → OpenAI: "
                    f"{self._bytes_microfone_enviados} bytes PCM enviados no total; "
                    f"fila={self._microfone.qsize()}.",
                )
            except RuntimeError as erro:
                self._debug(f"Falha ao enviar microfone: {erro}.")
                self._parar.set()
                if self._socket is not None:
                    self._socket.close()
                return

    def _reproduzir_audio_local(self) -> None:
        try:
            with sd.RawOutputStream(
                samplerate=self._amostragem_saida, channels=1, dtype="int16"
            ) as saida:
                with self._estado_lock:
                    self._saida_audio = saida
                while not self._parar.is_set():
                    try:
                        geracao, item_id, _indice, dados = self._reproducao.get(
                            timeout=0.1
                        )
                    except Empty:
                        continue
                    with self._estado_lock:
                        if geracao != self._geracao_audio:
                            continue
                        if dados is None:
                            continue
                        if self._inicio_reproducao is None:
                            self._inicio_reproducao = monotonic()
                        saida.write(dados)
                        self._amostras_enviadas += len(dados) // 2
        except (OSError, sd.PortAudioError) as erro:
            self._falhar_background(erro)
        finally:
            with self._estado_lock:
                self._saida_audio = None

    def _reproduzir_audio_unitree(self) -> None:
        cliente = self._cliente_audio_unitree
        if cliente is None:
            self._falhar_background(RuntimeError("O altifalante do G1 não foi iniciado."))
            return

        item_corrente: str | None = None
        conversor = ConversorPCM(self._amostragem_saida, AMOSTRAGEM_UNITREE)
        self._debug("Reprodutor Unitree pronto; à espera de áudio da OpenAI.")
        try:
            while not self._parar.is_set():
                try:
                    geracao, item_id, _indice, dados = self._reproducao.get(
                        timeout=0.1
                    )
                except Empty:
                    continue
                with self._estado_lock:
                    if geracao != self._geracao_audio or dados is None:
                        continue
                    if item_id != item_corrente:
                        item_corrente = item_id
                        conversor = ConversorPCM(
                            self._amostragem_saida, AMOSTRAGEM_UNITREE
                        )
                    pcm_unitree = conversor.converter(dados)
                    if not pcm_unitree:
                        continue
                    if self._inicio_reproducao is None:
                        self._inicio_reproducao = monotonic()
                    retorno = cliente.PlayStream(
                        APLICACAO_AUDIO_UNITREE, item_id, pcm_unitree
                    )
                    codigo = retorno[0] if isinstance(retorno, tuple) else retorno
                    self._bytes_unitree_enviados += len(pcm_unitree)
                    self._debug_periodico(
                        "unitree-playstream",
                        "Aplicação → PlayStream: "
                        f"{self._bytes_unitree_enviados} bytes PCM16/16 kHz no total; "
                        f"último bloco={len(pcm_unitree)} bytes, "
                        f"pico={self._pico_pcm16(pcm_unitree)}, "
                        f"item={item_id}, retorno={codigo!r}.",
                    )
                    if codigo not in (None, 0):
                        raise RuntimeError(
                            f"O AudioClient do G1 devolveu o código {codigo}."
                        )
                    self._amostras_enviadas += len(dados) // 2
        except Exception as erro:
            self._falhar_background(erro)

    def _interromper_resposta(self) -> None:
        with self._estado_lock:
            item_id = self._item_audio
            indice = self._indice_conteudo
            duracao_enviada = self._amostras_enviadas / self._amostragem_saida
            duracao_decorrida = (
                monotonic() - self._inicio_reproducao
                if self._inicio_reproducao is not None
                else 0.0
            )
            fim_ms = round(min(duracao_enviada, duracao_decorrida) * 1000)
            if self._saida_audio is not None:
                self._saida_audio.abort()
                self._saida_audio.start()
            self._geracao_audio += 1
            self._item_audio = None
            self._inicio_reproducao = None
            self._amostras_enviadas = 0

        if self._cliente_audio_unitree is not None:
            try:
                retorno = self._cliente_audio_unitree.PlayStop(
                    APLICACAO_AUDIO_UNITREE
                )
                self._debug(f"PlayStop enviado ao G1; retorno={retorno!r}.")
            except Exception as erro:
                self._falhar_background(erro)

        while True:
            try:
                self._reproducao.get_nowait()
            except Empty:
                break

        if item_id is not None:
            self._enviar(
                {
                    "type": "conversation.item.truncate",
                    "item_id": item_id,
                    "content_index": indice,
                    "audio_end_ms": fim_ms,
                }
            )

    def _enviar(self, evento: dict[str, object]) -> None:
        if self._socket is None:
            raise RuntimeError("A sessão Realtime não está ligada.")
        try:
            with self._envio_lock:
                self._socket.send(json.dumps(evento))
        except websocket.WebSocketException as erro:
            raise RuntimeError(f"Falha ao enviar áudio: {erro}") from erro

    def _falhar_background(self, erro: BaseException) -> None:
        self._debug(
            f"ERRO numa thread de áudio: {type(erro).__name__}: {erro}.",
            forcar=True,
        )
        self._erro_background = erro
        self._parar.set()
        if self._socket is not None:
            self._socket.close()

    def _debug(self, mensagem: str, forcar: bool = False) -> None:
        if not self._debug_audio and not forcar:
            return
        decorrido = monotonic() - self._debug_inicio
        print(f"[audio +{decorrido:7.2f}s] {mensagem}", flush=True)

    def _debug_periodico(
        self, chave: str, mensagem: str, intervalo: float = 1.0
    ) -> None:
        if not self._debug_audio:
            return
        agora = monotonic()
        anterior = self._debug_ultimos.get(chave, 0.0)
        if agora - anterior >= intervalo:
            self._debug_ultimos[chave] = agora
            self._debug(mensagem)

    @staticmethod
    def _pico_pcm16(pcm: bytes) -> int:
        if len(pcm) < 2:
            return 0
        amostras = array("h")
        amostras.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if sys.byteorder != "little":
            amostras.byteswap()
        return max((abs(valor) for valor in amostras), default=0)


class AudioTerminal:
    """Capta e reproduz áudio através dos dispositivos predefinidos do sistema."""

    def __init__(self, amostragem: int = 16_000) -> None:
        self._amostragem = amostragem

    def gravar(self, caminho: Path) -> None:
        """Grava do microfone até o utilizador premir Enter."""
        blocos: list[bytes] = []

        def guardar_bloco(
            dados: object, _frames: int, _tempo: object, estado: object
        ) -> None:
            if estado:
                print(f"Aviso de áudio: {estado}")
            blocos.append(bytes(dados))

        print("A gravar… fale agora e prima Enter quando terminar.")
        with sd.RawInputStream(
            samplerate=self._amostragem,
            channels=1,
            dtype="int16",
            callback=guardar_bloco,
        ):
            input()

        if not blocos:
            raise RuntimeError("O microfone não captou áudio.")

        with wave.open(str(caminho), "wb") as ficheiro:
            ficheiro.setnchannels(1)
            ficheiro.setsampwidth(2)
            ficheiro.setframerate(self._amostragem)
            ficheiro.writeframes(b"".join(blocos))

    def reproduzir(self, caminho: Path) -> None:
        """Reproduz um WAV no dispositivo de saída predefinido."""
        with wave.open(str(caminho), "rb") as ficheiro:
            canais = ficheiro.getnchannels()
            amostragem = ficheiro.getframerate()
            largura = ficheiro.getsampwidth()
            if largura != 2:
                raise RuntimeError("Foi recebido áudio num formato WAV não suportado.")
            with sd.RawOutputStream(
                samplerate=amostragem, channels=canais, dtype="int16"
            ) as saida:
                while dados := ficheiro.readframes(4096):
                    saida.write(dados)
