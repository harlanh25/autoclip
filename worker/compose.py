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


def _find_silence_split(clip_path, window_secs=180, fallback=60.0, min_pos=0.0):
    try:
        r = subprocess.run(
            ['ffmpeg', '-i', clip_path, '-vn', '-t', str(window_secs),
             '-af', 'silencedetect=noise=-30dB:d=0.5', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=120,
        )
        ends = re.findall(r'silence_end: (\d+\.?\d*)', r.stderr or '')
        candidates = [float(t) for t in ends
                      if min_pos <= float(t) < window_secs - 10]
        if candidates:
            return candidates[-1]
    except Exception:
        pass
    return max(fallback, min_pos)


def compose_clip_with_ads(
    clip_path,
    intro_ad=None,
    mid_ad=None,
    outro_ad=None,
    mid_position_sec=None,
    extra_mid_ads=None,        # list of additional mid-roll ad paths
    extra_mid_positions=None,  # list of positions (None = auto), same order
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
    extra_mid_v = []
    for _i, _p in enumerate(extra_mid_ads or []):
        _v = _ensure_video(_p, f'mid{_i + 2}')
        if _v:
            extra_mid_v.append((_v, (extra_mid_positions or [None] * 99)[_i]
                                if _i < len(extra_mid_positions or []) else None))
    outro_v = _ensure_video(outro_ad, 'outro')

    if progress_callback:
        progress_callback(15)

    # ---- N mid-roll ads -------------------------------------------------
    # Each entry: [position_or_None, video_path]
    mid_specs = []
    if mid_v:
        mid_specs.append([mid_position_sec, mid_v])
    for _v, _pos in extra_mid_v:
        mid_specs.append([_pos, _v])

    MIN_EDGE = 3.0      # keep this much real content at each end
    MIN_GAP = 5.0       # minimum spacing between two mid ads
    MIN_AUTO_POS = 120.0   # never AUTO-place a mid ad before this point
    MIN_AUTO_TAIL = 30.0   # ...and leave at least this much clip after it
    AUTO_WINDOW = 420.0    # how far in to search for a silence

    if mid_specs:
        _dur_r = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', clip_path],
            capture_output=True, text=True, timeout=15,
        )
        try:
            _clip_dur = float(_dur_r.stdout.strip())
        except Exception:
            _clip_dur = 0.0
        log.info('Clip duration: %ss, %d mid ad(s) requested', _clip_dur, len(mid_specs))

        if _clip_dur < 10.0:
            log.warning('Clip too short (%ss) for mid ads; skipping all mids', _clip_dur)
            mid_specs = []
        else:
            if _clip_dur >= MIN_AUTO_POS + MIN_AUTO_TAIL:
                auto_lo = MIN_AUTO_POS
            else:
                auto_lo = None
                _n = sum(1 for sp in mid_specs if sp[0] is None)
                if _n:
                    log.warning('Clip %ss too short to auto-place a mid '
                                'after %ss; dropping %d auto mid(s)',
                                round(_clip_dur, 1), MIN_AUTO_POS, _n)
                    mid_specs = [sp for sp in mid_specs if sp[0] is not None]

            n_mid = len(mid_specs)
            for i, spec in enumerate(mid_specs):
                if spec[0] is None:
                    if n_mid == 1:
                        # single mid: keep the silence-detection behaviour
                        spec[0] = _find_silence_split(
                            clip_path,
                            window_secs=min(AUTO_WINDOW, _clip_dur - 3),
                            fallback=auto_lo, min_pos=auto_lo)
                        log.info('Auto mid at %ss (floor %ss)',
                                 round(spec[0], 2), MIN_AUTO_POS)
                    else:
                        # multiple: distribute evenly
                        _hi = _clip_dur - MIN_EDGE
                        spec[0] = auto_lo + (_hi - auto_lo) * (i + 1) / (n_mid + 1)
                spec[0] = float(spec[0])

            # order by position, then clamp and enforce spacing
            mid_specs.sort(key=lambda sp: sp[0])
            prev = MIN_EDGE
            kept = []
            for spec in mid_specs:
                pos = max(prev, min(spec[0], _clip_dur - MIN_EDGE))
                if pos >= _clip_dur - MIN_EDGE:
                    log.warning('Dropping mid ad at %ss - no room left in clip', spec[0])
                    continue
                spec[0] = pos
                kept.append(spec)
                prev = pos + MIN_GAP
            mid_specs = kept
            log.info('Final mid positions: %s', [round(sp[0], 2) for sp in mid_specs])

    if mid_specs:
        cut_points = [sp[0] for sp in mid_specs]
        boundaries = [0.0] + cut_points + [None]
        parts = []
        for i in range(len(boundaries) - 1):
            start, end = boundaries[i], boundaries[i + 1]
            part = os.path.join(work_dir, f'clip_part{i}.mp4')
            args = ['-i', clip_path]
            if start and start > 0:
                args += ['-ss', str(start)]
            if end is not None:
                args += ['-to', str(end)]
            args += [*NVENC_CV, *AAC_CA, *COMMON_OUT, part]
            _run_ffmpeg(args)
            parts.append(part)
            if progress_callback:
                progress_callback(20 + int(35 * (i + 1) / (len(boundaries) - 1)))

        # interleave: part0, ad0, part1, ad1, part2 ...
        clip_pieces = []
        for i, part in enumerate(parts):
            clip_pieces.append(part)
            if i < len(mid_specs):
                clip_pieces.append(mid_specs[i][1])
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
