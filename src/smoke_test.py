if __package__:
    from .config import CHAT_MODEL, get_client, safe_error
else:
    from config import CHAT_MODEL, get_client, safe_error


def print_error(error):
    print(safe_error(error))


def main():
    try:
        client = get_client()
    except Exception as error:
        print_error(error)
        return

    model_ids = []
    try:
        model_ids = [model.id for model in client.models.list()]
        for model_id in model_ids:
            if "nemotron" in model_id.lower() or "embed" in model_id.lower():
                print(model_id)
    except Exception as error:
        print_error(error)

    try:
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": "Say hello in one sentence."}],
            max_tokens=300,
        )
        print(response.choices[0].message.content)
        if response.usage is not None:
            print(f"prompt_tokens: {response.usage.prompt_tokens}")
            print(f"completion_tokens: {response.usage.completion_tokens}")
            print(f"total_tokens: {response.usage.total_tokens}")
        else:
            print("Usage token counts were not returned.")
    except Exception as error:
        print_error(error)

    try:
        embedding_model = next(
            (model_id for model_id in model_ids if "qwen3-embedding" in model_id.lower()),
            None,
        )
        if embedding_model is None:
            raise ValueError("No listed model ID contains 'qwen3-embedding'.")
        print(f"Embedding model: {embedding_model}")
        response = client.embeddings.create(model=embedding_model, input="hello world")
        print(f"Vector length: {len(response.data[0].embedding)}")
    except Exception as error:
        print_error(error)


if __name__ == "__main__":
    main()
