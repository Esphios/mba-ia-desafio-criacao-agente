from pathlib import Path

from aurora_assistant.regulation import RegulationIndex


def test_pool_query_returns_only_pool_chapter() -> None:
    index = RegulationIndex(Path(__file__).parents[1] / "dados" / "regulamento.md")

    result = index.search("Até que horas a piscina funciona aos domingos?")

    assert "Capítulo IV: Piscina" in result
    assert "20h" in result
    assert "Capítulo V" not in result
    assert "garagem" not in result.lower()


def test_regulation_is_split_into_independent_chapters() -> None:
    index = RegulationIndex(Path(__file__).parents[1] / "dados" / "regulamento.md")

    assert len(index.chapters) == 14
    assert all(chapter.text.count("## Capítulo") == 1 for chapter in index.chapters)


def test_moving_query_selects_moving_chapter_not_pool_or_common_areas() -> None:
    index = RegulationIndex(Path(__file__).parents[1] / "dados" / "regulamento.md")

    result = index.search("Qual horário de mudança aos domingos?")

    assert "Capítulo IX: Mudanças" in result
    assert "Capítulo V" not in result


def test_noise_query_and_unknown_subject_do_not_select_unrelated_chapters() -> None:
    index = RegulationIndex(Path(__file__).parents[1] / "dados" / "regulamento.md")

    noise = index.search("Quais as regras para barulho no domingo?")
    unknown = index.search("Qual a política sobre drones?")

    assert "Capítulo III: Silêncio e boa convivência" in noise
    assert unknown == "Não encontrei um capítulo relacionado no regulamento."
