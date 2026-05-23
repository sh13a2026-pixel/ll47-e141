import io
import struct
import zlib

try:
    from PIL import Image
    _PIL_OK = True
except Exception:
    Image = None  # type: ignore
    _PIL_OK = False


def compress_image_bytes(image_bytes: bytes, max_size: int = 1280, quality: int = 75) -> tuple[bytes, bool]:
    """Nén ảnh và trả về (bytes, đã_convert_sang_jpeg).

    Ưu tiên Pillow (nén tốt nhất). Fallback thuần Python (chỉ strip metadata EXIF).
    - Nếu Pillow OK → resize + chuyển JPEG → (bytes_jpeg, True)
    - Nếu không có Pillow nhưng là JPEG → strip EXIF → (bytes_nhỏ_hơn, False)
    - Lỗi hoàn toàn → (bytes_gốc, False)
    """
    if _PIL_OK and Image is not None:
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
            print(f"[image_utils] Pillow compress failed: {e!r} — thử fallback")

    # --- Fallback thuần Python: strip EXIF khỏi JPEG ---
    try:
        result = _strip_jpeg_exif(image_bytes)
        if result and len(result) < len(image_bytes):
            return result, False
    except Exception:
        pass

    return image_bytes, False


def _strip_jpeg_exif(data: bytes) -> bytes:
    """Xóa EXIF/APP markers khỏi JPEG để giảm size mà không cần thư viện.
    Trả None nếu không phải JPEG hợp lệ."""
    if len(data) < 4 or data[:2] != b'\xff\xd8':
        return data  # Không phải JPEG — trả nguyên

    out = io.BytesIO()
    out.write(b'\xff\xd8')  # SOI marker

    i = 2
    while i < len(data) - 1:
        if data[i] != 0xff:
            break
        marker = data[i:i+2]
        i += 2

        # Markers không có length (SOI, EOI, RST*)
        if marker in (b'\xff\xd8', b'\xff\xd9') or (0xd0 <= marker[1] <= 0xd7):
            out.write(marker)
            if marker == b'\xff\xd9':  # EOI
                break
            continue

        if i + 2 > len(data):
            break
        length = struct.unpack('>H', data[i:i+2])[0]
        segment_end = i + length

        # APP0 (JFIF) giữ lại, APP1+ (EXIF/XMP) và APP2-APP15 → bỏ
        app0 = b'\xff\xe0'
        if marker == app0:
            out.write(marker)
            out.write(data[i:segment_end])
        elif marker[0] == 0xff and 0xe1 <= marker[1] <= 0xef:
            pass  # Bỏ EXIF/XMP/ICC/IPTC
        else:
            out.write(marker)
            out.write(data[i:segment_end])

        i = segment_end

    # Ghi phần còn lại (scan data)
    out.write(data[i:])
    return out.getvalue()
