from types import SimpleNamespace
import unittest

from conversa.chat import INSTRUCOES, Conversa


class ResponsesFalsas:
    def __init__(self) -> None:
        self.pedidos: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.pedidos.append(kwargs)
        numero = len(self.pedidos)
        return SimpleNamespace(id=f"resp_{numero}", output_text=f"Resposta {numero}")


def criar_conversa() -> tuple[Conversa, ResponsesFalsas]:
    responses = ResponsesFalsas()
    client = SimpleNamespace(responses=responses)
    return Conversa(client, model="modelo-teste"), responses


class TestConversa(unittest.TestCase):
    def test_primeira_mensagem_define_modelo_idioma_e_input(self) -> None:
        conversa, responses = criar_conversa()

        self.assertEqual(conversa.enviar("  Olá  "), "Resposta 1")
        self.assertEqual(
            responses.pedidos[0],
            {
                "model": "modelo-teste",
                "instructions": INSTRUCOES,
                "input": "Olá",
            },
        )

    def test_mensagem_seguinte_reutiliza_contexto(self) -> None:
        conversa, responses = criar_conversa()

        conversa.enviar("Primeira")
        conversa.enviar("Segunda")

        self.assertEqual(responses.pedidos[1]["previous_response_id"], "resp_1")

    def test_reiniciar_remove_contexto(self) -> None:
        conversa, responses = criar_conversa()

        conversa.enviar("Primeira")
        conversa.reiniciar()
        conversa.enviar("Nova conversa")

        self.assertNotIn("previous_response_id", responses.pedidos[1])

    def test_mensagem_vazia_e_rejeitada(self) -> None:
        conversa, _ = criar_conversa()

        with self.assertRaisesRegex(ValueError, "vazia"):
            conversa.enviar("   ")


if __name__ == "__main__":
    unittest.main()
