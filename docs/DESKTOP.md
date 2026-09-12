# Secret Agent desktop app

The desktop build wraps the existing Secret Agent organizational-memory demo in a native macOS/Windows window. It does not change the product into a consumer assistant: the same organizational procedures, retrieval tools, SQLite memory, and Secret Agent branding are used.

## What the desktop build does

- Runs the existing Python backend locally on `127.0.0.1` using an ephemeral port.
- Stores the procedural-memory SQLite database in the user's normal application-data directory instead of the install directory.
- Opens the existing Secret Agent web interface inside a native desktop window with `pywebview`.
- On first launch, asks for an OpenRouter API key and stores it in the operating-system credential store when available.
- Never exposes the local HTTP server on the LAN.
- Reuses the current Liquid/OpenRouter inference path and the current 50,000-record procedural-memory seed.

## Run from source

```bash
python -m pip install -r requirements-desktop.txt
python desktop_app.py
```

To force the first-run credential screen again for development:

```bash
SECRET_AGENT_RESET_KEY=1 python desktop_app.py
```

On Windows PowerShell:

```powershell
$env:SECRET_AGENT_RESET_KEY="1"
python desktop_app.py
```

## Build installers

The GitHub Actions workflow at `.github/workflows/desktop-build.yml` builds both platforms:

- Windows: `SecretAgent-Windows.exe`
- macOS: `SecretAgent-macOS.dmg`

Run the workflow manually from GitHub Actions to get downloadable artifacts. Pushing a tag such as `v0.1.0` builds both installers and publishes them as assets on a GitHub Release.

## macOS signing/notarization

The initial workflow produces an unsigned `.dmg`. macOS may show a Gatekeeper warning when an unsigned build is downloaded from the internet. For public organizational distribution, add an Apple Developer ID certificate and notarization credentials to GitHub Actions, then sign/notarize the app before creating the DMG.

## Windows signing

The initial Windows executable is unsigned. For broad deployment, add an Authenticode code-signing certificate to the workflow so SmartScreen has publisher identity.

## Organizational deployment

For a managed organization, users should eventually not have to paste a shared inference key. The next production step is to replace first-run OpenRouter-key entry with organization sign-in and a managed inference endpoint. The desktop shell is intentionally separated from the current agent logic so that authentication can be swapped without rewriting the retrieval system.
