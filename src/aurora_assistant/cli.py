def run_api() -> None:
    import os

    import uvicorn

    port = int(os.getenv("AURORA_PORT", "8000"))
    uvicorn.run("aurora_assistant.api:app", host="0.0.0.0", port=port)


def reset_data() -> None:
    from aurora_assistant.storage import reset_database

    reset_database()
