# pydashcamviewer

Cross-platform dashcam MP4 viewer with synchronized GPS map playback.

![pyDashcamViewer screenshot](pyDashcamview_image.png)

> [!IMPORTANT]
> This project is currently tested with **VIOFO A229 Plus** MP4 files.
> It may also work with other Novatek-based dashcams, but compatibility is not guaranteed yet.

## How to Use It

After installation, run the tool as `pydashcamviewer`.

Open the file picker and choose a video:

```bash
pydashcamviewer
```

Open a specific MP4 directly:

```bash
pydashcamviewer /path/to/dashcam_clip.mp4
```

Disable daylight-saving-time correction when parsing video timestamps:

```bash
pydashcamviewer --no-daylight-saving-time /path/to/dashcam_clip.mp4
```

Inside the app:

- Left panel: video playback controls (play, pause, seek, load new file)
- Right panel: live map marker synced to video time
- Bottom info: speed, latitude/longitude, and GPS timestamp

## Install

Install as a [uv tool](https://docs.astral.sh/uv/) from this GitHub repo:

```bash
uv tool install git+https://github.com/hammerill/pydashcamviewer
```

Or local dev install:

```bash
# in pydashcamviewer project folder
uv tool install -e .
```
