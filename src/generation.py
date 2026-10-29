def stream_completion(client, *, on_chunk, check_cancel, **parameters):
    if check_cancel is None:
        check_cancel = lambda: None
    check_cancel()
    stream = client.chat.completions.create(**parameters, stream=True)
    text, finish_reason = [], None
    try:
        for chunk in stream:
            check_cancel()
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            content = choice.delta.content
            if content:
                text.append(content)
                on_chunk(content)
            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason
        check_cancel()
        return "".join(text), finish_reason
    finally:
        stream.close()
