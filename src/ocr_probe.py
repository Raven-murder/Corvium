from io import BytesIO

if __package__:
    from .config import get_client, safe_error
    from .ocr import transcribe_image
else:
    from config import get_client, safe_error
    from ocr import transcribe_image


def main():
    try:
        from PIL import Image, ImageDraw, ImageFont

        model_ids = [model.id for model in get_client().models.list()]
        for model_id in model_ids:
            print(model_id)
        with Image.new("RGB", (600, 140), "white") as image:
            try:
                font = ImageFont.load_default(size=48)
            except TypeError:
                font = ImageFont.load_default()
            ImageDraw.Draw(image).text((20, 30), "OCR TEST 123", font=font, fill="black")
            output = BytesIO()
            image.save(output, format="PNG")
        for model_id in model_ids:
            if not any(term in model_id.lower() for term in ("vl", "vision", "deepseek", "qwen", "gemma", "cosmos", "llama")):
                continue
            try:
                result = transcribe_image(output.getvalue(), model_id)
                print(f"{model_id}: {' '.join(result.split())}")
            except Exception as error:
                print(f"{model_id}: {' '.join(safe_error(error).split())}")
    except Exception as error:
        print(" ".join(safe_error(error).split()))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
