from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Chapter:
    title: str
    text: str


def _terms(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    stopwords = {
        "aos", "ate", "como", "das", "dia", "dias", "domingo", "domingos",
        "funciona", "horario", "horas", "nos", "para", "pode", "politica",
        "qual", "quais", "que", "regras", "sobre", "uma", "uso",
    }
    return {
        term
        for term in re.findall(r"[a-z0-9]+", ascii_text)
        if len(term) > 2 and term not in stopwords
    }


class RegulationIndex:
    """Índice local que nunca devolve mais de um capítulo por consulta."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        content = self.path.read_text("utf-8")
        matches = list(re.finditer(r"(?m)^## (Capítulo .+)$", content))
        self.chapters = [
            Chapter(
                title=match.group(1),
                text=content[match.start() : matches[index + 1].start()].strip()
                if index + 1 < len(matches)
                else content[match.start() :].strip(),
            )
            for index, match in enumerate(matches)
        ]

    def search(self, query: str) -> str:
        query_terms = _terms(query)
        aliases = {
            "nadar": {"piscina"},
            "festa": {"salao"},
            "churrasco": {"churrasqueira"},
            "carro": {"garagem", "veiculos"},
            "pet": {"animais"},
            "cachorro": {"animais"},
            "lixo": {"coleta", "reciclagem"},
            "mudanca": {"mudancas"},
            "mudar": {"mudancas"},
            "barulho": {"silencio", "convivencia"},
            "ruido": {"silencio", "convivencia"},
        }
        expanded = set(query_terms)
        for term in query_terms:
            expanded.update(aliases.get(term, set()))

        def score(chapter: Chapter) -> tuple[int, int, int]:
            title_terms = _terms(chapter.title)
            body_terms = _terms(chapter.text)
            title_hits = len(expanded & title_terms)
            body_hits = len(expanded & body_terms)
            return (title_hits * 10 + body_hits, title_hits, body_hits)

        if not self.chapters:
            return "Nenhum capítulo do regulamento foi encontrado."
        best = max(self.chapters, key=score)
        _, title_hits, body_hits = score(best)
        if title_hits == 0 and body_hits < 2:
            return "Não encontrei um capítulo relacionado no regulamento."
        return best.text
