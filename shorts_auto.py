#!/usr/bin/env python3
"""
Zion Skank Shorts Automation Pipeline

Takes a long-form music video and outputs a ready-to-post YouTube Short.

Usage:
    python shorts_auto.py --input "my_video.mp4" --title "Original Video Title"
"""

import argparse
import os
import subprocess
import tempfile

import numpy as np
import librosa
from PIL import Image, ImageDraw, ImageFont

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


SEGMENT_DURATION = 15   # seconds — 10-15s range; 15s gives more loop breathing room
FADE_DURATION    = 1.0  # seconds of fade in/out at each end for smooth looping
OUTPUT_WIDTH     = 1080
OUTPUT_HEIGHT    = 1920

ZOOM_MAX         = 0.06   # yoyo_zoom: max zoom at clip midpoint (100% → 106% → 100%)
PULSE_ZOOM       = 0.03   # pulse: max zoom per oscillation cycle (100% → 103% → 100%)
PULSE_PERIOD     = 2.5    # pulse / vignette_pulse: cycle length in seconds
TILT_MAX_DEG     = 1.5    # tilt_rock: peak rotation in degrees
# tilt_rock pre-scales the frame before rotating so the crop never sees a black corner.
# At ±1.5° on 1080×1920, a 5% overshoot inscribes ≥ 1080×1920 after rotation.
TILT_OVERSHOOT_W = (int(OUTPUT_WIDTH  * 1.05) // 2) * 2   # 1134
TILT_OVERSHOOT_H = (int(OUTPUT_HEIGHT * 1.05) // 2) * 2   # 2016

# Preset combinations. --preset N sets all four flags at once.
# Preset 1 is identical to Milestone 1 behaviour (all defaults).
VARIATION_PRESETS = {
    1: {"variation": 1, "style": "classic", "motion": "static",         "meta_style": "standard"},
    2: {"variation": 2, "style": "gold",    "motion": "pulse",          "meta_style": "alternate"},
    3: {"variation": 1, "style": "green",   "motion": "tilt_rock",      "meta_style": "standard"},
    4: {"variation": 3, "style": "red",     "motion": "yoyo_zoom",      "meta_style": "alternate"},
    5: {"variation": 2, "style": "minimal", "motion": "vignette_pulse", "meta_style": "standard"},
}

CLIENT_SECRETS_FILE    = "client_secrets.json"
TOKEN_FILE             = "token.json"
TARGET_CHANNEL_ID      = "UCvqPwuW8ZctLPn7J1KfjBGw"  # Zion Skank Shorts
SCOPES = ["https://www.googleapis.com/auth/youtube"]

OVERLAY_TEXT = "Zion Skank"

# Text overlay style presets.
# color: RGBA fill tuple. stroke_width/stroke_fill: outline for legibility.
# size: font size in points.
# anchor: "center-top" places text centred at y=220 (top blurred band);
#         "top-right" places it in the upper-right corner with 40px padding.
OVERLAY_STYLES = {
    "classic": {"color": (255, 255, 255, 255), "stroke": (0,   0,   0, 180), "size": 96, "anchor": "center-top"},
    "gold":    {"color": (212, 175,  55, 255), "stroke": (0,   0,   0, 180), "size": 96, "anchor": "center-top"},
    "green":   {"color": (  0, 170,   0, 255), "stroke": (0,   0,   0, 180), "size": 96, "anchor": "center-top"},
    "red":     {"color": (204,  34,   0, 255), "stroke": (0,   0,   0, 180), "size": 96, "anchor": "center-top"},
    "minimal": {"color": (255, 255, 255, 255), "stroke": (0,   0,   0, 180), "size": 48, "anchor": "top-right"},
}

# Fonts to try in order — first one found on disk wins.
# Prefer plain .ttf files; .ttc (font collections) can cause issues with drawtext.
# macOS Sequoia keeps many supplemental fonts in the Supplemental subfolder.
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
]


def find_font():
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def create_text_overlay_png(output_path, style="classic"):
    """
    Render OVERLAY_TEXT onto a fully-transparent 1080x1920 RGBA PNG using
    Pillow. The result is composited in ffmpeg using the overlay filter —
    no libfreetype needed.

    style must be a key in OVERLAY_STYLES.
    """
    cfg = OVERLAY_STYLES[style]

    img = Image.new("RGBA", (OUTPUT_WIDTH, OUTPUT_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_path = find_font()
    try:
        font = ImageFont.truetype(font_path, cfg["size"]) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), OVERLAY_TEXT, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    if cfg["anchor"] == "center-top":
        x = (OUTPUT_WIDTH - text_w) // 2
        y = 220 - text_h // 2
    else:  # top-right
        padding = 40
        x = OUTPUT_WIDTH - text_w - padding
        y = padding

    draw.text(
        (x, y),
        OVERLAY_TEXT,
        font=font,
        fill=cfg["color"],
        stroke_width=3,
        stroke_fill=cfg["stroke"],
    )

    img.save(output_path, "PNG")


# ---------------------------------------------------------------------------
# Stage 1: Audio analysis and clip extraction
# ---------------------------------------------------------------------------

def extract_audio_to_temp(video_path):
    """
    Use ffmpeg to pull the audio track out of the video into a temporary
    WAV file. Returns the path to that file.

    We do this as a separate step because it's more reliable than asking
    librosa to decode video files directly.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    tmp.close()

    print("  Extracting audio track...")
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vn",                  # no video
            "-acodec", "pcm_s16le", # uncompressed WAV
            "-ar", "22050",         # sample rate (matches librosa default)
            "-ac", "1",             # mono
            tmp_path,
        ],
        check=True,
        capture_output=True,
    )
    return tmp_path


def find_best_segment(video_path, n=3):
    """
    Analyze the audio and return the top n non-overlapping 15-second windows
    ranked by average RMS energy, as [(start_time, energy_score), ...] ordered
    by energy descending.

    Two segments are non-overlapping if the end of one falls at or before the
    start of the next — i.e. their start times differ by at least SEGMENT_DURATION.
    After each pick, all candidate frames within segment_frames of that pick are
    masked so no future selection can produce an overlapping segment.
    """
    tmp_path = extract_audio_to_temp(video_path)

    try:
        print("  Analyzing energy levels...")
        y, sr = librosa.load(tmp_path, sr=None, mono=True)
    finally:
        os.unlink(tmp_path)

    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]

    frames_per_second = sr / hop_length
    segment_frames = int(SEGMENT_DURATION * frames_per_second)

    if segment_frames >= len(rms):
        return [(0.0, float(np.mean(rms)))]

    scores = np.array([
        np.mean(rms[i : i + segment_frames])
        for i in range(len(rms) - segment_frames)
    ])

    results = []
    working = scores.copy()

    for _ in range(n):
        best_frame = int(np.argmax(working))
        energy = float(working[best_frame])
        if energy == -np.inf:
            break
        results.append((best_frame / frames_per_second, energy))

        # Mask all candidate start frames that would produce an overlapping segment.
        # A candidate j overlaps with best_frame iff |best_frame - j| < segment_frames.
        lo = max(0, best_frame - segment_frames + 1)
        hi = min(len(working), best_frame + segment_frames)
        working[lo:hi] = -np.inf

    results.sort(key=lambda x: x[1], reverse=True)
    print(
        f"  Top {len(results)} segments: "
        + ", ".join(f"{t:.2f}s (rms={e:.4f})" for t, e in results)
    )
    return results


def extract_vertical_clip(video_path, start_time, output_path, style="classic", motion="static"):
    """
    Cut a 15-second clip from video_path starting at start_time and:

    1. Convert from 16:9 landscape to 9:16 vertical (1080x1920) using a
       blurred background fill so the result looks intentional.
    2. Overlay "Zion Skank" text composited from a Pillow-rendered PNG.
    3. Optionally apply a motion effect to the foreground layer:
         static  — no motion (default)
         zoom_in — slow scale from 100% to 106% over the clip duration
         pan     — slow horizontal drift across PAN_EXTRA pixels
    4. Add a 1-second video and audio fade in/out at each end so the clip
       loops seamlessly when YouTube repeats it.

    style must be a key in OVERLAY_STYLES.
    """
    print(f"  Cutting and converting clip (start={start_time:.2f}s, style={style}, motion={motion})...")

    # Render text to a temp PNG using Pillow (avoids needing libfreetype in ffmpeg)
    tmp_png = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_png.close()
    create_text_overlay_png(tmp_png.name, style=style)

    fade_out_start = SEGMENT_DURATION - FADE_DURATION

    # ── Motion-dependent filter sections ─────────────────────────────────────
    # All effects are applied to [combined] (post-composite) so the text overlay
    # sits on top of the motion and stays sharp.  fg_scale/fg_composite are the
    # same for every mode; only motion_step and text_in vary.
    #
    # pulse:          rhythmic scale 1.0→1.03→1.0 on a PULSE_PERIOD-second cosine.
    # tilt_rock:      ±TILT_MAX_DEG rotation, one sine cycle over the clip.
    #                 Frame is pre-scaled to TILT_OVERSHOOT dimensions before
    #                 rotation so the centred crop never exposes a black corner.
    # yoyo_zoom:      scale 1.0→1.06 in first half, back to 1.0 in second half
    #                 via a full cosine cycle (same formula as pulse but slower).
    # vignette_pulse: static video; dark vignette breathes on PULSE_PERIOD cycle.

    fg_scale     = f"[0:v]scale={OUTPUT_WIDTH}:-2[fg];"
    fg_composite = "[bg][fg]overlay=(W-w)/2:(H-h)/2[combined];"

    if motion == "pulse":
        motion_step = (
            "[combined]"
            f"scale=w='trunc({OUTPUT_WIDTH}*(1+{PULSE_ZOOM}*(1-cos(2*PI*t/{PULSE_PERIOD}))/2)/2)*2'"
            f":h='trunc({OUTPUT_HEIGHT}*(1+{PULSE_ZOOM}*(1-cos(2*PI*t/{PULSE_PERIOD}))/2)/2)*2'"
            f":eval=frame,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(iw-{OUTPUT_WIDTH})/2:(ih-{OUTPUT_HEIGHT})/2"
            "[combined_m];"
        )
        text_in = "[combined_m]"
    elif motion == "tilt_rock":
        motion_step = (
            "[combined]"
            f"scale={TILT_OVERSHOOT_W}:{TILT_OVERSHOOT_H},"
            f"rotate=angle='{TILT_MAX_DEG}*PI/180*sin(2*PI*t/{SEGMENT_DURATION})'"
            f":c=black:ow=iw:oh=ih,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(iw-{OUTPUT_WIDTH})/2:(ih-{OUTPUT_HEIGHT})/2"
            "[combined_m];"
        )
        text_in = "[combined_m]"
    elif motion == "yoyo_zoom":
        motion_step = (
            "[combined]"
            f"scale=w='trunc({OUTPUT_WIDTH}*(1+{ZOOM_MAX}*(1-cos(2*PI*t/{SEGMENT_DURATION}))/2)/2)*2'"
            f":h='trunc({OUTPUT_HEIGHT}*(1+{ZOOM_MAX}*(1-cos(2*PI*t/{SEGMENT_DURATION}))/2)/2)*2'"
            f":eval=frame,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(iw-{OUTPUT_WIDTH})/2:(ih-{OUTPUT_HEIGHT})/2"
            "[combined_m];"
        )
        text_in = "[combined_m]"
    elif motion == "vignette_pulse":
        motion_step = (
            "[combined]"
            f"vignette=angle='0.7+0.2*sin(2*PI*t/{PULSE_PERIOD})':mode=forward:eval=frame"
            "[combined_m];"
        )
        text_in = "[combined_m]"
    else:  # static
        motion_step = ""
        text_in     = "[combined]"

    filter_complex = (
        # ── Background ───────────────────────────────────────────────────────
        "[0:v]"
        f"scale=w={OUTPUT_WIDTH}:h={OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
        "gblur=sigma=20[bg];"

        # ── Foreground scale + composite (motion-dependent) ──────────────────
        + fg_scale
        + fg_composite
        + motion_step

        # ── Text overlay ─────────────────────────────────────────────────────
        # The PNG is full-frame (1080x1920) with a transparent background, so
        # overlaying at 0:0 positions it correctly with no extra arithmetic.
        + f"{text_in}[1:v]overlay=0:0[withtext];"

        # ── Video fade in/out for loop crossfade ─────────────────────────────
        + "[withtext]"
        + f"fade=t=in:st=0:d={FADE_DURATION},"
        + f"fade=t=out:st={fade_out_start}:d={FADE_DURATION}"
        + "[out]"
    )

    audio_filter = (
        f"afade=t=in:st=0:d={FADE_DURATION},"
        f"afade=t=out:st={fade_out_start}:d={FADE_DURATION}"
    )

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start_time),  # seek before input = fast
                "-i", video_path,        # input 0: the video
                "-loop", "1",            # loop the still image for the full clip duration
                "-i", tmp_png.name,      # input 1: the text overlay PNG
                "-t", str(SEGMENT_DURATION),
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-map", "0:a",
                "-af", audio_filter,
                "-c:v", "libx264",
                "-c:a", "aac",
                "-crf", "23",
                "-movflags", "+faststart",
                output_path,
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print("\nffmpeg failed. Error output:\n")
        print(e.stderr.decode(errors="replace"))
        raise
    finally:
        os.unlink(tmp_png.name)

    print(f"  Vertical clip saved: {output_path}")


# ---------------------------------------------------------------------------
# Stage 2: Title and description generation (hardcoded template)
# ---------------------------------------------------------------------------

def generate_metadata(original_title, meta_style="standard"):
    """
    Build a Short-appropriate title and description from the original title.

    meta_style="standard":
        Title: {original_title} | Reggae Music | Zion Skank #Shorts
        Tagline: Reggae music for the soul. Subscribe for roots, dub, and groove.
        Hashtags: #Reggae #Dub #ReggaeInstrumental #ReggaeMusic #Shorts #ZionSkank

    meta_style="alternate":
        Title: {original_title} | Roots & Dub Vibes | Zion Skank #Shorts
        Tagline: Roots, dub, and groove — delivered weekly. Subscribe for the vibes.
        Hashtags: shuffled order

    YouTube enforces a 100-character title limit. If the original title is
    too long, it is trimmed at the last word boundary before the limit.
    """
    if meta_style == "alternate":
        suffix  = " | Roots & Dub Vibes | Zion Skank #Shorts"
        tagline = "Roots, dub, and groove — delivered weekly. Subscribe for the vibes."
        hashtags = "#ZionSkank #Shorts #ReggaeMusic #Dub #Reggae #ReggaeInstrumental"
    else:  # standard
        suffix  = " | Reggae Music | Zion Skank #Shorts"
        tagline = "Reggae music for the soul. Subscribe for roots, dub, and groove."
        hashtags = "#Reggae #Dub #ReggaeInstrumental #ReggaeMusic #Shorts #ZionSkank"

    max_title_len = 100 - len(suffix)

    if len(original_title) <= max_title_len:
        trimmed = original_title
    else:
        # Trim to the last complete word that fits, then add ellipsis
        trimmed = original_title[:max_title_len - 3].rsplit(" ", 1)[0] + "..."

    title = trimmed + suffix

    description = (
        f"{original_title}\n\n"
        f"{tagline}\n\n"
        "Zion Skank: https://www.youtube.com/@zionskank\n"
        "Zion Skank Dub Instrumentals: https://www.youtube.com/@zionskankdub\n"
        "Zion Skank Groove: https://www.youtube.com/@zionskankgroove\n\n"
        f"{hashtags}"
    )

    return title, description


# ---------------------------------------------------------------------------
# Stage 3: YouTube upload
# ---------------------------------------------------------------------------

def get_authenticated_service():
    """
    Return an authorised YouTube API client.

    On the first run this opens a browser window for the one-time OAuth
    consent flow and saves the resulting token to token.json.
    On every subsequent run it loads that token and refreshes it silently
    if it has expired — no browser needed.
    """
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(
            f"'{CLIENT_SECRETS_FILE}' not found. "
            "Download it from Google Cloud Console → APIs & Services → Credentials "
            "and place it in the same folder as this script."
        )

    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("  Refreshing access token...")
            creds.refresh(Request())
        else:
            print(
                "\n  ACTION REQUIRED before completing sign-in:\n"
                "  1. In the browser that opens, sign in with your main Google account\n"
                "  2. If YouTube is not already showing 'Zion Skank Shorts' as the\n"
                "     active channel, open a new tab, go to youtube.com, switch to\n"
                "     Zion Skank Shorts using the channel switcher, then return to\n"
                "     the OAuth tab and complete sign-in.\n"
                "  The token will be issued for whichever channel is active.\n"
            )
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0, prompt="select_account")

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(video_path, title, description):
    """
    Upload video_path to YouTube with the given title and description.

    Uses resumable upload so progress is shown and large files won't time
    out on slow connections. Returns the YouTube video ID.
    """
    print("  Authenticating...")
    youtube = get_authenticated_service()

    # Check which channel the token is targeting before uploading anything.
    # With the full `youtube` scope this call is reliable.
    response = youtube.channels().list(part="id,snippet", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError(
            "No YouTube channel found for this account.\n"
            "Delete token.json and re-run, signing in with your main Google account."
        )
    active_id   = items[0]["id"]
    active_name = items[0]["snippet"]["title"]
    print(f"  Active channel: {active_name} ({active_id})")

    if active_id != TARGET_CHANNEL_ID:
        raise RuntimeError(
            f"Wrong channel: '{active_name}' ({active_id}).\n"
            f"Delete token.json and re-run. See instructions below for how to\n"
            f"get the OAuth flow to pick up Zion Skank Shorts."
        )

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "10",  # Music
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)

    print("  Uploading...")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  {int(status.progress() * 100)}% uploaded...")

    video_id      = response["id"]
    uploaded_to   = response["snippet"]["channelId"]

    if uploaded_to != TARGET_CHANNEL_ID:
        print(f"  Wrong channel ({uploaded_to}). Deleting video...")
        youtube.videos().delete(id=video_id).execute()
        raise RuntimeError(
            f"Video landed on channel {uploaded_to}, not Zion Skank Shorts "
            f"({TARGET_CHANNEL_ID}).\n"
            f"Delete token.json and re-run, signing in with the account that "
            f"manages the Zion Skank Shorts channel."
        )

    print(f"  Channel confirmed: Zion Skank Shorts")
    print(f"  Live at: https://www.youtube.com/shorts/{video_id}")
    return video_id


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Zion Skank Shorts Pipeline")
    parser.add_argument("--input", required=True, help="Path to input MP4 file")
    parser.add_argument("--title", required=True, help="Original video title")
    parser.add_argument(
        "--variation", type=int, default=1, choices=[1, 2, 3],
        help="Which energy-ranked segment to use (1=highest, 2=second, 3=third). Default: 1",
    )
    parser.add_argument(
        "--style", default="classic", choices=list(OVERLAY_STYLES),
        help="Text overlay style preset. Default: classic",
    )
    parser.add_argument(
        "--motion", default="static", choices=["static", "pulse", "tilt_rock", "yoyo_zoom", "vignette_pulse"],
        help="Foreground motion effect. Default: static",
    )
    parser.add_argument(
        "--meta-style", default="standard", choices=["standard", "alternate"],
        help="Title/description template variant. Default: standard",
    )
    parser.add_argument(
        "--preset", type=int, choices=list(VARIATION_PRESETS),
        help="Apply a named preset (1-5), overriding --variation/--style/--motion/--meta-style.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip the YouTube upload (Stages 1 and 2 still run).",
    )
    args = parser.parse_args()

    if args.preset is not None:
        p = VARIATION_PRESETS[args.preset]
        args.variation  = p["variation"]
        args.style      = p["style"]
        args.motion     = p["motion"]
        args.meta_style = p["meta_style"]
        print(f"  Preset {args.preset}: variation={args.variation}, style={args.style}, "
              f"motion={args.motion}, meta_style={args.meta_style}")

    if not os.path.exists(args.input):
        print(f"Error: file not found: {args.input}")
        return

    base = os.path.splitext(args.input)[0]
    output_path = f"{base}_short.mp4"

    # Stage 1
    print("\n[Stage 1] Finding best segment and extracting clip...")
    segments = find_best_segment(args.input)
    idx = args.variation - 1
    if idx >= len(segments):
        print(
            f"Error: --variation {args.variation} requested but only "
            f"{len(segments)} non-overlapping segment(s) found in this track."
        )
        return
    start_time, _ = segments[idx]
    extract_vertical_clip(args.input, start_time, output_path, style=args.style, motion=args.motion)
    print("[Stage 1] Done.\n")

    # Stage 2
    print("[Stage 2] Generating title and description...")
    title, description = generate_metadata(args.title, meta_style=args.meta_style)
    print(f"  Title ({len(title)} chars): {title}")
    print(f"  Description:\n    {description.replace(chr(10), chr(10) + '    ')}")
    print("[Stage 2] Done.\n")

    # Stage 3
    if args.dry_run:
        print("[Stage 3] Skipped (--dry-run).\n")
        return
    print("[Stage 3] Uploading to YouTube...")
    upload_to_youtube(output_path, title, description)
    print("[Stage 3] Done.\n")


if __name__ == "__main__":
    main()
