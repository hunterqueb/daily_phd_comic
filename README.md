# phd-daily

A small service that shows one PHD Comics strip ("Piled Higher and
Deeper" by Jorge Cham) each day. Use it in a Homarr **iFrame** widget.

- The service selects one comic from the archive for each day.
- All browsers show the same comic on the same day.
- The comic changes at midnight in the time zone that `TZ` sets.
- No comic repeats until all comics in the archive have had one day
  (approximately 5.6 years).
- The service gets the page and the images one time each day. It sends
  the images from its own address, so the browser does not connect to
  phdcomics.com.

The service uses only the Python standard library.

## Install

1. Put `phd_daily.py`, `Dockerfile` and `compose.yaml` in one folder.
2. In `compose.yaml`, set `TZ` to your time zone (for example
   `America/Chicago`).
3. Start the container:

   ```sh
   docker compose up -d --build
   ```

4. Open `http://<docker-host>:8085/?theme=dark` in a browser. Make sure
   that you see a comic.

Without Docker:

```sh
TZ=America/Chicago PHD_PORT=8085 python3 phd_daily.py
```

## Add the widget to Homarr

1. Open the board and go into edit mode.
2. Add an item. Select the **iFrame** widget.
3. Set **Embed URL** to `http://<docker-host>:8085/?theme=dark`.
   Use `theme=light` if the board uses the light color scheme.
4. Set **Allow scrolling** to off. (Use on only with `fit=width`.)
5. Save. Make the widget approximately 4 x 3 grid cells. Most strips
   are wider than they are tall.

A click on the comic opens the original page in a new tab.

### Important: HTTPS

If you open Homarr with `https://`, the browser blocks an `http://`
iFrame (mixed content). Put the service behind the same reverse proxy
as Homarr and use an `https://` URL in the widget.

If the proxy uses a path prefix, keep the slash at the end, for
example `https://home.example.com/phd/?theme=dark`. The page uses
relative image URLs.

### Important: theme

The iFrame is transparent only when its color scheme is the same as
the color scheme of the board. If they are different, the browser
paints a white background. Set `theme` to the color scheme of your
board. `theme=auto` follows the operating system, not Homarr.

### Optional: board CSS

The service makes the comic. You do not need CSS for it. To remove the
space around the widget:

1. Edit the widget. Open **Advanced options**. Add the custom CSS class
   `phd-comic`.
2. In the board settings, add this to **Custom CSS**:

   ```css
   /* Remove the padding around the comic widget. */
   .phd-comic {
     padding: 0 !important;
   }
   ```

## Page options

| Parameter | Values | Default | Effect |
|-----------|--------|---------|--------|
| `theme` | `auto`, `light`, `dark` | `auto` | Color scheme of the page. |
| `fit` | `contain`, `width` | `contain` | `contain` shows the full strip in the widget. `width` fills the width and the page scrolls. |
| `caption` | `1`, `0` | `1` | Show or hide the title, date and credit line. |
| `id` | comic ID | none | Show one specific comic. Use this to test the layout. |

Example: `http://<docker-host>:8085/?theme=dark&fit=width&caption=0`

## Settings

| Variable | Default | Effect |
|----------|---------|--------|
| `TZ` | `UTC` | Time zone for the day change. |
| `PHD_PORT` | `8080` | TCP port in the container. |
| `PHD_HOST` | `0.0.0.0` | Address to listen on. |
| `PHD_SALT` | `phd` | Sets the shuffle order. Change it to get a different sequence. |
| `PHD_LATEST_ID` | read from the site | Highest comic ID. The site value was 2051 in November 2026. |
| `PHD_SKIP_IDS` | none | Comma-separated comic IDs to skip. |
| `PHD_LOG_LEVEL` | `INFO` | Set `DEBUG` to log each request. |

## Endpoints

| Path | Content |
|------|---------|
| `/` | HTML page for the iFrame. |
| `/api/today` | JSON data for the comic of the day. |
| `/image/<id>/<n>` | Image `n` of comic `id`. |
| `/healthz` | Returns `ok`. |

## Troubleshooting

| Problem | Cause and action |
|---------|------------------|
| The widget is empty. | The browser cannot reach the URL, or it blocks mixed content. Open the URL in a new tab. Look at the browser console. |
| White box around the comic. | The `theme` value is not the same as the board color scheme. |
| "Offline." in the caption. | The service cannot reach phdcomics.com. It shows the last comic. It tries again after 5 minutes. |
| One comic is not good. | Add its ID to `PHD_SKIP_IDS`. Then restart the container. |

## Credit

"Piled Higher and Deeper" by Jorge Cham, www.phdcomics.com. The caption
shows this credit. The service is for personal dashboards. It gets one
page and its images each day.
