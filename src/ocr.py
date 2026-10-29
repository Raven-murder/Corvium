import base64
from io import BytesIO

if __package__:
    from .config import get_client, get_vision_model
else:
    from config import get_client, get_vision_model

PROMPT = "Transcribe all visible text exactly. Return only the transcription, with no commentary or formatting added. If there is no visible text, return an empty response."


def transcribe_image(image_bytes, model):
    from PIL import Image

    with Image.open(BytesIO(image_bytes)) as image:
        output = BytesIO()
        image.convert("RGB").save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    client = get_client()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
    ]}]
    for limit in (8000, 16000):
        response = client.chat.completions.create(model=model, messages=messages, max_tokens=limit)
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        content = getattr(getattr(choice, "message", None), "content", None)
        text = content.strip() if isinstance(content, str) else ""
        if text and getattr(choice, "finish_reason", None) != "length":
            return text
    return text


def ocr_image(image_bytes):
    model = get_vision_model()
    return transcribe_image(image_bytes, model) if model else ""
