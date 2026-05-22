import io
try:
    from PIL import Image
    _PIL_OK = True
except Exception:
    Image = None  # type: ignore
    _PIL_OK = False


def compress_image_bytes(image_bytes: bytes, max_size: int = 1280, quality: int = 75) -> tuple[bytes, bool]:
    """Nén ảnh và trả về (bytes, đã_convert_sang_jpeg).

    Trả tuple để caller biết content-type có phải đổi sang image/jpeg không.
    - Nếu Pillow OK → resize + chuyển JPEG → (bytes_jpeg, True)
    - Nếu lỗi (không phải ảnh, không có PIL, format không hỗ trợ) → (bytes_gốc, False)
      → caller phải giữ nguyên content-type gốc, KHÔNG ép thành image/jpeg.
    """
    if not _PIL_OK or Image is None:
        return image_bytes, False
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        width, height = img.size
        if width > max_size or height > max_size:
            if width > height:
                new_width = max_size
                new_height = int((max_size / width) * height)
            else:
                new_height = max_size
                new_width = int((max_size / height) * width)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=quality, optimize=True)
        return out_buf.getvalue(), True
    except Exception as e:
        print(f"[image_utils] compress failed: {e!r} — gửi nguyên bytes gốc")
        return image_bytes, False
