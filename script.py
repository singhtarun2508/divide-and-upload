"""
Google Drive Video Processor
-----------------------------
- Fetches videos from 'main_videos' Google Drive folder
- Downloads one at a time locally
- Splits into 35-second clips using FFmpeg
- Uploads clips to 'ready_clips' Google Drive folder
- Deletes original after successful upload
- Fully resumable via progress.json
"""

import os
import json
import time
import subprocess
import math
from pathlib import Path
import re
import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
import tqdm

# ─────────────────────────────────────────────
# CONFIG — update these two IDs before running
# ─────────────────────────────────────────────
MAIN_VIDEOS_FOLDER_ID = "1c7wmuavpLD8tVNOS8ar_uyL1FkUEVyxz"   # paste folder ID here
READY_CLIPS_FOLDER_ID = "1SkQgsJRR9G3lRYQlFzyR3wXz8gyjg4l3"   # paste folder ID here
STATUS_FOLDER_ID = "1bxd0BHRm-AU5JguMmP9sW3u5zZCvoQvQ"  # create a 'status' folder on Drive and paste ID here

CLIP_DURATION = 35          # seconds per clip
MAX_RETRIES   = 3           # retry attempts for download/upload
RETRY_DELAY   = 5           # seconds between retries

# Paths
BASE_DIR      = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
CLIPS_DIR     = BASE_DIR / "clips"
PROGRESS_FILE = BASE_DIR / "progress.json"
CREDS_FILE    = BASE_DIR / "credentials.json"
TOKEN_FILE    = BASE_DIR / "token.json"

SCOPES = ["https://www.googleapis.com/auth/drive"]

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────
DOWNLOADS_DIR.mkdir(exist_ok=True)
CLIPS_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────
def get_drive_service():
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


# ─────────────────────────────────────────────
# PROGRESS TRACKING
# ─────────────────────────────────────────────


def load_progress(service):
    """Load latest progress from status folder on Drive."""
    resp = service.files().list(
        q=f"'{STATUS_FOLDER_ID}' in parents and mimeType='application/json' and trashed=false",
        spaces="drive",
        fields="files(id, name)",
        orderBy="name desc",
    ).execute()
    files = resp.get("files", [])
    if not files:
        print("  No status file found. Starting fresh.")
        return {}
    latest = files[0]
    print(f"  Loading status from: {latest['name']}")
    request = service.files().get_media(fileId=latest["id"])
    import io
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return json.loads(fh.getvalue().decode())

def save_progress(service, progress):
    """Save progress to a dated JSON file in status folder on Drive."""
    today = datetime.date.today().isoformat()
    file_name = f"{today}.json"
    content = json.dumps(progress, indent=2).encode()

    # Write to a temp local file first
    temp_path = BASE_DIR / file_name
    with open(temp_path, "w") as f:
        json.dump(progress, f, indent=2)

    # Check if today's file already exists on Drive
    resp = service.files().list(
        q=f"'{STATUS_FOLDER_ID}' in parents and name='{file_name}' and trashed=false",
        spaces="drive",
        fields="files(id)",
    ).execute()
    existing = resp.get("files", [])

    media = MediaFileUpload(str(temp_path), mimetype="application/json", resumable=False)

    if existing:
        service.files().update(
            fileId=existing[0]["id"],
            media_body=media,
        ).execute()
    else:
        service.files().create(
            body={"name": file_name, "parents": [STATUS_FOLDER_ID]},
            media_body=media,
            fields="id"
        ).execute()

    temp_path.unlink()  # remove temp file
def set_status(service, progress, file_id, status, extra=None):
    if file_id not in progress:
        progress[file_id] = {}
    progress[file_id]["status"] = status
    if extra:
        progress[file_id].update(extra)
    save_progress(service, progress)
    print(f"  [progress] {file_id[:12]}... → {status}")



# ─────────────────────────────────────────────
# GOOGLE DRIVE HELPERS
# ─────────────────────────────────────────────
def list_videos(service, folder_id):
    """List all video files in the given Drive folder."""
    results = []
    page_token = None
    query = (
        f"'{folder_id}' in parents"
        " and (mimeType contains 'video/' or mimeType = 'application/octet-stream')"
        " and trashed = false"
    )
    while True:
        resp = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, size)",
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def download_file(service, file_id, file_name, dest_folder):
    """Download a file from Drive with retry logic."""
    dest_path = dest_folder / file_name

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  Downloading '{file_name}' (attempt {attempt})...")
            request = service.files().get_media(fileId=file_id)
            with open(dest_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                pbar = tqdm.tqdm(total=100, unit="%", desc="  Download")
                while not done:
                    status, done = downloader.next_chunk()
                    if status:
                        pbar.n = int(status.progress() * 100)
                        pbar.refresh()
                pbar.close()
            print(f"  ✓ Downloaded to {dest_path}")
            return dest_path
        except Exception as e:
            print(f"  ✗ Download failed (attempt {attempt}): {e}")
            if dest_path.exists():
                dest_path.unlink()
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(f"Failed to download '{file_name}' after {MAX_RETRIES} attempts.")


def upload_file(service, local_path, parent_folder_id):
    """Upload a file to Drive with retry logic. Returns the uploaded file's ID."""
    file_name = local_path.name
    mime_type = "video/x-matroska"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"    Uploading '{file_name}' (attempt {attempt})...")
            metadata = {"name": file_name, "parents": [parent_folder_id]}
            media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
            uploaded = service.files().create(
                body=metadata, media_body=media, fields="id"
            ).execute()
            print(f"    ✓ Uploaded '{file_name}' (id: {uploaded['id'][:12]}...)")
            return uploaded["id"]
        except Exception as e:
            print(f"    ✗ Upload failed (attempt {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(f"Failed to upload '{file_name}' after {MAX_RETRIES} attempts.")


def delete_drive_file(service, file_id, file_name):
    """Permanently delete a file from Drive."""
    try:
        service.files().update(fileId=file_id, body={"trashed": True}).execute()
        print(f"  ✓ Deleted original '{file_name}' from Drive.")

    except Exception as e:
        print(f"  ✗ Could not delete '{file_name}': {e}")
        print(f"  [hint] Full error: {str(e)}")
        raise

# ─────────────────────────────────────────────
# FFMPEG SPLITTING
# ─────────────────────────────────────────────
def get_video_duration(video_path):
    """Use ffprobe to get video duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def split_video(video_path, output_folder, clip_duration=CLIP_DURATION):
    """
    Split video into clips of `clip_duration` seconds each.
    Uses -c copy (no re-encoding) for speed.
    Returns list of clip paths.
    """
    duration   = get_video_duration(video_path)
    num_clips  = math.ceil(duration / clip_duration)
    stem       = video_path.stem
    clips      = []

    print(f"  Splitting '{video_path.name}' → {num_clips} clips of {clip_duration}s each")

    for i in range(num_clips):
        start     = i * clip_duration
        clip_name = f"{stem}_clip{i+1:03d}.mp4"
        clip_path = output_folder / clip_name

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(video_path),
            "-t", str(clip_duration),
            "-c", "copy",
            str(clip_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg failed on clip {i+1}:\n{result.stderr}"
            )

        clips.append(clip_path)
        print(f"    ✓ {clip_name}")

    return clips


# ─────────────────────────────────────────────
# CLEANUP
# ─────────────────────────────────────────────
def cleanup_local(video_path, clips):
    """Remove downloaded video and all its clips from local disk."""
    if video_path and video_path.exists():
        video_path.unlink()
        print(f"  ✓ Removed local video: {video_path.name}")
    for clip in clips:
        if clip.exists():
            clip.unlink()
    if clips:
        print(f"  ✓ Removed {len(clips)} local clips.")


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────
def process_all():
    print("\n========================================")
    print("  Google Drive Video Processor — Start  ")
    print("========================================\n")

    service  = get_drive_service()
    
    progress = load_progress(service)

    # Step 1: List all videos in main_videos
    print("Scanning 'main_videos' folder on Drive...")
    videos = list_videos(service, MAIN_VIDEOS_FOLDER_ID)

    if not videos:
        print("No videos found in 'main_videos'. Exiting.")
        return

    print(f"Found {len(videos)} video(s).\n")
    

    videos = sorted(videos, key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x['name'])])
    videos = videos[:1]  # only process the topmost video

    # Check clip count in ready_clips before proceeding
    print("Checking clip count in 'ready_clips' folder...") 
    clip_resp = service.files().list(
        q=f"'{READY_CLIPS_FOLDER_ID}' in parents and trashed = false",
        spaces="drive",
        fields="files(id)",
    ).execute()
    clip_count = len(clip_resp.get("files", []))
    print(f"  Clips in ready_clips: {clip_count}")

    if clip_count < 4:
        print(f"  ✋ {clip_count} clips already in ready_clips (> 4). Skipping processing.")
        return

    print(f"  ✓ Clip count is {clip_count} (≤ 4), proceeding...\n")
    print(f"Processing only: {videos[0]['name']}\n")    


    for video in videos:
        file_id   = video["id"]
        file_name = video["name"]
        state     = progress.get(file_id, {}).get("status", "pending")

        print(f"─────────────────────────────────────────")
        print(f"Video : {file_name}")
        print(f"Status: {state}")

        # Already fully done — skip
        if state == "done":
            print("  Already processed. Skipping.\n")
            continue

        local_video = DOWNLOADS_DIR / file_name
        clips       = []

        # ── STEP: Download ──────────────────────
        if state in ("pending",):
            local_video = download_file(service, file_id, file_name, DOWNLOADS_DIR)
            set_status(service,progress, file_id, "downloaded", {"local_path": str(local_video)})
            state = "downloaded"

        if state == "downloaded":
            local_video = Path(progress[file_id].get("local_path", str(local_video)))
            if not local_video.exists():
                print("  Local file missing, re-downloading...")
                local_video = download_file(service, file_id, file_name, DOWNLOADS_DIR)
                set_status(service,progress, file_id, "downloaded", {"local_path": str(local_video)})

        # ── STEP: Split ─────────────────────────
        if state == "downloaded":
            clips = split_video(local_video, CLIPS_DIR)
            clip_names = [c.name for c in clips]
            set_status(service,progress, file_id, "split", {"clips": clip_names})
            state = "split"

        # ── STEP: Upload clips ───────────────────
        if state == "split":
            clip_names   = progress[file_id].get("clips", [])
            uploaded_ids = progress[file_id].get("uploaded_ids", [])
            already_done = len(uploaded_ids)

            clips = [CLIPS_DIR / name for name in clip_names]

            print(f"  Uploading {len(clips)} clips ({already_done} already uploaded)...")
            for i, clip_path in enumerate(clips):
                if i < already_done:
                    continue  # resumable — skip already uploaded
                if not clip_path.exists():
                    raise FileNotFoundError(f"Clip missing: {clip_path}")
                uid = upload_file(service, clip_path, READY_CLIPS_FOLDER_ID)
                uploaded_ids.append(uid)
                # save after each upload so crash mid-upload is resumable
                set_status(service,progress, file_id, "split", {
                    "clips": clip_names,
                    "uploaded_ids": uploaded_ids,
                })

            set_status(service,progress, file_id, "uploaded", {
                "clips": clip_names,
                "uploaded_ids": uploaded_ids,
            })
            state = "uploaded"

        # ── STEP: Delete original from Drive ────
        if state == "uploaded":
            delete_drive_file(service, file_id, file_name)
            local_video = DOWNLOADS_DIR / file_name
            clips       = [CLIPS_DIR / n for n in progress[file_id].get("clips", [])]
            cleanup_local(local_video, clips)
            set_status(service,progress, file_id, "done")
            print(f"  ✓ '{file_name}' fully processed.\n")

    print("\n========================================")
    print("  All videos processed successfully!    ")
    print("========================================\n")


if __name__ == "__main__":
    process_all()
