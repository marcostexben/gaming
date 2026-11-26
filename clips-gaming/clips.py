#!/usr/bin/env python3
"""Clips gaming: de enlaces (Twitch, Kick…) a recopilaciones y shorts sin descargar los vídeos enteros en calidad.

    clips.py analizar URL [URL ...] [--top 60] [--antes 14] [--despues 6]
        Descarga solo el audio (o la versión más ligera, si no hay audio suelto), mide el volumen cada medio
        segundo y busca picos de emoción (gritos, reacciones) frente al nivel normal de ese minuto.

    clips.py revisar ID [--top 40]
        Descarga en 360p SOLO los tramos candidatos, mide la acción en pantalla y genera una hoja de
        fotogramas por candidato (trabajo/<id>/hojas/) y hojas resumen (trabajo/<id>/resumen-*.jpg).

    clips.py montar --duracion SEG [--formato horizontal|vertical] [--max-clips N] ID[:c1,c2,...] [ID ...] [-o nombre]
        Descarga en 1080p solo los momentos elegidos y monta el vídeo con fundidos y volumen a -14 LUFS.
        Sin lista de candidatos elige los mejores (voz + acción); el mejor va primero y el resto en orden.

    clips.py lista
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import numpy as np

# ffmpeg solo lee http_proxy: si el sistema usa un proxy HTTPS (como este entorno en la nube), se lo pasamos.
if os.environ.get("https_proxy") and not os.environ.get("http_proxy"):
    os.environ["http_proxy"] = os.environ["https_proxy"]

RAIZ = Path(__file__).resolve().parent
TRABAJO = RAIZ / "trabajo"
SALIDA = RAIZ / "salida"
YTDLP = str(RAIZ / ".venv" / "bin" / "yt-dlp")
VENTANA = 0.5  # segundos por medida de volumen
SR = 8000
PARALELO = 8


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def fmt(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


# ---------- fuentes y segmentos ----------

def ytdlp_info(url):
    out = subprocess.run([YTDLP, "-J", "--no-warnings", url], capture_output=True, text=True)
    if out.returncode != 0:
        err = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "error desconocido"
        if "not a bot" in err or "Sign in" in err:
            err += "\n  → YouTube bloquea descargas desde servidores en la nube. Usa Twitch/Kick o sube el vídeo a otro sitio."
        sys.exit(f"✗ No puedo leer {url}\n  {err}")
    return json.loads(out.stdout)


def slug(info):
    return re.sub(r"[^a-z0-9_-]+", "-", f"{info.get('extractor_key', 'web')}-{info.get('id', 'x')}".lower())


def elegir_formato(info, uso):
    fs = [f for f in info.get("formats", []) if f.get("url") and f.get("protocol", "").startswith("m3u8")]
    if not fs:
        fs = [f for f in info.get("formats", []) if f.get("url")]
    if uso == "audio":
        solo = [f for f in fs if f.get("vcodec") == "none" and f.get("acodec") != "none"]
        if solo:
            return solo[-1]
        conaudio = [f for f in fs if f.get("acodec") != "none" and f.get("height")]
        return min(conaudio or fs, key=lambda f: f.get("tbr") or 1e9)
    tope = 360 if uso == "baja" else 1080
    vid = [f for f in fs if f.get("height") and f["height"] <= tope and f.get("acodec") != "none"]
    return max(vid or fs, key=lambda f: ((f.get("height") or 0), (f.get("fps") or 0), (f.get("tbr") or 0)))


def segmentos(m3u8):
    txt = subprocess.run(["curl", "-sfL", "--retry", "3", m3u8], capture_output=True, text=True, check=True).stdout
    if "#EXT-X-STREAM-INF" in txt:  # lista maestra: coger la primera variante
        var = [l for l in txt.splitlines() if l and not l.startswith("#")][0]
        return segmentos(urljoin(m3u8, var))
    init, segs, t, dur = None, [], 0.0, None
    for l in txt.splitlines():
        if l.startswith("#EXT-X-MAP"):
            init = urljoin(m3u8, re.search(r'URI="([^"]+)"', l).group(1))
        elif l.startswith("#EXTINF"):
            dur = float(l.split(":")[1].split(",")[0])
        elif l and not l.startswith("#") and dur is not None:
            segs.append((t, dur, urljoin(m3u8, l)))
            t += dur
            dur = None
    return init, segs


def bajar(urls, destino):
    """Descarga varias URLs en paralelo y las une en un solo archivo (init + segmentos)."""
    with tempfile.TemporaryDirectory(dir=TRABAJO) as tmp:
        cfg = Path(tmp) / "curl.cfg"
        cfg.write_text("".join(f'url = "{u}"\noutput = "{tmp}/{i:06d}"\n' for i, u in enumerate(urls)))
        run(["curl", "-sfL", "--retry", "4", "--retry-delay", "2", "--parallel", "--parallel-max", str(PARALELO), "-K", str(cfg)])
        with open(destino, "wb") as out:
            for i in range(len(urls)):
                with open(f"{tmp}/{i:06d}", "rb") as f:
                    shutil.copyfileobj(f, out)
    return destino


class Fuente:
    def __init__(self, sid):
        self.d = TRABAJO / sid
        self.meta = json.loads((self.d / "info.json").read_text())
        self._info, self._listas = None, {}

    def lista(self, uso):
        if uso not in self._listas:
            if self._info is None:
                self._info = ytdlp_info(self.meta["url"])
            self._listas[uso] = segmentos(elegir_formato(self._info, uso)["url"])
        return self._listas[uso]

    def tramo(self, ini, fin, uso, destino):
        """Baja solo los segmentos que cubren [ini, fin]. Devuelve (archivo, desplazamiento, duración)."""
        init, segs = self.lista(uso)
        dentro = [s for s in segs if s[0] + s[1] > ini and s[0] < fin]
        off = ini - dentro[0][0]
        ext = ".mp4" if init else ".ts"
        destino = destino.with_suffix(ext)
        if not destino.exists():
            bajar(([init] if init else []) + [s[2] for s in dentro], destino)
        return destino, off, fin - ini


# ---------- 1. analizar ----------

def volumen(ruta):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(ruta), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         stdout=subprocess.PIPE)
    n = int(SR * VENTANA)
    vals, resto = [], np.zeros(0, dtype=np.float32)
    while True:
        b = p.stdout.read(n * 2 * 2000)
        if not b:
            break
        x = np.concatenate([resto, np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768])
        k = len(x) // n
        vals.append(10 * np.log10(np.mean(x[: k * n].reshape(k, n) ** 2, axis=1) + 1e-10))
        resto = x[k * n:]
    p.wait()
    return np.concatenate(vals) if vals else np.zeros(0)


def candidatos(db, top, antes, despues, separacion=35.0):
    if len(db) < 10:
        return []
    w = int(60 / VENTANA)
    pad = np.pad(db, (w // 2, w - w // 2 - 1), mode="edge")
    base = np.median(np.lib.stride_tricks.sliding_window_view(pad, w), axis=1)
    exc = db - base
    k = int(2 / VENTANA)
    suave = np.convolve(exc, np.ones(k) / k, mode="same")
    nota = 0.6 * suave + 0.4 * exc
    nota[db < np.percentile(db, 30)] = -99
    elegidos, dur = [], len(db) * VENTANA
    for i in np.argsort(-nota):
        if len(elegidos) >= top or nota[i] < 3:
            break
        t = i * VENTANA
        if all(abs(t - c["pico"]) > separacion for c in elegidos):
            elegidos.append({"pico": round(t, 1), "ini": round(max(0, t - antes), 1),
                             "fin": round(min(dur, t + despues), 1), "voz": round(float(nota[i]), 1)})
    for n, c in enumerate(elegidos, 1):
        c["n"] = n
    return elegidos


def cmd_analizar(a):
    for url in a.urls:
        info = ytdlp_info(url)
        sid = slug(info)
        d = TRABAJO / sid
        d.mkdir(parents=True, exist_ok=True)
        print(f"▶ {info.get('title')}  ({fmt(info.get('duration') or 0)})  → {sid}", flush=True)
        (d / "info.json").write_text(json.dumps({"url": url, "titulo": info.get("title"), "duracion": info.get("duration"),
                                                 "candidatos": []}, indent=1, ensure_ascii=False))
        audio = d / "audio.m4a"
        if not audio.exists():
            f = elegir_formato(info, "audio")
            init, segs = segmentos(f["url"])
            print(f"  Bajando audio ({f.get('format_id')}, {len(segs)} segmentos)…", flush=True)
            crudo = bajar(([init] if init else []) + [s[2] for s in segs], d / ("crudo.mp4" if init else "crudo.ts"))
            run(["ffmpeg", "-v", "error", "-y", "-i", str(crudo), "-vn", "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(audio)])
            crudo.unlink()
        db = volumen(audio)
        np.save(d / "volumen.npy", db)
        meta = json.loads((d / "info.json").read_text())
        meta["candidatos"] = candidatos(db, a.top, a.antes, a.despues)
        (d / "info.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
        print(f"  {len(meta['candidatos'])} momentos candidatos (los 15 primeros):")
        for c in meta["candidatos"][:15]:
            print(f"   #{c['n']:2d}  {fmt(c['ini'])}–{fmt(c['fin'])}  emoción {c['voz']:+.1f} dB")


# ---------- 2. revisar ----------

def accion(ruta, off, dur):
    out = subprocess.run(["ffmpeg", "-i", str(ruta), "-vf",
                          f"trim=start={off:.2f}:duration={dur:.2f},setpts=PTS-STARTPTS,"
                          "scale=160:-2,fps=5,select='gte(scene,0)',metadata=print", "-an", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    v = [float(x) for x in re.findall(r"lavfi\.scene_score=([\d.]+)", out)]
    return round(float(np.mean(v)) * 100, 1) if v else 0.0


def hoja(ruta, off, dur, destino, titulo):
    titulo = titulo.replace(":", "\\:")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(ruta), "-vf",
         f"trim=start={off:.2f}:duration={dur:.2f},setpts=PTS-STARTPTS,fps=8/{dur:.2f},scale=480:-2,tile=4x2:padding=6:color=black,"
         f"drawtext=text='{titulo}':x=10:y=10:fontsize=40:fontcolor=yellow:box=1:boxcolor=black@0.7",
         "-frames:v", "1", str(destino)])


def cmd_revisar(a):
    fu = Fuente(a.id)
    (fu.d / "baja").mkdir(exist_ok=True)
    (fu.d / "hojas").mkdir(exist_ok=True)
    hechas = []
    for c in fu.meta["candidatos"][: a.top]:
        r, off, dur = fu.tramo(c["ini"], c["fin"], "baja", fu.d / "baja" / f"c{c['n']:02d}")
        c["accion"] = accion(r, off, dur)
        h = fu.d / "hojas" / f"c{c['n']:02d}.jpg"
        hoja(r, off, dur, h, f"#{c['n']} {fmt(c['ini'])}")
        hechas.append(h)
        print(f"   #{c['n']:2d}  {fmt(c['ini'])}  emoción {c['voz']:+.1f} dB  acción {c['accion']}", flush=True)
    (fu.d / "info.json").write_text(json.dumps(fu.meta, indent=1, ensure_ascii=False))
    # hojas resumen: 6 candidatos por imagen, uno debajo de otro
    for k in range(0, len(hechas), 6):
        grupo = hechas[k:k + 6]
        ins = sum([["-i", str(h)] for h in grupo], [])
        run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex",
             "".join(f"[{i}:v]scale=1000:-2[v{i}];" for i in range(len(grupo))) +
             "".join(f"[v{i}]" for i in range(len(grupo))) + f"vstack=inputs={len(grupo)}" if len(grupo) > 1 else "[0:v]scale=1000:-2",
             str(fu.d / f"resumen-{k // 6 + 1}.jpg")])
    print(f"Hojas en {fu.d / 'hojas'} y resúmenes en {fu.d}")


# ---------- 3. montar ----------

def nota_total(c):
    return c["voz"] + 0.5 * c.get("accion", 0)


def preparar(src, off, dur, dst, formato, fundido=0.3):
    fo = max(0.0, dur - fundido)
    if formato == "vertical":
        vf = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:3,"
              "eq=brightness=-0.15[fondo];[b]scale=1080:-2[juego];[fondo][juego]overlay=0:(H-h)/2,fps=30")
    else:
        vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=60"
    vf = f"trim=start={off:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS," + vf
    vf += f",format=yuv420p,fade=t=in:d={fundido},fade=t=out:st={fo:.2f}:d={fundido}"
    af = (f"atrim=start={off:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
          f"afade=t=in:d={fundido},afade=t=out:st={fo:.2f}:d={fundido}")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(src),
         "-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]", "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-c:a", "aac", "-b:a", "192k", str(dst)])


def cmd_montar(a):
    explicita = any(":" in f for f in a.fuentes)
    fuentes, elegidos = {}, []
    for arg in a.fuentes:
        sid, _, lst = arg.partition(":")
        fuentes[sid] = Fuente(sid)
        cands = fuentes[sid].meta["candidatos"]
        if lst:
            cands = [c for n in map(int, lst.split(",")) for c in cands if c["n"] == n]
        elegidos += [(sid, c) for c in cands]
    if not explicita:
        elegidos.sort(key=lambda e: -nota_total(e[1]))
    sel, total = [], 0.0
    for e in elegidos:
        if total >= a.duracion or (a.max_clips and len(sel) >= a.max_clips):
            break
        sel.append(e)
        total += e[1]["fin"] - e[1]["ini"]
    if not sel:
        sys.exit("No hay momentos que montar.")
    if not explicita:
        sel = [sel[0]] + sorted(sel[1:], key=lambda e: (e[0], e[1]["ini"]))
    tmp = TRABAJO / "_montaje"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    partes = []
    for i, (sid, c) in enumerate(sel):
        fu = fuentes[sid]
        src, off, dur = fu.tramo(c["ini"], c["fin"], "alta", fu.d / f"alta-c{c['n']:02d}")
        listo = tmp / f"{i:03d}.mp4"
        preparar(src, off, dur, listo, a.formato)
        partes.append(listo)
        print(f"  ✓ {i + 1}/{len(sel)}  #{c['n']} {fmt(c['ini'])}", flush=True)
    (tmp / "lista.txt").write_text("".join(f"file '{p}'\n" for p in partes))
    SALIDA.mkdir(exist_ok=True)
    destino = SALIDA / f"{a.o or a.formato + '-' + str(len(partes)) + 'clips'}.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp / "lista.txt"),
         "-c:v", "copy", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", str(destino)])
    (destino.with_suffix(".txt")).write_text("".join(
        f"{fmt(sum(sel[j][1]['fin'] - sel[j][1]['ini'] for j in range(i)))}  {sid} #{c['n']} (original {fmt(c['ini'])})\n"
        for i, (sid, c) in enumerate(sel)))
    print(f"✓ {destino}  ({total:.0f} s, {len(partes)} clips)")


def cmd_lista(a):
    for d in sorted(TRABAJO.glob("*/info.json")):
        m = json.loads(d.read_text())
        print(f"{d.parent.name}: {m['titulo']} ({fmt(m['duracion'] or 0)}), {len(m['candidatos'])} candidatos")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("analizar"); x.add_argument("urls", nargs="+"); x.add_argument("--top", type=int, default=60)
    x.add_argument("--antes", type=float, default=14); x.add_argument("--despues", type=float, default=6)
    x.set_defaults(f=cmd_analizar)
    x = s.add_parser("revisar"); x.add_argument("id"); x.add_argument("--top", type=int, default=40)
    x.set_defaults(f=cmd_revisar)
    x = s.add_parser("montar"); x.add_argument("fuentes", nargs="+"); x.add_argument("--duracion", type=float, required=True)
    x.add_argument("--formato", choices=["horizontal", "vertical"], default="horizontal")
    x.add_argument("--max-clips", type=int, default=0); x.add_argument("-o")
    x.set_defaults(f=cmd_montar)
    x = s.add_parser("lista"); x.set_defaults(f=cmd_lista)
    a = p.parse_args()
    TRABAJO.mkdir(exist_ok=True)
    a.f(a)


if __name__ == "__main__":
    main()
