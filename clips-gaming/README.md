# Clips gaming

Convierte enlaces de directos (Kick, Twitch…) en recopilaciones horizontales o shorts verticales
sin descargar el vídeo entero en calidad: analiza solo el audio, revisa los candidatos en 360p y
baja en 1080p únicamente los momentos elegidos.

Necesita Python 3.10+, ffmpeg y curl.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python clips.py analizar URL
.venv/bin/python clips.py revisar ID
.venv/bin/python clips.py montar --duracion 600 ID
```

Detalles de cada orden: `clips.py --help`.
Nota: YouTube suele bloquear las descargas desde servidores en la nube.
