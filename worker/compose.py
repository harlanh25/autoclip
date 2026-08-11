"""ffmpeg composition with NVENC (GPU h.264). Same pipeline as Phase 3b."""
import os
import subprocess
import re
import logging

log = logging.getLogger('compose')

NVENC_CV = ['-c:v', 'h264_nvenc', '-preset', 'p5', '-rc', 'vbr',
            '-cq', '23', '-b:v', '5M', '-maxrate', '8M', '-bufsize', '10M']
AAC_CA = ['-c:a', 'aac', '-b:a', '192k']
COMMON_OUT = ['-pix_fmt', 'yuv420p', '-y']

TARGET_W = 1920
TARGET_H = 1080
TARGET_FPS = 30


def _run_ffmpeg(args, timeout=1800):
    log.info("ffmpeg %s ...", ' '.join(args[:6]))
    r = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error'] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg failed (exit {r.returncode}): {r.stderr[-1000:]}')
    return r


def _probe_has_video(path):
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip() == 'video'
    except Exception:
        return False


def _wrap_audio_as_video(audio_path, out_path):
    _run_ffmpeg([
        '-f', 'lavfi', '-i', f'color=c=black:s={TARGET_W}x{TARGET_H}:r={TARGET_FPS}',
        '-i', audio_path,
        '-map', '0:v', '-map', '1:a',
        *NVENC_CV, *AAC_CA, *COMMON_OUT,
        '-shortest', out_path,
    ], timeout=300)


def _find_silence_split(clip_path, window_secs=180, fallback=60.0):
    try:
        r = subprocess.run(
            ['ffmpeg', '-i', clip_path, '-t', str(window_secs),
             '-af', 'silencedetect=noise=-30dB:d=0.5', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=120,
        )
        ends = re.findall(r'silence_end: (\d+\.?\d*)', r.stderr or '')
        candidates = [float(t) for t in ends if float(t) < window_secs - 10]
        if candidates:
            return candidates[-1]
    except Exception:
        pass
    return fallback


def compose_clip_with_ads(
    clip_path,
    intro_ad=None,
    mid_ad=None,
    outro_ad=None,
    mid_position_sec=None,
    output_path='composed.mp4',
    progress_callback=None,
):
    if not os.path.exists(clip_path):
        raise RuntimeError(f'clip not found: {clip_path}')

    work_dir = os.path.dirname(os.path.abspath(output_path))

    def _ensure_video(ad_path, tag):
        if not ad_path:
            return None
        if _probe_has_video(ad_path):
            return ad_path
        v = os.path.join(work_dir, f'{tag}_video.mp4')
        _wrap_audio_as_video(ad_path, v)
        return v

    if progress_callback:
        progress_callback(5)

    intro_v = _ensure_video(intro_ad, 'intro')
    mid_v = _ensure_video(mid_ad, 'mid')
    outro_v = _ensure_video(outro_ad, 'outro')

    if progress_callback:
        progress_callback(15)

    if mid_v:
        # Probe clip duration and validate mid_position_sec
        _dur_r = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', clip_path],
            capture_output=True, text=True, timeout=15,
        )
        try:
            _clip_dur = float(_dur_r.stdout.strip())
        except Exception:
            _clip_dur = 0.0
        log.info(f'Clip duration: {_clip_dur}s, mid_position_sec: {mid_position_sec}')
        # If clip is too short for a mid split, skip mid ad entirely
        if _clip_dur < 10.0:
            log.warning(f'Clip too short ({_clip_dur}s) for mid ad; skipping mid')
            mid_v = None
        elif mid_position_sec is None:
            mid_position_sec = _find_silence_split(clip_path, window_secs=min(180, _clip_dur - 3))
        # Clamp so both parts have real content
        if mid_v is not None:
            mid_position_sec = max(3.0, min(mid_position_sec, _clip_dur - 3.0))
            log.info(f'Clamped mid_position_sec: {mid_position_sec}')

    if mid_v:
        part1 = os.path.join(work_dir, 'clip_part1.mp4')
        part2 = os.path.join(work_dir, 'clip_part2.mp4')
        _run_ffmpeg([
            '-i', clip_path, '-t', str(mid_position_sec),
            *NVENC_CV, *AAC_CA, *COMMON_OUT, part1,
        ])
        if progress_callback:
            progress_callback(35)
        _run_ffmpeg([
            '-i', clip_path, '-ss', str(mid_position_sec),
            *NVENC_CV, *AAC_CA, *COMMON_OUT, part2,
        ])
        if progress_callback:
            progress_callback(55)
        clip_pieces = [part1, mid_v, part2]
    else:
        clip_pieces = [clip_path]
        if progress_callback:
            progress_callback(40)

    ordered = []
    if intro_v:
        ordered.append(intro_v)
    ordered.extend(clip_pieces)
    if outro_v:
        ordered.append(outro_v)

    if len(ordered) <= 1:
        raise RuntimeError('nothing to compose (no ads set)')

    if progress_callback:
        progress_callback(65)

    input_args = []
    for p in ordered:
        input_args += ['-i', p]
    n = len(ordered)
    stream_pairs = ''.join(f'[{i}:v:0][{i}:a:0]' for i in range(n))
    filter_str = f'{stream_pairs}concat=n={n}:v=1:a=1[outv][outa]'

    _run_ffmpeg([
        *input_args,
        '-filter_complex', filter_str,
        '-map', '[outv]', '-map', '[outa]',
        *NVENC_CV, *AAC_CA, *COMMON_OUT,
        output_path,
    ])

    if progress_callback:
        progress_callback(100)

    return output_path
