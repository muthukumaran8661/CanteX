"""
update_app_icons.py
Generates crisp, high-resolution PWA icons (192x192, 512x512) for Smart Canteen / CanteX.
"""
from PIL import Image, ImageDraw, ImageFont
import os
import math

def render_logo(size):
    # Create image with super-sampling (2x) for ultra smooth edges
    scale = 2
    dim = size * scale
    img = Image.new('RGBA', (dim, dim), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Gradient background rounded rectangle
    # Draw vertical/diagonal gradient
    c1 = (200, 16, 46)     # Brand Red #C8102E
    c2 = (225, 29, 72)     # Crimson #E11D48
    c3 = (255, 87, 34)     # Orange #FF5722

    radius = int(dim * 0.22)
    # Create background mask with rounded rectangle
    mask = Image.new('L', (dim, dim), 0)
    mask_draw = ImageDraw.Draw(mask)
    margin = int(dim * 0.02)
    mask_draw.rounded_rectangle([margin, margin, dim - margin, dim - margin], radius=radius, fill=255)

    # Render gradient
    grad = Image.new('RGBA', (dim, dim))
    grad_draw = ImageDraw.Draw(grad)
    for y in range(dim):
        ratio = y / dim
        if ratio < 0.5:
            t = ratio * 2
            r = int(c1[0] * (1 - t) + c2[0] * t)
            g = int(c1[1] * (1 - t) + c2[1] * t)
            b = int(c1[2] * (1 - t) + c2[2] * t)
        else:
            t = (ratio - 0.5) * 2
            r = int(c2[0] * (1 - t) + c3[0] * t)
            g = int(c2[1] * (1 - t) + c3[1] * t)
            b = int(c2[2] * (1 - t) + c3[2] * t)
        grad_draw.line([(0, y), (dim, y)], fill=(r, g, b, 255))

    img.paste(grad, (0, 0), mask)
    draw = ImageDraw.Draw(img)

    # Gold / White Cloche & Plate
    gold = (255, 179, 0, 255)
    white = (255, 255, 255, 255)
    gold_light = (255, 224, 130, 255)

    cx = dim / 2
    cy = dim * 0.52

    # Steam waves above cloche
    w_scale = dim / 100.0
    for offset_x in [-10, 0, 10]:
        sx = cx + offset_x * w_scale
        sy = cy - 22 * w_scale
        color = white if offset_x == 0 else gold_light
        draw.arc([sx - 4*w_scale, sy - 10*w_scale, sx + 4*w_scale, sy + 6*w_scale],
                 start=140, end=320, fill=color, width=max(2, int(3 * w_scale)))

    # Cloche Handle
    handle_r = int(4.5 * w_scale)
    handle_cy = int(cy - 14 * w_scale)
    draw.ellipse([cx - handle_r, handle_cy - handle_r, cx + handle_r, handle_cy + handle_r], fill=gold)

    # Cloche Dome
    dome_box = [cx - 28 * w_scale, cy - 10 * w_scale, cx + 28 * w_scale, cy + 24 * w_scale]
    draw.pieslice(dome_box, start=180, end=360, fill=white)

    # Gold Rim
    rim_h = max(2, int(4.5 * w_scale))
    draw.rounded_rectangle([cx - 32 * w_scale, cy + 8 * w_scale, cx + 32 * w_scale, cy + 8 * w_scale + rim_h],
                           radius=int(2 * w_scale), fill=gold)

    # Platter base
    plat_w = 36 * w_scale
    plat_y = cy + 14 * w_scale
    plat_h = max(2, int(4 * w_scale))
    draw.rounded_rectangle([cx - plat_w, plat_y, cx + plat_w, plat_y + plat_h],
                           radius=int(2 * w_scale), fill=white)

    # Modern 4-point star sparkle
    sp_x = cx + 27 * w_scale
    sp_y = cy - 22 * w_scale
    sr = int(6 * w_scale)
    draw.polygon([
        (sp_x, sp_y - sr), (sp_x + sr//3, sp_y - sr//3),
        (sp_x + sr, sp_y), (sp_x + sr//3, sp_y + sr//3),
        (sp_x, sp_y + sr), (sp_x - sr//3, sp_y + sr//3),
        (sp_x - sr, sp_y), (sp_x - sr//3, sp_y - sr//3)
    ], fill=gold_light)

    # Resize down with high-quality Lanczos resampling
    final_img = img.resize((size, size), Image.Resampling.LANCZOS)
    return final_img

if __name__ == '__main__':
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    icon192 = render_logo(192)
    icon192.save(os.path.join(static_dir, 'icon-192.png'))
    icon512 = render_logo(512)
    icon512.save(os.path.join(static_dir, 'icon-512.png'))
    icon512.save(os.path.join(static_dir, 'icon-maskable.png'))
    print("Icons generated successfully!")
