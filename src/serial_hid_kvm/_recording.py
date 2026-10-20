"""Validated background MP4 export. Originals are never removed."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def _run(args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=43200, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or "Media tool failed")
    return result.stdout


def _timeline(probe, path):
    # Streaming scan: browser WebM often has no duration header. Do not load
    # millions of packets into memory for long recordings.
    count, first, last, last_duration = 0, math.inf, -math.inf, 0.0
    with tempfile.TemporaryFile(mode="w+b") as errors:
        proc = subprocess.Popen(
            [probe, "-v", "error", "-select_streams", "v:0", "-show_packets",
             "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            for line in proc.stdout:
                parts = line.strip().split(",")
                try:
                    pts = float(parts[0])
                    duration = float(parts[1]) if len(parts) > 1 and parts[1] != "N/A" else 0
                except ValueError:
                    continue
                if not math.isfinite(pts) or not math.isfinite(duration):
                    continue
                count += 1
                first = min(first, pts)
                # H.264 B-frames arrive in decode order. A packet's DTS-based
                # interval can span an earlier capture gap and must not extend
                # the timeline past later presentation frames. Only the final
                # presentation frame contributes the trailing duration.
                if pts >= last:
                    last, last_duration = pts, duration
            code = proc.wait(timeout=60)
            if code or not count:
                raise RuntimeError("No readable video packets in recording")
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    return count, last + last_duration - first


def convert_recording(source, destination=None):
    """Return verified MP4 path; failures preserve WebM and any existing MP4."""
    source = Path(source)
    ffmpeg, probe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not probe:
        raise RuntimeError("FFmpeg/ffprobe unavailable; WebM was kept")
    destination = Path(destination) if destination is not None else source.with_suffix(".mp4")
    if destination.exists():
        raise RuntimeError("MP4 already exists; refusing to overwrite it")
    temporary = destination.with_name("." + destination.stem + "." + uuid.uuid4().hex + ".partial.mp4")
    log = source.with_suffix(".conversion.log")
    try:
        source_count, source_duration = _timeline(probe, source)
        source_info = json.loads(_run([probe, "-v", "error", "-show_streams", "-of", "json", str(source)]))
        audio_count = sum(s["codec_type"] == "audio" for s in source_info["streams"])
        video_codecs = [s["codec_name"] for s in source_info["streams"] if s["codec_type"] == "video"]
        audio_codecs = [s["codec_name"] for s in source_info["streams"] if s["codec_type"] == "audio"]
        if video_codecs[:1] == ["h264"] and all(c == "aac" for c in audio_codecs):
            # Server recordings are already H.264/AAC: remux only (seconds, no quality loss).
            codec_args = ["-c", "copy"]
        elif video_codecs[:1] == ["h264"]:
            # Browser H.264 WebM (Opus audio): keep the video, convert only the audio.
            codec_args = ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]
        else:
            codec_args = ["-c:v", "libx264", "-threads", "2",
                          "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                          "-crf", "23", "-preset", "veryfast", "-fps_mode:v", "passthrough",
                          "-enc_time_base:v", "1:1000", "-c:a", "aac", "-b:a", "128k"]
        _run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(source),
              "-map", "0:v:0", "-map", "0:a?", *codec_args,
              "-movflags", "+faststart", "-n", str(temporary)])
        count, duration = _timeline(probe, temporary)
        info = json.loads(_run([probe, "-v", "error", "-show_streams", "-of", "json", str(temporary)]))
        output_audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
        if count != source_count or abs(duration - source_duration) > 0.15:
            raise RuntimeError("Frame count/duration verification failed")
        if len(output_audio) != audio_count or any(s["codec_name"] != "aac" for s in output_audio):
            raise RuntimeError("Audio track verification failed")
        _run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror",
              "-i", str(temporary), "-map", "0:v:0", "-map", "0:a?",
              "-fps_mode:v", "passthrough", "-enc_time_base:v", "1:1000", "-f", "null", "-"])
        # Hard-link publication is atomic and fails if a destination appeared
        # during conversion; never replace another session's exported video.
        os.link(temporary, destination)
        log.write_text(f"OK: {count} video packets, {duration:.3f}s, {audio_count} audio track(s)\n"
                       f"MP4: {destination}\nSource retained: {source}\n", encoding="utf-8")
        return destination
    except Exception as exc:
        log.write_text(f"FAILED: {exc}\nSource retained: {source}\n", encoding="utf-8")
        raise
    finally:
        temporary.unlink(missing_ok=True)
