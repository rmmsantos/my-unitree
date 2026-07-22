import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest

from conversa.voz import (
    APLICACAO_AUDIO_UNITREE,
    ConversorPCM,
    INSTRUCOES_VOZ,
    PROMPT_TRANSCRICAO,
    VozOpenAI,
    VozRealtime,
)


class TranscricoesFalsas:
    def __init__(self) -> None:
        self.pedidos: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.pedidos.append(kwargs)
        return SimpleNamespace(text="  Bom dia!  ")


class RespostaVozFalsa:
    def __init__(self, destino: list[Path]) -> None:
        self._destino = destino

    def __enter__(self) -> "RespostaVozFalsa":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def stream_to_file(self, caminho: Path) -> None:
        self._destino.append(caminho)


class SinteseFalsa:
    def __init__(self) -> None:
        self.pedidos: list[dict[str, object]] = []
        self.destinos: list[Path] = []
        self.with_streaming_response = self

    def create(self, **kwargs: object) -> RespostaVozFalsa:
        self.pedidos.append(kwargs)
        return RespostaVozFalsa(self.destinos)


def criar_voz() -> tuple[VozOpenAI, TranscricoesFalsas, SinteseFalsa]:
    transcricoes = TranscricoesFalsas()
    sintese = SinteseFalsa()
    client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=transcricoes, speech=sintese)
    )
    return VozOpenAI(client, "modelo-stt", "modelo-tts", "voz-teste"), transcricoes, sintese


class TestVozOpenAI(unittest.TestCase):
    def test_transcricao_pede_portugues_de_portugal(self) -> None:
        voz, transcricoes, _ = criar_voz()
        with TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "pergunta.wav"
            caminho.write_bytes(b"audio")

            self.assertEqual(voz.transcrever(caminho), "Bom dia!")

        pedido = transcricoes.pedidos[0]
        self.assertEqual(pedido["model"], "modelo-stt")
        self.assertEqual(pedido["language"], "pt")
        self.assertEqual(pedido["prompt"], PROMPT_TRANSCRICAO)

    def test_sintese_pede_pronuncia_ptpt_e_wav(self) -> None:
        voz, _, sintese = criar_voz()
        caminho = Path("resposta.wav")

        voz.sintetizar("  Olá!  ", caminho)

        self.assertEqual(
            sintese.pedidos[0],
            {
                "model": "modelo-tts",
                "voice": "voz-teste",
                "input": "Olá!",
                "instructions": INSTRUCOES_VOZ,
                "response_format": "wav",
            },
        )
        self.assertEqual(sintese.destinos, [caminho])

    def test_sintese_rejeita_texto_vazio(self) -> None:
        voz, _, _ = criar_voz()

        with self.assertRaisesRegex(ValueError, "vazio"):
            voz.sintetizar("  ", Path("resposta.wav"))


class SocketFalso:
    def __init__(self) -> None:
        self.eventos: list[dict[str, object]] = []

    def send(self, mensagem: str) -> None:
        self.eventos.append(json.loads(mensagem))


class SaidaFalsa:
    def __init__(self) -> None:
        self.abortada = 0
        self.iniciada = 0

    def abort(self) -> None:
        self.abortada += 1

    def start(self) -> None:
        self.iniciada += 1


class ClienteUnitreeFalso:
    def __init__(self) -> None:
        self.blocos: list[tuple[str, str, bytes]] = []
        self.paragens: list[str] = []
        self.reproduziu = Event()

    def PlayStream(self, app_name: str, stream_id: str, pcm: bytes) -> tuple[int, None]:
        self.blocos.append((app_name, stream_id, pcm))
        self.reproduziu.set()
        return (0, None)

    def PlayStop(self, app_name: str) -> tuple[int, None]:
        self.paragens.append(app_name)
        return (0, None)


class TestVozRealtime(unittest.TestCase):
    def test_configura_audio_pcm_vad_semantico_e_ptpt(self) -> None:
        voz = VozRealtime("chave", modelo="modelo-rt", voz="voz-teste")

        evento = voz.configuracao_sessao()
        sessao = evento["session"]

        self.assertEqual(evento["type"], "session.update")
        self.assertEqual(sessao["model"], "modelo-rt")
        self.assertEqual(sessao["output_modalities"], ["audio"])
        self.assertIn("português europeu", sessao["instructions"])
        self.assertEqual(
            sessao["audio"]["input"]["format"],
            {"type": "audio/pcm", "rate": 24_000},
        )
        self.assertEqual(
            sessao["audio"]["input"]["turn_detection"],
            {
                "type": "semantic_vad",
                "eagerness": "high",
                "create_response": True,
                "interrupt_response": True,
            },
        )
        self.assertEqual(
            sessao["audio"]["output"]["format"],
            {"type": "audio/pcm", "rate": 24_000},
        )
        self.assertEqual(sessao["audio"]["output"]["voice"], "voz-teste")

    def test_unitree_configura_entrada_a_16khz_e_saida_a_24khz(self) -> None:
        voz = VozRealtime("chave", backend_audio="unitree")

        audio = voz.configuracao_sessao()["session"]["audio"]

        self.assertEqual(audio["input"]["format"]["rate"], 16_000)
        self.assertEqual(audio["output"]["format"]["rate"], 24_000)

    def test_delta_de_audio_e_descodificado_para_reproducao(self) -> None:
        voz = VozRealtime("chave")
        pcm = b"\x01\x00\x02\x00"

        voz.processar_evento(
            {
                "type": "response.output_audio.delta",
                "item_id": "item_1",
                "content_index": 0,
                "delta": base64.b64encode(pcm).decode("ascii"),
            }
        )

        self.assertEqual(voz._reproducao.get_nowait(), (0, "item_1", 0, pcm))

    def test_inicio_de_fala_descarta_audio_e_trunca_resposta(self) -> None:
        voz = VozRealtime("chave")
        socket = SocketFalso()
        voz._socket = socket
        voz._item_audio = "item_1"
        voz._amostras_enviadas = 24_000
        voz._inicio_reproducao = 0.0
        saida = SaidaFalsa()
        voz._saida_audio = saida
        voz._reproducao.put((0, "item_1", 0, b"audio por ouvir"))

        voz.processar_evento({"type": "input_audio_buffer.speech_started"})

        self.assertTrue(voz._reproducao.empty())
        self.assertEqual(saida.abortada, 1)
        self.assertEqual(saida.iniciada, 1)
        self.assertEqual(socket.eventos[0]["type"], "conversation.item.truncate")
        self.assertEqual(socket.eventos[0]["item_id"], "item_1")
        self.assertEqual(socket.eventos[0]["audio_end_ms"], 1000)

    def test_unitree_converte_e_envia_audio_para_altifalante(self) -> None:
        cliente = ClienteUnitreeFalso()
        voz = VozRealtime(
            "chave", backend_audio="unitree", cliente_audio_unitree=cliente
        )
        pcm_24khz = b"\x01\x00" * 240
        voz.processar_evento(
            {
                "type": "response.output_audio.delta",
                "item_id": "item_g1",
                "content_index": 0,
                "delta": base64.b64encode(pcm_24khz).decode("ascii"),
            }
        )
        leitor = Thread(target=voz._reproduzir_audio_unitree)
        leitor.start()

        self.assertTrue(cliente.reproduziu.wait(timeout=1))
        voz._parar.set()
        leitor.join(timeout=1)

        aplicacao, stream_id, pcm_16khz = cliente.blocos[0]
        self.assertEqual(aplicacao, APLICACAO_AUDIO_UNITREE)
        self.assertEqual(stream_id, "item_g1")
        self.assertEqual(len(pcm_16khz), 160 * 2)

    def test_interrupcao_para_altifalante_unitree(self) -> None:
        cliente = ClienteUnitreeFalso()
        voz = VozRealtime(
            "chave", backend_audio="unitree", cliente_audio_unitree=cliente
        )
        socket = SocketFalso()
        voz._socket = socket
        voz._item_audio = "item_g1"

        voz.processar_evento({"type": "input_audio_buffer.speech_started"})

        self.assertEqual(cliente.paragens, [APLICACAO_AUDIO_UNITREE])
        self.assertEqual(socket.eventos[0]["type"], "conversation.item.truncate")


class TestConversorPCM(unittest.TestCase):
    def test_converte_pcm16_de_24khz_para_16khz(self) -> None:
        conversor = ConversorPCM(24_000, 16_000)

        convertido = conversor.converter(b"\x10\x00" * 240)

        self.assertEqual(len(convertido), 160 * 2)

    def test_rejeita_bloco_pcm_incompleto(self) -> None:
        with self.assertRaisesRegex(ValueError, "ímpar"):
            ConversorPCM(24_000, 16_000).converter(b"\x00")


if __name__ == "__main__":
    unittest.main()
