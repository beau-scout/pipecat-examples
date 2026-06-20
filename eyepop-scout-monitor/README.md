# EyePop Scout Monitor

A Windows-friendly local web app that connects to an **RTSP camera** (tested
target: Axis), runs people detection through **[EyePop.ai](https://docs.eyepop.ai/developer-documentation)**,
and tells you:

- how many **people** are in view,
- whether each person looks like an **adult or a child**,
- whether a person is **alone or in a group** (proximity-based), and
- whether a monitored **door is open** (a security risk).

It serves a small dashboard with the live annotated video, running counts, and a
condition-based **alerts** feed (e.g. *“child alone”*, *“Front Door is OPEN”*).

> Built on the EyePop Python SDK (`workerEndpoint` → `set_pop` → `upload_stream`
> → `predict`). Frames are grabbed locally from the camera and uploaded to EyePop,
> because your Axis cameras live on your LAN where the EyePop cloud can't reach
> them directly.

---

## How it works

```
Axis RTSP ──► FrameGrabber (OpenCV, background thread)
                  │ latest frame
                  ▼
            Inference loop (every ~1s)
                  ├─ EyePop person + age  ─► adult/child
                  ├─ proximity clustering ─► alone/group
                  └─ door detection       ─► open/closed
                  │
                  ├─► Alert engine (debounced rules)
                  └─► Shared state ──► FastAPI
                                        ├─ /stream  annotated MJPEG
                                        ├─ /events  SSE alerts + stats
                                        └─ /        dashboard
```

The video preview stays smooth because the inference loop runs on its own
thread; the stream simply draws the most recent detections onto the freshest
frame.

---

## Requirements

- **Python 3.11+** (the EyePop SDK requires ≥3.11)
- An **EyePop.ai account** with a Pop configured for **Person w/ Demographic
  Data** (person detection + age-range). Grab your **secret key** and **Pop id**
  from <https://dashboard.eyepop.ai>.
- An RTSP camera (Axis) reachable from the PC, or use the built-in synthetic
  source to try it with no hardware.

## Setup (Windows)

```powershell
cd eyepop-scout-monitor
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env          # then edit .env with your EyePop key + Pop id
copy config.example.yaml config.yaml
```

Edit `.env`:

```
EYEPOP_SECRET_KEY=sk_...
EYEPOP_POP_ID=...
```

Set your camera in `config.yaml` (`camera.source`). Typical Axis RTSP URLs:

```
rtsp://USER:PASS@192.168.1.50/axis-media/media.amp
rtsp://USER:PASS@192.168.1.50/axis-media/media.amp?videocodec=h264&resolution=1280x720
```

## Run

```powershell
python run.py
# then open http://localhost:8000
```

Handy overrides (no config edits needed):

```powershell
python run.py --source rtsp://user:pass@192.168.1.50/axis-media/media.amp
python run.py --mock --source synthetic    # full demo, no camera and no API spend
```

`--mock` fabricates detections; `--source synthetic` generates a test video
in-process. Together they let you click around the dashboard immediately.

---

## Configuration highlights (`config.yaml`)

| Section | Key | Meaning |
|---|---|---|
| `inference` | `interval_s` | Seconds between EyePop calls (controls cost). |
| `inference` | `mock` | Synthetic detections, no API calls. |
| `age` | `adult_min_age` | Age (years) at/above which a person is an adult. |
| `age` | `child_labels` | Raw labels always treated as a child. |
| `grouping` | `proximity_factor` | Group link distance = factor × mean person height. |
| `doors` | `enabled` / `mode` | Turn on door detection; pick `roi_threshold` or `eyepop`. |
| `alerts` | `rules` | Enable/disable each rule and its cooldown. |
| `alerts` | `webhook_url` | Optional: POST each alert as JSON. |

### Adult vs child

Age comes from EyePop's `age-range` class on each person. The app maps a label
like `10-17` to **child** and `25-34` to **adult** using `age.adult_min_age`
(default 18). It also honors explicit `child_labels` (e.g. `child`, `minor`).

> ⚠️ **Confirm against your account.** Some EyePop demographic models are tuned
> for **18+** age bands and may not reliably distinguish young children. The app
> logs whatever raw age labels it receives so you can tune `adult_min_age` /
> `child_labels` to your Pop's actual outputs.

### Alone vs group

Two people are linked into a group when the distance between their bounding-box
centers is within `proximity_factor × mean(box height)`. The threshold scales
with apparent size, so people standing close are grouped while far-apart people
are not. A person in no cluster is **alone**.

### Open-door detection

This was added per request, referencing EyePop's **Door Thresholding** work
(`eyepop-ai/scout_shared`). That repo is **private**, so the implementation here
is a faithful reconstruction of the *intent* with two interchangeable modes —
**please confirm the exact model/thresholds** against that source:

**`roi_threshold` (no model needed):**
1. Mark each door region in `config.yaml → doors.rois` as `rect: [x, y, w, h]`.
2. With all doors closed, click **“Capture doors closed reference”** in the
   dashboard (or set `doors.reference_image`).
3. Each cycle the app measures how much the ROI differs from the reference; a
   large, sustained difference (`open_threshold`, held `consecutive_frames`)
   means **open**.

**`eyepop` (model-based):** set `doors.mode: eyepop` and point it at a Pop that
detects doors and classifies open/closed (via `EYEPOP_DOOR_POP_ID` or
`doors.door_pop_json`). The app reads the open/closed class per door.

---

## Alerts

Rules evaluated every cycle, debounced per `default_cooldown_s`:

| Rule | Fires when | Severity |
|---|---|---|
| `child_detected` | any child in view | medium |
| `child_alone` | a child with no group nearby | high |
| `child_with_group` | a child is part of a group | medium |
| `group_detected` | a group of `min_size`+ people | low |
| `door_open` | a monitored door is open | high |

Alerts appear live in the dashboard (with optional sound + browser
notification), are logged to the console, and can be POSTed to a `webhook_url`.

---

## Project layout

```
eyepop-scout-monitor/
├─ run.py                # entrypoint (uvicorn)
├─ config.example.yaml   # copy to config.yaml
├─ .env.example          # copy to .env (secrets)
├─ app/
│  ├─ config.py          # settings models + loader
│  ├─ capture.py         # RTSP/file/webcam/synthetic frame grabber
│  ├─ eyepop_client.py   # EyePop wrapper + mock inferer
│  ├─ analysis.py        # people, adult/child, proximity groups
│  ├─ doors.py           # open-door detection (roi_threshold | eyepop)
│  ├─ alerts.py          # rule engine + debounce + SSE/webhook fan-out
│  ├─ annotate.py        # overlay drawing
│  ├─ pipeline.py        # orchestration + shared state
│  └─ server.py          # FastAPI routes (stream, events, REST)
├─ web/                  # dashboard (index.html, app.js, styles.css)
└─ pops/                 # optional in-code Pop definitions
```

## To confirm with the team

- **EyePop age model:** does your Pop emit child-range labels, or only 18+?
  This determines how reliable adult/child is and how to set `child_labels`.
- **Door Thresholding:** share the `scout_shared/Door Thresholding` approach (or
  the door Pop/ability id) so this can match your existing implementation exactly.
```
