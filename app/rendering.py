"""Original 1080x1350 designs, primitives and system font only; reject overflow."""
import io

from PIL import Image, ImageDraw, ImageFont

from app.config import get_settings


class RenderError(Exception):
    def __init__(self):
        super().__init__("Text does not fit a supported original template")


RENDER_VERSION = "geometry-v1"


def draw_text(draw, text, box, color, font_path):
    if any(not (32 <= ord(char) <= 126 or 160 <= ord(char) <= 255) for char in text):
        raise RenderError()
    left, top, right, bottom = box
    for size in range(78, 35, -2):
        try:
            font = ImageFont.truetype(font_path, size=size)
        except OSError:
            raise RenderError() from None
        lines = []
        line = ""
        fits = True
        for word in text.split():
            word_box = draw.textbbox((0, 0), word, font=font)
            if word_box[2] - word_box[0] > right - left:
                fits = False
                break
            trial = (line + " " + word).strip()
            bounds = draw.textbbox((0, 0), trial, font=font)
            if bounds[2] - bounds[0] > right - left:
                lines.append(line)
                line = word
            else:
                line = trial
        if not fits:
            continue
        lines.append(line)
        wrapped = "\n".join(lines)
        bounds = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=14, align="center")
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        if width <= right - left and height <= bottom - top:
            draw.multiline_text((left + (right - left - width) / 2 - bounds[0],
                                 top + (bottom - top - height) / 2 - bounds[1]),
                                wrapped, font=font, fill=color, spacing=14, align="center")
            return
    raise RenderError()


def render_meme(meme, *, font_path=None):
    font_path = font_path or get_settings().render_font_path
    if meme.template_type not in ("split_panel", "terminal", "geometric"):
        raise RenderError()
    image = Image.new("RGB", (1080, 1350), "#f2eddf")
    draw = ImageDraw.Draw(image)
    if meme.template_type == "terminal":
        draw.rectangle((0, 0, 1080, 1350), fill="#142823")
        draw.rounded_rectangle((56, 96, 1024, 1254), radius=32, outline="#91ddaa", width=4)
        for x, color in ((96, "#f87866"), (136, "#eec866"), (176, "#91ddaa")):
            draw.ellipse((x, 132, x + 22, 154), fill=color)
        draw.line((96, 676, 984, 676), fill="#91ddaa", width=4)
        foreground = "#ecf8ed"
    elif meme.template_type == "geometric":
        draw.ellipse((-180, -220, 520, 480), fill="#e89b66")
        draw.polygon(((720, 830), (1260, 1040), (940, 1510)), fill="#719995")
        draw.rounded_rectangle((56, 180, 1024, 1170), radius=40, fill="#f2eddf", outline="#273c45", width=4)
        draw.line((112, 676, 968, 676), fill="#273c45", width=5)
        foreground = "#182d36"
    else:
        draw.rectangle((0, 675, 1080, 1350), fill="#243b58")
        draw.rectangle((56, 94, 1024, 114), fill="#d45d3d")
        draw.rectangle((56, 1236, 1024, 1256), fill="#eabf64")
        foreground = "#182d36"
    draw_text(draw, meme.top_text, (100, 224, 980, 610), foreground, font_path)
    draw_text(draw, meme.bottom_text, (100, 744, 980, 1130),
              "#f9f3e8" if meme.template_type == "split_panel" else foreground, font_path)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=95, subsampling=0, optimize=False)
    return output.getvalue()
