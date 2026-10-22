# Word add-in (optional, Windows only)

The task pane in `addin/` is a small Office.js client for the local review
server. Everything it shows is also available from the CLI, so skip this page
if you do not use Word. I did not run the add-in inside Word for this edition;
the pane's pure logic is covered by the node checks in the test suite
(`tests/js/`), and the server by `tests/test_serve_submission.py`.

## What the pane does

- **Review the open document**: sends the open document to the local server and shows the result.
- **Margin tab**: findings in order, worst first, with Prev / Next / Confirm & next.
- **Checklist tab**: the 15 items and their evidence state.
- **Gaps tab**: what the review did not establish.
- **Mark up the document**: adds comments anchored to the quoted text. **Remove markup** takes them out again.
- **Mark up a submission**: the same for an outgoing proposal (`cgate submission`).

The add-in is labelled "cgate (demo)".

## Set up

```powershell
pip install -e ".[serve]"
python -m cgate trust            # trust the locally generated TLS certificate
python -m cgate serve            # 127.0.0.1:8765, loopback only
python -m cgate addin --install  # point Word at addin/manifest.xml (per-user registry value)
```

Restart Word, then open the pane from the Home tab.

Undo:

```powershell
python -m cgate addin --remove
python -m cgate trust --remove
```

## Why TLS on localhost

Office will not load a task pane over plain HTTP. `cgate trust` issues a
self-signed certificate valid for `localhost` and nothing else, and stores it
under `%LOCALAPPDATA%\cgate\certs` (override with `CGATE_CERT_DIR`). `--insecure` serves plain HTTP for testing the
pane in a browser; Word will refuse it.

## Data

The server binds to loopback. It writes `review.json` and `review.md` under the
`--work` directory, the same files the CLI writes. Documents are not sent to any
other host.

## Fonts

The pane bundles three font families under the SIL Open Font License 1.1
(licence texts in `addin/assets/fonts/OFL-*.txt`): IBM Plex Mono, Libre
Franklin and Source Serif 4. See `NOTICE`.
