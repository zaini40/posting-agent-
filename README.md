[README (1).md](https://github.com/user-attachments/files/32642339/README.1.md)
# Reels Posting Agent

A tiny web server that reliably posts a video as a Facebook Reel with a
caption attached — the piece n8n itself kept failing at.

## What it does

You send it a video URL, a caption, your Page ID, and your access token.
It downloads the video, uploads it to Facebook correctly (exact byte size,
raw bytes, no guessing), attaches the caption, and publishes it. It sends
back a simple success/failure response.

## 1. Get a server (if you don't have one yet)

Any small Linux server works. Cheapest options:
- Oracle Cloud Free Tier — $0/month
- Hetzner — about $5/month

## 2. Install Python and the two packages this needs

On the server, run:
```
sudo apt update
sudo apt install python3 python3-pip -y
pip3 install -r requirements.txt
```

## 3. Run it

```
python3 app.py
```

This starts the server listening on port 5000. Leave this running — for a
real always-on agent later, we'll set it up with `systemd` so it survives
reboots and keeps running in the background, but for testing this is fine
to run directly in a terminal.

To check it's alive, visit (in a browser or with curl):
```
http://YOUR_SERVER_IP:5000/health
```
You should see: `{"status": "ok"}`

## 4. Call it from n8n

In your n8n workflow, replace the whole Reels-posting chain (start session,
HEAD request, download, upload, finish) with a single **HTTP Request** node:

- **Method**: POST
- **URL**: `http://YOUR_SERVER_IP:5000/post-facebook-reel`
- **Body Content Type**: JSON
- **Body**:
```json
{
  "video_url": "={{ $json.driveDirectDownloadLink }}",
  "caption": "={{ $json.captions }}",
  "page_id": "1260966167099030",
  "access_token": "YOUR_LONG_LIVED_TOKEN"
}
```

That's it — one node instead of five, and no more exact-byte-size problems,
because this script reads the real file size straight off the disk.

## Posting to Instagram

Use the same server's second route: **`/post-instagram-reel`**

- **Method**: POST
- **URL**: `http://YOUR_SERVER_IP:5000/post-instagram-reel` (or your Render URL)
- **Body Content Type**: JSON
- **Body**:
```json
{
  "video_url": "={{ $json.driveDirectDownloadLink }}",
  "caption": "={{ $json.captions }}",
  "ig_user_id": "1260966167099030",
  "access_token": "YOUR_LONG_LIVED_TOKEN"
}
```

Note: Instagram's API fetches the video from `video_url` itself (rather
than receiving uploaded bytes like Facebook does), so the URL must be
publicly reachable — the same Drive direct-download link format works.
Instagram also takes a little longer to process before publishing; the
script polls automatically and only returns once it's actually live.

## Next steps (once this is working)

- Add YouTube and LinkedIn routes the same way.
- Move the access token into an environment variable instead of passing it
  in every request, so it's not sitting in your n8n workflow in plain text.
- Set this up with `systemd` so it runs permanently and restarts itself
  if the server reboots.
