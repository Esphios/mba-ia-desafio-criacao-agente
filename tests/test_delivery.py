from pathlib import Path
import hashlib
import subprocess


ROOT = Path(__file__).parents[1]


def test_readme_contains_required_sections_and_commands() -> None:
    readme = (ROOT / "README.md").read_text("utf-8")

    for heading in ("## Arquitetura", "## Garantias", "## Como rodar"):
        assert heading in readme
    assert "uv run aurora-reset" in readme
    assert "uv run aurora-api" in readme
    for path in (
        "src/aurora_assistant/storage.py",
        "src/aurora_assistant/tools.py",
        "src/aurora_assistant/runtime.py",
        "src/aurora_assistant/regulation.py",
    ):
        assert path in readme


def test_evaluator_script_exists_and_compiles() -> None:
    script = ROOT / "scripts" / "evaluate_flow.py"
    assert script.exists()
    result = subprocess.run(
        ["uv", "run", "python", "-m", "py_compile", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_adk_version_is_exact_and_secrets_are_not_tracked() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
    assert 'google-adk[db]==2.15.1' in pyproject

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert ".env" not in tracked


def test_seed_data_matches_upstream() -> None:
    expected = {
        "apartamentos.json": "d4b297899313f37e5bbb3ab9d1f7f070bb5985feca06058c5bce32c8c970b44b",
        "areas.json": "e56489365f156bd1be75961a3ccc965bf72cc2cef998bc5863c78ddded44c619",
        "regulamento.md": "86893f3b1471c06ec48c4004d2a9739134b22fac4ec5802b5d10a554cb5f4494",
        "reservas.json": "a1b849ac213ee8c208907e525e42d45f6ffe544710b7eed53a60f195e1a87197",
        "visitantes.json": "15a98d85b92479f5fa4f242d475c7ed4a2f7f4cd5826d2d0b867fa68c1fb88d5",
    }

    actual = {
        path.name: hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for path in sorted((ROOT / "dados").iterdir())
        if path.is_file()
    }
    assert actual == expected
