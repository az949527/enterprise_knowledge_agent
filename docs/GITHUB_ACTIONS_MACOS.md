# Build macOS Releases With GitHub Actions

The workflow builds two macOS variants:

- `arm64`: Apple Silicon Macs
- `x86_64`: Intel Macs

Both builds start without uploaded documents, local indexes, `.env` files, or an
LLM API Key.

## Before Pushing

The root `.gitignore` excludes:

- `data/`
- `outputs/`
- `.env` files
- virtual environments
- local IDE and browser automation files

Do not force-add those files to Git.

## Run Manually

1. Push the project to GitHub.
2. Open the repository's **Actions** tab.
3. Select **Build macOS Desktop**.
4. Choose **Run workflow**.
5. Wait for both matrix jobs to finish.
6. Download the artifacts from the workflow run page.

Artifacts:

```text
LocalKnowledgeTool-macOS-arm64
LocalKnowledgeTool-macOS-x86_64
```

Each artifact contains:

```text
LocalKnowledgeTool-macOS-<arch>.zip
LocalKnowledgeTool-macOS-<arch>.zip.sha256
```

## Build From A Tag

Tags beginning with `desktop-v` also trigger the workflow:

```bash
git tag desktop-v1.0.0
git push origin desktop-v1.0.0
```

## macOS Security

The generated `.app` is unsigned. Users may need to right-click the application
and choose **Open** on first launch.

For public distribution without Gatekeeper warnings, the app must later be
signed and notarized with an Apple Developer ID.
