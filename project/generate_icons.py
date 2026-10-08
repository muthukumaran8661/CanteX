"""
generate_icons.py
Run once to create PWA icon PNGs for Smart Canteen.
Usage: python generate_icons.py
"""
import struct
import zlib
import os

def create_png(size, output_path):
    width = height = size
    r, g, b = 200, 16, 46  # Smart Canteen brand red #C8102E

    raw_data = bytearray()
    cx, cy = width // 2, height // 2

    for y in range(height):
        raw_data.append(0)  # PNG filter: None
        for x in range(width):
            dx, dy = x - cx, y - cy
            dist = (dx * dx + dy * dy) ** 0.5
            radius = cx * 0.82  # rounded rect approximation

            # White dish/plate circle in centre
            if dist < cx * 0.42:
                raw_data.extend([255, 255, 255])
            # Red ring
            elif dist < cx * 0.58:
                raw_data.extend([r, g, b])
            # White inner ring
            elif dist < cx * 0.62:
                raw_data.extend([255, 255, 255])
            # Red background
            elif abs(dx) < cx - size * 0.06 and abs(dy) < cy - size * 0.06:
                raw_data.extend([r, g, b])
            # Corner rounding to transparent (use bg colour)
            else:
                raw_data.extend([r, g, b])

    compressed = zlib.compress(bytes(raw_data), 9)

    def png_chunk(name, data):
        body = name + data
        crc = zlib.crc32(body) & 0xFFFFFFFF
        return struct.pack('>I', len(data)) + body + struct.pack('>I', crc)

    png  = b'\x89PNG\r\n\x1a\n'
    png += png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    png += png_chunk(b'IDAT', compressed)
    png += png_chunk(b'IEND', b'')

    with open(output_path, 'wb') as f:
        f.write(png)
    print(f'[OK] {output_path}  ({len(png):,} bytes)')

if __name__ == '__main__':
    static = os.path.join(os.path.dirname(__file__), 'static')
    create_png(192, os.path.join(static, 'icon-192.png'))
    create_png(512, os.path.join(static, 'icon-512.png'))
    create_png(512, os.path.join(static, 'icon-maskable.png'))
    print('All Smart Canteen PWA icons generated.')
