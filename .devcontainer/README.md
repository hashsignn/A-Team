# Running this in a Codespace

## `onAutoForward` is `notify`, not `openPreview`

`openPreview` opens VS Code's **Simple Browser** — an embedded webview that
caches far harder than a real browser and gives you no way to force a reload.
It opens once, at whatever the app looked like at that moment, and keeps
showing that. Every deploy after the first then looks like nothing changed.

That is not hypothetical. It is exactly the symptom this repo produced in a
Codespace: the files were current, the server was serving current bytes, and
the pane showed a version from days earlier.

`notify` pops a toast with an **Open in Browser** button, which opens the
forwarded `https://…app.github.dev` URL in a real tab — where
`Cache-Control: no-store` is honoured and `Ctrl+Shift+R` works.

**If the preview pane is already open, close it.** Use the **PORTS** tab at
the bottom of VS Code, find port 8000, and click the globe icon (or
right-click → *Open in Browser*).

## The server binds `0.0.0.0` in here, not `127.0.0.1`

A Codespace reaches the app through a port forwarder that lives outside the
process namespace. Bound to loopback, the server can be curled from the same
terminal and still be invisible at the forwarded URL — a server that is
plainly running, and a page that will not load.

`run.py serve` detects `CODESPACES` and binds every interface automatically.
An explicit `--host` still wins.

## The checklist when something looks stale

```bash
git pull                                  # the workspace is a clone; it does not update itself
.venv/bin/python scripts/verify_install.py
```

That prints your commit, which features are on disk, and which are in the
bytes the running server sends. Whichever pair disagrees names the fix.

Then, in order:

1. **Nothing at all loads** → check the PORTS tab; port 8000 must be forwarded.
2. **Loads but looks old** → you are in the preview pane. Open it in a real browser.
3. **Real browser, still old** → `Ctrl+Shift+R` once, with the page focused.
4. **Still old** → the server is running from before your `git pull`. Stop it and start it again.
