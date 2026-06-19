# Conference Visitor Check-In Kiosk

A single-file web app for checking in registered guests at a conference and
printing 3" × 3" name badges. Designed for a large (27") touchscreen running
fullscreen in a browser — no server, no build step, no dependencies.

## Quick start

1. Open `index.html` in Chrome (or serve the folder: `python3 -m http.server`).
2. Press `F11` for fullscreen kiosk mode.
3. Guests type their name on the on-screen keyboard, tap their card, and
   confirm — the badge prints and they're marked as checked in.

A small sample guest list is preloaded so you can try it immediately.

## Loading your guest list

1. Tap the gear icon (top right) and enter the admin PIN (default **1234**,
   change `ADMIN_PIN` near the top of the `<script>` in `index.html`).
2. Tap **Upload Guest List (CSV)** and choose your file.

The CSV needs a header row with a `name` column (or `first name` /
`last name`), plus optional `company` (or `organization`) and `email`
columns — the format most registration platforms (Eventbrite, Luma, etc.)
export. See `sample-guests.csv` for an example.

## Walk-in registration

Guests who aren't on the pre-registered list can check themselves in at the
door. When a search returns no match (or from the welcome screen), a **walk-in**
button opens a short form with its own on-screen keyboard. The guest enters
their name and (optionally) company, and the app adds them to the list, checks
them in, and prints their badge just like a pre-registered guest. Walk-ins are
flagged in the exported check-in report (`walk_in` column) so you can
reconcile them later.

## Badge printing (3" × 3" labels)

The badge is laid out with print CSS at exactly 3in × 3in (`@page { size: 3in 3in; margin: 0 }`):
event name on top, the guest's name large in the middle (auto-shrinks for
long names), company below it, and an ATTENDEE footer with a QR code.

### QR code

Each badge includes a QR code (bottom-right of the footer) encoding the
guest as a **vCard** — name, company, and email. Attendees can scan each
other's badges with a phone camera to save the contact, and you can scan
them at session doors for attendance. UTF-8 names (accents, non-Latin
characters) are supported.

Toggle the QR code on or off from the admin panel (**Badge QR Code: On/Off**);
the setting is remembered per kiosk. The QR is generated fully offline by a
vendored copy of the MIT-licensed
[`qrcode-generator`](https://github.com/kazuhikoarase/qrcode-generator)
library (`qrcode.js`) — no network or external service is used.

For a smooth kiosk experience with a label printer (Dymo, Brother QL,
Zebra, etc.):

1. In your OS printer settings, set the label printer as the **default
   printer** with a 3" × 3" media size.
2. Launch Chrome with silent printing so no print dialog appears:

   ```bash
   chrome --kiosk --kiosk-printing http://localhost:8000
   ```

   (`--kiosk-printing` prints straight to the default printer; `--kiosk`
   gives you fullscreen with no browser chrome.)

3. Use **Print Test Badge** in the admin panel to check alignment before
   doors open.

Without those flags the normal print dialog appears — fine for testing.

## Admin panel features

- Live stats: registered / checked in / remaining
- Upload guest list (replaces the current list)
- Set the event name shown on screen and printed on badges
- Export a check-in report CSV (who checked in and when)
- Print a test badge
- Reset all check-ins

## Notes

- All data lives in the browser's `localStorage` on the kiosk machine —
  check-ins survive a page refresh or browser restart, but are per-machine.
  Export the check-in report before clearing browser data.
- Guests who are already checked in can tap their name again to reprint a
  lost badge (it won't double-count them).
- Walk-in guests are added to the same list and persist with everyone else;
  they're marked `walk_in=yes` in the exported report.
- The screen resets to the welcome state after 60 seconds of inactivity
  (`IDLE_RESET_MS`).
- A physical USB keyboard also works for search if you prefer it over the
  on-screen keyboard.

## Files

- `index.html` — the entire app (HTML, CSS, and JS in one file)
- `qrcode.js` — vendored MIT-licensed QR code generator (offline badge QR codes)
- `sample-guests.csv` — example guest list for testing the CSV upload
- `README.md` — this file

## Third-party

`qrcode.js` is [`qrcode-generator`](https://github.com/kazuhikoarase/qrcode-generator)
by Kazuhiko Arase, used under the MIT license. The license header is retained
at the top of the file.
