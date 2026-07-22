"""Lógica de conversa com a Responses API da OpenAI."""

from __future__ import annotations

from typing import Protocol


INSTRUCOES = """És um assistente útil, claro e rigoroso.
Responde sempre em português europeu (pt-PT), usando vocabulário, ortografia e
construções naturais de Portugal. Evita português do Brasil, salvo quando o
utilizador pedir explicitamente uma tradução, comparação ou citação.
"""


class ResponsesClient(Protocol):
    """Parte do cliente OpenAI usada pela aplicação."""

    class Responses(Protocol):
        def create(self, **kwargs: object) -> object: ...

    responses: Responses


class Conversa:
    """Mantém o contexto de uma conversa entre pedidos."""

    def __init__(self, client: ResponsesClient, model: str) -> None:
        self._client = client
        self._model = model
        self._previous_response_id: str | None = None

    def enviar(self, mensagem: str) -> str:
        """Envia uma mensagem e devolve o texto produzido pelo modelo."""
        mensagem = mensagem.strip()
        if not mensagem:
            raise ValueError("A mensagem não pode estar vazia.")

        parametros: dict[str, object] = {
            "model": self._model,
            "instructions": INSTRUCOES,
            "input": mensagem,
        }
        if self._previous_response_id:
            parametros["previous_response_id"] = self._previous_response_id

        resposta = self._client.responses.create(**parametros)
        self._previous_response_id = str(resposta.id)  # type: ignore[attr-defined]
        texto = str(resposta.output_text).strip()  # type: ignore[attr-defined]

        if not texto:
            raise RuntimeError("O modelo não devolveu uma resposta de texto.")
        return texto

    def reiniciar(self) -> None:
        """Começa uma conversa sem o contexto anterior."""
        self._previous_response_id = None

