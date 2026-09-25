"""
Custom Reels Posting Agent
---------------------------
A small web server that n8n (or anything else) can call to reliably
post a video as a Facebook Reel or Instagram Reel WITH a caption attached.

Why this exists: Facebook's Resumable Upload API needs the *exact*
byte size of the video file and the *raw* bytes streamed to it.
n8n's cloud binary-data handling made that unreliable. This script
avoids the problem entirely by downloading the video to a real file
on disk and reading its exact size directly from the filesystem --
no buffering tricks, no size estimation, no guessing.

How it's used:

    POST /post-facebook-reel
    {
        "video_url": "https://.../video.mp4",   # any direct-downloadable URL
        "caption": "Your caption text here",
        "page_id": "your_facebook_page_id",
        "access_token": "your_long_lived_page_token"
    }

    POST /post-instagram-reel
    {
        "video_url": "https://.../video.mp4",   # must be a publicly reachable URL
        "caption": "Your caption text here",
        "ig_user_id": "your_instagram_business_account_id",
        "access_token": "your_long_lived_page_token"
    }

    Response (both):
        { "success": true, ... }
    or
        { "success": false, "error": "..." }
"""

import os
import time
import tempfile
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

GRAPH_API_VERSION = "v25.0"


def download_video(video_url: str) -> str:
    """Download the video to a temp file on disk and return its local path.
    Downloading to a real file (instead of holding it in memory) is what
    lets us get an exact, trustworthy byte size afterwards."""
    response = requests.get(video_url, stream=True, timeout=120)
    response.raise_for_status()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    with tmp as f:
        for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
            if chunk:
                f.write(chunk)

    return tmp.name


def start_upload_session(page_id: str, access_token: str) -> dict:
    """Step 1: tell Facebook we're about to upload a Reel. Returns
    the video_id and the upload_url to send the actual bytes to."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}/video_reels"
    resp = requests.post(url, data={
        "upload_phase": "start",
        "access_token": access_token,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def upload_video_bytes(upload_url: str, access_token: str, file_path: str) -> None:
    """Step 2: stream the raw video bytes to Facebook's resumable upload
    endpoint. Reading the exact size straight from disk (os.path.getsize)
    is the key fix -- it can never be wrong or rounded."""
    file_size = os.path.getsize(file_path)

    headers = {
        "Authorization": f"OAuth {access_token}",
        "offset": "0",
        "file_size": str(file_size),
    }

    with open(file_path, "rb") as f:
        resp = requests.post(upload_url, headers=headers, data=f, timeout=600)
    resp.raise_for_status()


def finish_upload_session(page_id: str, access_token: str, video_id: str, caption: str) -> dict:
    """Step 3: tell Facebook the upload is complete and attach the caption,
    then publish it. This is the step that actually makes the caption stick."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}/video_reels"
    resp = requests.post(url, data={
        "upload_phase": "finish",
        "video_id": video_id,
        "description": caption,
        "video_state": "PUBLISHED",
        "access_token": access_token,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def post_facebook_reel(video_url: str, caption: str, page_id: str, access_token: str) -> dict:
    """The full end-to-end flow. Raises an exception on any failure so the
    caller (the Flask route below) can report a clear error back to n8n."""
    local_path = download_video(video_url)
    try:
        session = start_upload_session(page_id, access_token)
        video_id = session["video_id"]
        upload_url = session["upload_url"]

        upload_video_bytes(upload_url, access_token, local_path)

        # Facebook needs a moment to process the uploaded bytes before
        # it will accept the "finish" call -- a short wait avoids a race.
        time.sleep(5)

        result = finish_upload_session(page_id, access_token, video_id, caption)
        return {"success": True, "video_id": video_id, "facebook_response": result}
    finally:
        # Always clean up the downloaded file, even if something failed.
        if os.path.exists(local_path):
            os.remove(local_path)


def create_instagram_container(video_url: str, caption: str, ig_user_id: str, access_token: str) -> str:
    """Step 1: ask Instagram to create a Reel from a video URL it will
    fetch itself. Unlike Facebook, Instagram's Graph API pulls the video
    from a public URL rather than receiving raw bytes, so no local
    download or byte-size math is needed here. Returns a creation_id."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ig_user_id}/media"
    resp = requests.post(url, data={
        "video_url": video_url,
        "caption": caption,
        "media_type": "REELS",
        "access_token": access_token,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def wait_for_container_ready(creation_id: str, access_token: str, timeout_seconds: int = 180) -> None:
    """Instagram processes the video asynchronously after creating the
    container. We poll its status until it's FINISHED (ready to publish)
    or ERROR, instead of guessing a fixed wait time."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{creation_id}"
    waited = 0
    while waited < timeout_seconds:
        resp = requests.get(url, params={
            "fields": "status_code",
            "access_token": access_token,
        }, timeout=30)
        resp.raise_for_status()
        status = resp.json().get("status_code")

        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError("Instagram failed to process the video container")

        time.sleep(5)
        waited += 5

    raise TimeoutError("Instagram container did not finish processing in time")


def publish_instagram_container(creation_id: str, ig_user_id: str, access_token: str) -> dict:
    """Step 3: publish the now-ready container as a live Reel."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ig_user_id}/media_publish"
    resp = requests.post(url, data={
        "creation_id": creation_id,
        "access_token": access_token,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def post_instagram_reel(video_url: str, caption: str, ig_user_id: str, access_token: str) -> dict:
    """The full end-to-end Instagram flow."""
    creation_id = create_instagram_container(video_url, caption, ig_user_id, access_token)
    wait_for_container_ready(creation_id, access_token)
    result = publish_instagram_container(creation_id, ig_user_id, access_token)
    return {"success": True, "post_id": result.get("id"), "instagram_response": result}


@app.route("/post-instagram-reel", methods=["POST"])
def post_instagram_reel_route():
    data = request.get_json(force=True)

    required_fields = ["video_url", "caption", "ig_user_id", "access_token"]
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return jsonify({"success": False, "error": f"Missing fields: {', '.join(missing)}"}), 400

    try:
        result = post_instagram_reel(
            video_url=data["video_url"],
            caption=data["caption"],
            ig_user_id=data["ig_user_id"],
            access_token=data["access_token"],
        )
        return jsonify(result)
    except requests.HTTPError as e:
        return jsonify({"success": False, "error": f"Instagram API error: {e.response.text}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/post-facebook-reel", methods=["POST"])
def post_facebook_reel_route():
    data = request.get_json(force=True)

    required_fields = ["video_url", "caption", "page_id", "access_token"]
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return jsonify({"success": False, "error": f"Missing fields: {', '.join(missing)}"}), 400

    try:
        result = post_facebook_reel(
            video_url=data["video_url"],
            caption=data["caption"],
            page_id=data["page_id"],
            access_token=data["access_token"],
        )
        return jsonify(result)
    except requests.HTTPError as e:
        return jsonify({"success": False, "error": f"Facebook API error: {e.response.text}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Listens on all network interfaces so n8n (running elsewhere) can reach it.
    app.run(host="0.0.0.0", port=5000)
